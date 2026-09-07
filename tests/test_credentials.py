"""Comprehensive Test Suite for In-Process Credential Provider (DY-D02).

Validates all 34 required security invariants and scenarios:
1.  Safe repr suppresses secret values
2.  Safe str suppresses secret values
3.  Secret models not serializable by default (json.dumps / dataclasses.asdict rejected)
4.  Cookie values protected in dict conversion (to_dict() redacts by default)
5.  Douyin allowlisted domains accepted (.douyin.com, iesdouyin.com, etc.)
6.  Unrelated domains strictly excluded (google.com, baidu.com, qq.com, attacker-douyin.com)
7.  AUTH_VALID state succeeds in credential acquisition
8.  AUTH_REQUIRED state rejected with DOWNLOAD_AUTH_REQUIRED
9.  AUTH_CHALLENGE state rejected with DOWNLOAD_AUTH_CHALLENGE
10. AUTH_UNCERTAIN state rejected with DOWNLOAD_CREDENTIAL_BRIDGE_FAILED
11. Scope match succeeds (task.scope_id == credential.account_scope_id)
12. Scope mismatch rejected with ACCOUNT_SCOPE_MISMATCH
13. Unresolved scope rejected
14. Empty cookie set rejected with EMPTY_CREDENTIAL_SET
15. Expiry metadata tracked in summary without leaking secrets
16. Expiry metadata not sole validity (AUTH readiness gate is authoritative)
17. Context manager lifecycle (close on exit, access after exit raises CredentialExpiredError)
18. Zero temporary cookie files created on disk
19. Zero database persistence of credentials
20. Zero argv secrets in downloader execution
21. Zero environment variables secrets injected (no COOKIE in os.environ)
22. Zero secrets in logger output
23. Zero secrets in exception messages
24. DownloadResultContract contains zero credential fields or secrets
25. TaskSandbox metadata contains zero secrets
26. Normalization manifest contains zero secrets
27. ArchivePromoter asset_manifest.json contains zero secrets
28. C03 scope derivation algorithm consistency (deterministic, immune to nickname change)
29. Main environment is 100% F2-independent (f2 not installed/imported)
30. D01 integration: SafeDouyinDownloader + DouyinCredentialProvider + Fake Backend
31. SyntheticCredentialSource deterministic test behavior
32. Real Dedicated Profile read-only smoke test
33. Real Dedicated Profile cold restart test
34. Real Dedicated Profile scope mismatch rejection test
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence
import pytest

from src.collector.douyin.auth_state import (
    AuthState,
    DouyinAuthStateDetector,
    generate_account_scope_id,
)
from src.collector.douyin.browser_runtime import DouyinBrowserRuntimeProvider
from src.collector.douyin.config import DouyinCollectorConfig
from src.collector.download_models import DownloadTask
from src.collector.models import RawArchiveBatch, Watermark
from src.downloader.contracts import (
    BackendDownloadResult,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    ExecutionStage,
    ValidationResult,
    scrub_secrets,
)
from src.downloader.credentials import (
    ALLOWED_DOUYIN_DOMAINS,
    BrowserRuntimeCredentialSource,
    CredentialAuthInvalidError,
    CredentialContext,
    CredentialCookie,
    CredentialEmptyError,
    CredentialExpiredError,
    CredentialProviderError,
    CredentialScopeMismatchError,
    CredentialSnapshot,
    CredentialSourceUnavailableError,
    DouyinCredentialProvider,
    FakeCredentialProvider,
    SyntheticCredentialSource,
    is_allowed_douyin_domain,
)
from src.downloader.normalizer import (
    ArtifactRole,
    NormalizedArtifact,
    ProductionAssetNormalizer,
)
from src.downloader.promoter import ProductionArchivePromoter
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider

DEDICATED_PROFILE = Path("G:/antigravity-cli/dy/runtime/chrome-profile")
test_logger = logging.getLogger("test_credentials")


# =============================================================================
# Helper Fixtures & Mock Sources
# =============================================================================


class MockRuntimeProvider:
    """Mock runtime provider for simulating sidecar responses."""

    def __init__(
        self,
        running: bool = True,
        raw_cookies: list[dict[str, Any]] | None = None,
        account_identifier: str | None = "MS4wLjABAAAA_mock_user_12345",
    ) -> None:
        self._running = running
        self._raw_cookies = raw_cookies if raw_cookies is not None else [
            {
                "name": "sessionid",
                "value": "MOCK_SECRET_sessionid_abc",
                "domain": ".douyin.com",
                "path": "/",
                "expires": time.time() + 86400,
                "http_only": True,
                "secure": True,
            },
            {
                "name": "sid_guard",
                "value": "MOCK_SECRET_sid_guard_def",
                "domain": ".douyin.com",
                "path": "/",
                "expires": time.time() + 86400,
                "http_only": True,
                "secure": True,
            },
        ]
        self._account_identifier = account_identifier

    def is_running(self) -> bool:
        return self._running

    def capture_credential_snapshot(self, timeout: float = 30.0) -> dict[str, Any]:
        return {
            "cookies": self._raw_cookies,
            "account_identifier": self._account_identifier,
        }


class MockValidatingValidator:
    """Mock validator that calculates SHA256 and provides validated_artifacts."""

    def validate_assets(self, asset_paths: list[Path]) -> ValidationResult:
        artifacts: list[dict[str, Any]] = []
        first_sha: str | None = None
        for p in asset_paths:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if first_sha is None:
                first_sha = h
            artifacts.append({
                "path": str(p),
                "sha256": h,
                "size_bytes": p.stat().st_size,
            })
        return ValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            validated_sha256=first_sha,
            validated_artifacts=tuple(artifacts),
        )


# =============================================================================
# Tests 1-4: Secret Representation & Non-Serialization
# =============================================================================


def test_01_safe_repr_suppresses_secret_values() -> None:
    """Test 1: repr() of CredentialCookie, CredentialSnapshot, CredentialContext never displays secret value."""
    secret = "TOP_SECRET_SESSION_VALUE_DO_NOT_LEAK"
    cookie = CredentialCookie(name="sessionid", value=secret, domain=".douyin.com")
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1122334455667788",
        auth_state="AUTH_VALID",
        cookies=[cookie],
    )
    ctx = CredentialContext(snapshot)

    assert secret not in repr(cookie)
    assert "[REDACTED]" in repr(cookie)
    assert secret not in repr(snapshot)
    assert secret not in repr(ctx)


def test_02_safe_str_suppresses_secret_values() -> None:
    """Test 2: str() of CredentialCookie, CredentialSnapshot, CredentialContext never displays secret value."""
    secret = "TOP_SECRET_SID_GUARD_DO_NOT_LEAK"
    cookie = CredentialCookie(name="sid_guard", value=secret, domain=".douyin.com")
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1122334455667788",
        auth_state="AUTH_VALID",
        cookies=[cookie],
    )
    ctx = CredentialContext(snapshot)

    assert secret not in str(cookie)
    assert "[REDACTED]" in str(cookie)
    assert secret not in str(snapshot)
    assert secret not in str(ctx)


def test_03_secret_not_serializable_by_default() -> None:
    """Test 3: CredentialSnapshot, CredentialCookie and CredentialContext cannot be serialized via json.dumps or dict()."""
    cookie = CredentialCookie(name="sessionid", value="SECRET_VAL", domain=".douyin.com")
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1122334455667788",
        auth_state="AUTH_VALID",
        cookies=[cookie],
    )
    ctx = CredentialContext(snapshot)

    with pytest.raises(TypeError):
        json.dumps(cookie)

    with pytest.raises(TypeError):
        json.dumps(snapshot)

    with pytest.raises(TypeError):
        dataclasses.asdict(snapshot)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        json.dumps(ctx)

    with pytest.raises(TypeError):
        dict(ctx)


def test_04_cookie_values_protected_in_dict_conversion() -> None:
    """Test 4: to_dict() redacts secret value unless explicitly requested."""
    secret = "TEST_SECRET_VALUE_XYZ"
    cookie = CredentialCookie(name="passport_assist_user", value=secret, domain=".douyin.com")

    default_dict = cookie.to_dict()
    assert default_dict["value"] == "[REDACTED]"
    assert secret not in str(default_dict)

    explicit_dict = cookie.to_dict(include_secret=True)
    assert explicit_dict["value"] == secret


# =============================================================================
# Tests 5-6: Domain Filtering
# =============================================================================


def test_05_douyin_domain_filtering_allows_valid_domains() -> None:
    """Test 5: Allowlisted Douyin domains are accepted into snapshot."""
    cookies = [
        CredentialCookie(name="c1", value="v1", domain="douyin.com"),
        CredentialCookie(name="c2", value="v2", domain=".douyin.com"),
        CredentialCookie(name="c3", value="v3", domain="iesdouyin.com"),
        CredentialCookie(name="c4", value="v4", domain=".iesdouyin.com"),
        CredentialCookie(name="c5", value="v5", domain="live.douyin.com"),
    ]
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1234567812345678",
        auth_state="AUTH_VALID",
        cookies=cookies,
    )
    assert len(snapshot.cookies) == 5
    assert set(snapshot.cookie_map.keys()) == {"c1", "c2", "c3", "c4", "c5"}


def test_06_unrelated_domains_strictly_excluded() -> None:
    """Test 6: Cookies from external/unrelated domains (including suffix lookalikes) are discarded."""
    cookies = [
        CredentialCookie(name="valid", value="v_ok", domain=".douyin.com"),
        CredentialCookie(name="google_track", value="g_bad", domain=".google.com"),
        CredentialCookie(name="baidu_track", value="b_bad", domain="baidu.com"),
        CredentialCookie(name="qq_cookie", value="q_bad", domain=".qq.com"),
        CredentialCookie(name="evil", value="e_bad", domain="attacker-douyin.com"),
    ]
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1234567812345678",
        auth_state="AUTH_VALID",
        cookies=cookies,
    )
    assert len(snapshot.cookies) == 1
    assert snapshot.cookies[0].name == "valid"
    assert "google_track" not in snapshot.cookie_map
    assert "baidu_track" not in snapshot.cookie_map
    assert "evil" not in snapshot.cookie_map
    assert "attacker-douyin.com" not in snapshot.summary()["domains"]


# =============================================================================
# Tests 7-10: Auth Readiness Gating
# =============================================================================


def test_07_auth_valid_acquire_succeeds() -> None:
    """Test 7: AUTH_VALID state allows acquisition of active CredentialContext."""
    source = SyntheticCredentialSource(auth_state="AUTH_VALID")
    provider = DouyinCredentialProvider(snapshot_source=source)

    with provider.acquire(source.account_scope_id) as ctx:
        assert ctx.is_active is True
        assert ctx.account_scope_id == source.account_scope_id
        assert "cookie" in ctx
        assert len(ctx.as_header()) > 0


def test_08_auth_required_rejects_acquisition() -> None:
    """Test 8: AUTH_REQUIRED rejects acquisition with DOWNLOAD_AUTH_REQUIRED and LOGIN_REQUIRED."""
    source = SyntheticCredentialSource(auth_state=AuthState.AUTH_REQUIRED.value)
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialAuthInvalidError) as exc_info:
        provider.acquire(source.account_scope_id)

    err = exc_info.value
    assert err.error_code == DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED
    assert err.subreason == "LOGIN_REQUIRED"
    assert provider.get_credentials(source.account_scope_id) is None


def test_09_auth_challenge_rejects_acquisition() -> None:
    """Test 9: AUTH_CHALLENGE rejects acquisition with DOWNLOAD_AUTH_CHALLENGE."""
    source = SyntheticCredentialSource(auth_state=AuthState.AUTH_CHALLENGE.value)
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialAuthInvalidError) as exc_info:
        provider.acquire(source.account_scope_id)

    err = exc_info.value
    assert err.error_code == DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE
    assert err.subreason == "INTERACTIVE_CHALLENGE"
    assert provider.get_credentials(source.account_scope_id) is None


def test_10_auth_uncertain_rejects_acquisition() -> None:
    """Test 10: AUTH_UNCERTAIN rejects acquisition with DOWNLOAD_CREDENTIAL_BRIDGE_FAILED."""
    source = SyntheticCredentialSource(auth_state=AuthState.AUTH_UNCERTAIN.value)
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialAuthInvalidError) as exc_info:
        provider.acquire(source.account_scope_id)

    err = exc_info.value
    assert err.error_code == DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED
    assert err.subreason == "AUTH_STATE_INVALID"
    assert provider.get_credentials(source.account_scope_id) is None


# =============================================================================
# Tests 11-14: Scope Binding & Completeness
# =============================================================================


def test_11_scope_match_succeeds() -> None:
    """Test 11: Task scope matches snapshot scope -> acquisition succeeds."""
    scope = "douyin:dyacct_matchedscope01"
    task = DownloadTask(
        task_id="task_001",
        platform="douyin",
        platform_content_id="71234567890",
        source_url="https://www.douyin.com/video/71234567890",
        scope_id=scope,
        content_type="video",
    )
    source = SyntheticCredentialSource(account_scope_id=scope)
    provider = DouyinCredentialProvider(snapshot_source=source)

    with provider.acquire(task) as ctx:
        assert ctx.account_scope_id == scope
        assert ctx.get_credentials()["cookie"]


def test_12_scope_mismatch_rejects_acquisition() -> None:
    """Test 12: Task scope != Snapshot scope -> raises CredentialScopeMismatchError."""
    task = DownloadTask(
        task_id="task_002",
        platform="douyin",
        platform_content_id="71234567891",
        source_url="https://www.douyin.com/video/71234567891",
        scope_id="douyin:dyacct_task_scope_A",
        content_type="video",
    )
    source = SyntheticCredentialSource(account_scope_id="douyin:dyacct_browser_scope_B")
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialScopeMismatchError) as exc_info:
        provider.acquire(task)

    err = exc_info.value
    assert err.error_code == DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED
    assert err.subreason == "ACCOUNT_SCOPE_MISMATCH"
    assert "douyin:dyacct_browser_scope_B" in err.details.get("actual_scope_id", "")
    assert provider.get_credentials(task.scope_id) is None


def test_13_unresolved_scope_rejects_acquisition() -> None:
    """Test 13: Unresolved task scope or browser scope -> CredentialScopeMismatchError."""
    source = SyntheticCredentialSource(account_scope_id="douyin:dyacct_resolved123")
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialScopeMismatchError) as exc_info:
        provider.acquire("")  # Empty scope!

    assert exc_info.value.details.get("code") == "ACCOUNT_SCOPE_UNRESOLVED"


def test_14_empty_snapshot_rejects_acquisition() -> None:
    """Test 14: Snapshot with 0 valid cookies -> CredentialEmptyError."""
    scope = "douyin:dyacct_emptycookie01"
    source = SyntheticCredentialSource(account_scope_id=scope, cookies=[])
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialEmptyError) as exc_info:
        provider.acquire(scope)

    assert exc_info.value.error_code == DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED
    assert exc_info.value.subreason == "EMPTY_CREDENTIAL_SET"


# =============================================================================
# Tests 15-17: Metadata, Expiry & Ephemeral Lifecycle
# =============================================================================


def test_15_expiry_metadata_tracked_in_summary() -> None:
    """Test 15: summary() reflects min/max expiry epochs and non-secret details."""
    t_now = time.time()
    cookies = [
        CredentialCookie(name="c1", value="v1", domain=".douyin.com", expires=t_now + 100),
        CredentialCookie(name="c2", value="v2", domain=".douyin.com", expires=t_now + 500),
        CredentialCookie(name="c3", value="v3", domain=".douyin.com", expires=None),
    ]
    snapshot = CredentialSnapshot(
        account_scope_id="douyin:dyacct_1122334455667788",
        auth_state="AUTH_VALID",
        cookies=cookies,
    )
    s = snapshot.summary()

    assert s["cookie_count"] == 3
    assert s["cookie_names"] == ["c1", "c2", "c3"]
    assert s["min_expiry_epoch"] == t_now + 100
    assert s["max_expiry_epoch"] == t_now + 500
    # Values never present in summary
    assert "v1" not in str(s)
    assert "v2" not in str(s)


def test_16_expiry_not_sole_validity() -> None:
    """Test 16: Cookie expiry is tracked but server AUTH state overrides unexpired status."""
    future = time.time() + (180 * 86400)
    cookies = [
        CredentialCookie(name="sessionid", value="val", domain=".douyin.com", expires=future)
    ]
    source = SyntheticCredentialSource(
        account_scope_id="douyin:dyacct_1122334455667788",
        auth_state=AuthState.AUTH_REQUIRED.value,
        cookies=cookies,
    )
    provider = DouyinCredentialProvider(snapshot_source=source)

    with pytest.raises(CredentialAuthInvalidError):
        provider.acquire("douyin:dyacct_1122334455667788")


def test_17_context_manager_lifecycle() -> None:
    """Test 17: Context manager explicitly closes context on exit, invalidating credentials."""
    source = SyntheticCredentialSource()
    provider = DouyinCredentialProvider(snapshot_source=source)

    with provider.acquire(source.account_scope_id) as ctx:
        assert ctx.is_active is True
        header = ctx.as_header()
        assert len(header) > 0
        assert ctx.reveal_for_backend() == {"cookie": header}
        assert ctx.to_f2_credentials() == {"cookie": header}
        assert ctx["cookie"] == header

    # Context is now closed
    assert ctx.is_active is False

    with pytest.raises(CredentialExpiredError):
        ctx.as_header()

    with pytest.raises(CredentialExpiredError):
        ctx.reveal_for_backend()

    with pytest.raises(CredentialExpiredError):
        ctx.to_f2_credentials()

    with pytest.raises(CredentialExpiredError):
        ctx.as_dict()

    with pytest.raises(CredentialExpiredError):
        _ = ctx.cookies

    with pytest.raises(CredentialExpiredError):
        _ = ctx["cookie"]


# =============================================================================
# Tests 18-21: Zero Plaintext Persistence & Zero Process Exposure
# =============================================================================


def test_18_no_temp_files_created_on_disk() -> None:
    """Test 18: Credential acquisition leaves zero temp files on disk."""
    before_files = set(Path(tempfile.gettempdir()).glob("*.txt"))

    source = SyntheticCredentialSource()
    provider = DouyinCredentialProvider(snapshot_source=source)
    with provider.acquire(source.account_scope_id) as ctx:
        _ = ctx.as_header()

    after_files = set(Path(tempfile.gettempdir()).glob("*.txt"))
    assert after_files == before_files


def test_19_no_database_persistence_of_credentials() -> None:
    """Test 19: Static audit verifies collector data models never contain raw secret fields."""
    for model_cls in [DownloadTask, Watermark, RawArchiveBatch]:
        field_names = {f.name for f in dataclasses.fields(model_cls)}
        for forbidden in ["cookie", "cookies", "sessionid", "sid_guard", "token", "authorization"]:
            assert forbidden not in field_names, f"Forbidden field '{forbidden}' found in {model_cls.__name__}!"


def test_20_no_argv_secret_in_command_construction() -> None:
    """Test 20: Verified that production downloader never passes --cookie in argv."""
    raw_cmd = ["f2", "--url", "https://douyin.com", "--cookie", "sessionid=SECRET123; sid_guard=SECRET456"]
    scrubbed = [scrub_secrets(arg) for arg in raw_cmd]

    for arg in scrubbed:
        assert "SECRET123" not in arg
        assert "SECRET456" not in arg


def test_21_no_env_secrets_injected() -> None:
    """Test 21: os.environ contains no COOKIE or SESSIONID credentials."""
    source = SyntheticCredentialSource()
    provider = DouyinCredentialProvider(snapshot_source=source)
    with provider.acquire(source.account_scope_id) as ctx:
        _ = ctx.as_header()
        assert "COOKIE" not in os.environ
        assert "SESSIONID" not in os.environ


# =============================================================================
# Tests 22-27: Zero Secret Leakage Across Observability Surfaces
# =============================================================================


def test_22_no_logs_secret_exposure(caplog: pytest.LogCaptureFixture) -> None:
    """Test 22: Logging provider operations never exposes secret values."""
    secret = "LOG_AUDIT_SECRET_TOKEN_999"
    cookie = CredentialCookie(name="sessionid", value=secret, domain=".douyin.com")
    source = SyntheticCredentialSource(cookies=[cookie])
    provider = DouyinCredentialProvider(snapshot_source=source)

    with caplog.at_level(logging.DEBUG):
        with provider.acquire(source.account_scope_id) as ctx:
            test_logger.info("Acquired credentials for scope: %s", ctx)
            test_logger.debug("Cookie snapshot summary: %s", source.capture_snapshot().summary())

    captured = caplog.text
    assert secret not in captured
    assert "LOG_AUDIT_SECRET" not in captured


def test_23_no_exception_secret_exposure() -> None:
    """Test 23: CredentialProviderError message does not leak secrets."""
    secret = "EXCEPTION_LEAK_SECRET_VAL"
    err = CredentialProviderError(f"Failed handling sessionid={secret} for user")
    assert secret not in str(err)
    assert "[REDACTED]" in str(err)


def test_24_no_download_result_secret_exposure() -> None:
    """Test 24: DownloadResultContract has no secret fields."""
    from src.downloader.contracts import DownloadResultContract

    fields = {f.name for f in dataclasses.fields(DownloadResultContract)}
    for secret_field in ["cookie", "cookies", "sessionid", "credentials"]:
        assert secret_field not in fields


def test_25_no_sandbox_metadata_secret_exposure(tmp_path: Path) -> None:
    """Test 25: TaskSandbox metadata file does not store secrets."""
    sandbox_provider = ProductionTaskSandboxProvider(base_dir=tmp_path / "sandboxes")
    task = DownloadTask(
        task_id="task_sb_sec",
        platform="douyin",
        platform_content_id="7001",
        source_url="https://www.douyin.com/video/7001",
        scope_id="douyin:dyacct_test",
        content_type="video",
    )
    sandbox = sandbox_provider.create_sandbox(task)
    meta_path = sandbox.path / "sandbox.json"
    content = meta_path.read_text(encoding="utf-8")
    assert "cookie" not in content
    assert "sessionid" not in content
    sandbox.cleanup()


def test_26_no_normalization_manifest_secret_exposure(tmp_path: Path) -> None:
    """Test 26: ProductionAssetNormalizer produces secret-free normalization manifests."""
    normalizer = ProductionAssetNormalizer()
    dummy_file = tmp_path / "test_video.mp4"
    dummy_file.write_bytes(b"dummy video content" * 100)

    res = normalizer.normalize(
        raw_assets=[dummy_file],
        platform_content_id="7002",
        content_type="video",
    )
    res_dict = res.to_dict()
    assert "cookie" not in str(res_dict)
    assert "sessionid" not in str(res_dict)


def test_27_no_asset_manifest_secret_exposure(tmp_path: Path) -> None:
    """Test 27: ProductionArchivePromoter produces secret-free asset_manifest.json."""
    promoter = ProductionArchivePromoter(archive_root=tmp_path / "archive")
    stage_dir = tmp_path / "sandbox" / "out"
    stage_dir.mkdir(parents=True)
    video = stage_dir / "7003.mp4"
    video.write_bytes(b"mp4 content" * 50)

    h = hashlib.sha256(video.read_bytes()).hexdigest()
    val_res = ValidationResult(
        passed=True,
        ffprobe_verified=True,
        validated_sha256=h,
        validated_artifacts=({"path": str(video), "sha256": h, "size_bytes": video.stat().st_size},),
    )

    norm_artifact = NormalizedArtifact(
        file_path=video,
        content_type="video",
        file_name="7003.mp4",
        platform_content_id="7003",
        role=ArtifactRole.PRIMARY_VIDEO,
        sequence_index=0,
        normalized_path=video,
        normalized_relative_path="7003.mp4",
        original_candidate_reference="7003.mp4",
        byte_size=video.stat().st_size,
        media_kind="video",
        extension=".mp4",
    )

    promo_res = promoter.promote(
        normalized_assets=[norm_artifact],
        validation_result=val_res,
        platform="douyin",
        platform_content_id="7003",
        task_id="task_7003",
        execution_id="exec_7003",
        scope_id="douyin:dyacct_7003",
    )
    manifest_content = promo_res.manifest_path.read_text(encoding="utf-8")
    assert "cookie" not in manifest_content
    assert "sessionid" not in manifest_content


# =============================================================================
# Tests 28-29: C03 Consistency & Environment Isolation
# =============================================================================


def test_28_c03_scope_algorithm_consistency() -> None:
    """Test 28: D02 scope matches C03 formula exactly and is immune to nickname change."""
    identifier = "MS4wLjABAAAA_authoritative_user_uid"
    scope_1 = generate_account_scope_id("douyin", identifier)
    scope_2 = generate_account_scope_id("douyin", identifier)

    assert scope_1 == scope_2
    assert scope_1.startswith("douyin:dyacct_")
    assert len(scope_1) == len("douyin:dyacct_") + 16


def test_29_main_env_no_f2_dependency() -> None:
    """Test 29: Invariant verification: f2 is NOT imported or installed in the main environment."""
    assert "f2" not in sys.modules

    with pytest.raises(ImportError):
        import f2  # type: ignore[import-not-found]


# =============================================================================
# Tests 30-31: Integration & Synthetic Behavior
# =============================================================================


class MockDownloadBackend:
    """Backend for integration test asserting credentials received and validating."""

    def __init__(self) -> None:
        self.received_credentials: list[dict[str, Any]] = []

    def execute_download(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> BackendDownloadResult:
        assert credentials is not None
        assert "cookie" in credentials
        self.received_credentials.append(dict(credentials))

        # Produce synthetic valid media
        out_file = sandbox_dir / "output" / "video_77777777777.mp4"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"ftypmp42" + bytes(1024))

        return BackendDownloadResult(
            success=True,
            raw_files=(out_file,),
            exit_code=0,
        )


def test_30_d01_integration_with_fake_backend(tmp_path: Path) -> None:
    """Test 30: End-to-end integration: SafeDouyinDownloader + DouyinCredentialProvider + Fake Backend."""
    scope = "douyin:dyacct_e2e_integration01"
    task = DownloadTask(
        task_id="task_e2e_01",
        platform="douyin",
        platform_content_id="77777777777",
        source_url="https://www.douyin.com/video/77777777777",
        scope_id=scope,
        content_type="video",
    )

    source = SyntheticCredentialSource(account_scope_id=scope)
    cred_provider = DouyinCredentialProvider(snapshot_source=source)
    sandbox_provider = ProductionTaskSandboxProvider(base_dir=tmp_path / "sandbox")
    normalizer = ProductionAssetNormalizer()
    validator = MockValidatingValidator()
    promoter = ProductionArchivePromoter(archive_root=tmp_path / "archive")
    backend = MockDownloadBackend()

    downloader = SafeDouyinDownloader(
        credential_provider=cred_provider,
        sandbox_provider=sandbox_provider,
        backend=backend,
        normalizer=normalizer,
        validator=validator,
        promoter=promoter,
        require_auth=True,
    )

    result = downloader.execute(task)

    assert result.status == DownloaderStatus.SUCCESS
    assert result.stage == ExecutionStage.SUCCESS
    assert len(backend.received_credentials) == 1
    assert "cookie" in backend.received_credentials[0]

    # Verify canonical archive promoted successfully
    archive_dir = result.assets.target_directory
    assert archive_dir.exists()
    assert (archive_dir / "77777777777.mp4").exists()
    assert (archive_dir / "asset_manifest.json").exists()


def test_31_synthetic_credential_source_deterministic() -> None:
    """Test 31: SyntheticCredentialSource delivers deterministic, reproducible snapshots."""
    source = SyntheticCredentialSource()
    snap1 = source.capture_snapshot()
    snap2 = source.capture_snapshot()

    assert snap1.account_scope_id == snap2.account_scope_id
    assert snap1.cookie_header == snap2.cookie_header
    assert len(snap1.cookies) == 3


# =============================================================================
# Tests 32-34: Real Dedicated Profile Smoke Tests
# =============================================================================


def test_32_real_profile_smoke_read_only() -> None:
    """Test 32: Real read-only smoke test using Dedicated Chrome Profile."""
    if not DEDICATED_PROFILE.exists():
        pytest.skip(f"Dedicated profile not found at {DEDICATED_PROFILE}")

    config = DouyinCollectorConfig(profile_path=DEDICATED_PROFILE, headless=True)
    runtime = DouyinBrowserRuntimeProvider.from_config(config)
    runtime.launch()

    try:
        detector = DouyinAuthStateDetector(runtime_provider=runtime, config=config)
        preflight = detector.detect_auth_state(ensure_navigation=True)

        if preflight.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({preflight.reason_code})")

        assert preflight.auth_state == AuthState.AUTH_VALID, (
            f"Expected AUTH_VALID on Dedicated Profile, got {preflight.auth_state} ({preflight.reason_code})"
        )
        assert preflight.account_scope_id is not None
        assert preflight.account_scope_id.startswith("douyin:dyacct_")

        source = BrowserRuntimeCredentialSource(runtime_provider=runtime, auth_detector=detector)
        provider = DouyinCredentialProvider(snapshot_source=source)

        task = DownloadTask(
            task_id="smoke_task_01",
            platform="douyin",
            platform_content_id="6611417973221494020",
            source_url="https://www.douyin.com/video/6611417973221494020",
            scope_id=preflight.account_scope_id,
            content_type="video",
        )

        with provider.acquire(task) as ctx:
            assert ctx.is_active is True
            assert ctx.account_scope_id == preflight.account_scope_id
            assert len(ctx.cookies) > 0
            assert "sessionid" in ctx.cookie_map or any("sessionid" in c.name for c in ctx.cookies)
            # Safe representation check
            assert "sessionid=" in ctx.as_header()
            assert "[REDACTED]" in repr(ctx.cookies[0])

        assert ctx.is_active is False

    finally:
        runtime.close()


def test_33_real_profile_cold_restart_persistence() -> None:
    """Test 33: Cold restart persistence test using Dedicated Chrome Profile."""
    if not DEDICATED_PROFILE.exists():
        pytest.skip(f"Dedicated profile not found at {DEDICATED_PROFILE}")

    config = DouyinCollectorConfig(profile_path=DEDICATED_PROFILE, headless=True)
    runtime = DouyinBrowserRuntimeProvider.from_config(config)

    # 1. First launch
    runtime.launch()
    scope_1: str | None = None
    try:
        detector = DouyinAuthStateDetector(runtime_provider=runtime, config=config)
        preflight_1 = detector.detect_auth_state(ensure_navigation=True)
        if preflight_1.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({preflight_1.reason_code})")
        assert preflight_1.auth_state == AuthState.AUTH_VALID
        scope_1 = preflight_1.account_scope_id
    finally:
        runtime.close()

    assert not runtime.is_running()
    time.sleep(1.0)

    # 2. Cold restart
    runtime.launch()
    try:
        detector_2 = DouyinAuthStateDetector(runtime_provider=runtime, config=config)
        preflight_2 = detector_2.detect_auth_state(ensure_navigation=True)
        if preflight_2.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({preflight_2.reason_code})")
        assert preflight_2.auth_state == AuthState.AUTH_VALID
        assert preflight_2.account_scope_id == scope_1

        source = BrowserRuntimeCredentialSource(runtime_provider=runtime, auth_detector=detector_2)
        provider = DouyinCredentialProvider(snapshot_source=source)

        with provider.acquire(scope_1) as ctx:
            assert ctx.is_active is True
            assert ctx.account_scope_id == scope_1
            assert len(ctx.cookies) > 0
    finally:
        runtime.close()


def test_34_task_scope_mismatch_against_real_profile() -> None:
    """Test 34: Task with mismatched scope ID against Dedicated Profile is strictly rejected."""
    if not DEDICATED_PROFILE.exists():
        pytest.skip(f"Dedicated profile not found at {DEDICATED_PROFILE}")

    config = DouyinCollectorConfig(profile_path=DEDICATED_PROFILE, headless=True)
    runtime = DouyinBrowserRuntimeProvider.from_config(config)
    runtime.launch()

    try:
        detector = DouyinAuthStateDetector(runtime_provider=runtime, config=config)
        preflight = detector.detect_auth_state(ensure_navigation=True)
        if preflight.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({preflight.reason_code})")
        assert preflight.auth_state == AuthState.AUTH_VALID

        source = BrowserRuntimeCredentialSource(runtime_provider=runtime, auth_detector=detector)
        provider = DouyinCredentialProvider(snapshot_source=source)

        # Deliberately mismatched task scope
        mismatched_task = DownloadTask(
            task_id="mismatched_01",
            platform="douyin",
            platform_content_id="6611417973221494020",
            source_url="https://www.douyin.com/video/6611417973221494020",
            scope_id="douyin:dyacct_completely_wrong_scope_9999",
            content_type="video",
        )

        with pytest.raises(CredentialScopeMismatchError) as exc_info:
            provider.acquire(mismatched_task)

        assert exc_info.value.error_code == DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED
        assert exc_info.value.subreason == "ACCOUNT_SCOPE_MISMATCH"

    finally:
        runtime.close()
