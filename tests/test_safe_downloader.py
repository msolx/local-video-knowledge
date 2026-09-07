"""Comprehensive Unit and Component Tests for SafeDouyinDownloader (DY-D01).

Validates:
1. QW-13 DownloadResultContract v1 schema compliance and JSON serializability.
2. Secret scrubbing invariants across messages, errors, and validation outputs.
3. Strict environment isolation (NO direct f2 imports in main python env).
4. Full 8-stage state machine transitions and stage ordering.
5. Sandbox lifecycle and guaranteed cleanup on success and failure.
6. Archive safety invariants (no promotion on validation failure).
7. Error taxonomy classification (15 error codes across 5 categories) and retry policies.
8. Idempotency bypass (SKIPPED on existing valid assets).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from src.collector.download_models import DownloadPriority, DownloadReason, DownloadTask
from src.downloader.contracts import (
    ArchivePromoter,
    AssetNormalizer,
    AssetStateInspector,
    BackendDownloadResult,
    ContentRouter,
    CredentialProvider,
    DownloadBackend,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadErrorPolicy,
    DownloadResultContract,
    ExecutionStage,
    MediaValidator,
    NormalizedAsset,
    TaskSandbox,
    TaskSandboxProvider,
    ValidationResult,
    scrub_secrets,
)
from src.downloader.douyin_f2 import F2DouyinDownloader
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.stubs import (
    DefaultDownloadErrorPolicy,
    DefaultTaskSandboxProvider,
    FakeArchivePromoter,
    FakeAssetNormalizer,
    FakeAssetStateInspector,
    FakeContentRouter,
    FakeDownloadBackend,
    FakeMediaValidator,
    InMemoryTaskSandbox,
    NullCredentialProvider,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_task() -> DownloadTask:
    return DownloadTask(
        task_id="dl_douyin_7671141177986518318_a548f124e58e8e6a",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="7671141177986518318",
        content_type="video",
        source_url="https://www.douyin.com/video/7671141177986518318",
        download_input={
            "play_addr_h264": "https://aweme.snssdk.com/video/tos/sample.mp4",
            "is_volatile_hint": True,
            "width": 1080,
            "height": 1920,
        },
    )


# =============================================================================
# 1. Contract & Schema Serialization Tests
# =============================================================================


def test_01_download_result_contract_defaults_and_dict_serialization() -> None:
    """Test 01: DownloadResultContract conforms strictly to QW-13 schema."""
    contract = DownloadResultContract(
        source_url="https://www.douyin.com/video/123456",
        platform_content_id="123456",
        status=DownloaderStatus.SUCCESS,
        error_code=None,
        retryable=False,
        retry_after=None,
        message="All good",
        assets=[
            DownloadedAsset(
                file_name="123456.mp4",
                relative_path="123456.mp4",
                size_bytes=1048576,
                content_type="video",
                sha256="abc123hash",
                width=1080,
                height=1920,
                duration_sec=12.5,
            )
        ],
        validation={"passed": True, "ffprobe_verified": True},
        diagnostics_ref=None,
        elapsed_sec=1.234,
        stage=ExecutionStage.SUCCESS,
    )

    data = contract.to_dict()
    assert data["source_url"] == "https://www.douyin.com/video/123456"
    assert data["platform_content_id"] == "123456"
    assert data["status"] == "SUCCESS"
    assert data["error_code"] is None
    assert data["retryable"] is False
    assert data["retry_after"] is None
    assert data["message"] == "All good"
    assert len(data["assets"]) == 1
    assert data["assets"][0]["size_bytes"] == 1048576
    assert data["validation"]["passed"] is True
    assert data["stage"] == "SUCCESS"
    assert data["schema_version"] == "download-result-v1"


def test_02_download_result_contract_to_json() -> None:
    """Test 02: to_json() produces valid, parseable JSON text."""
    contract = DownloadResultContract(
        source_url="https://www.douyin.com/video/123456",
        platform_content_id="123456",
        status=DownloaderStatus.FAILED,
        error_code="DOWNLOAD_NETWORK_ERROR",
        retryable=True,
        retry_after=2,
        message="Connection reset by peer",
        stage=ExecutionStage.DOWNLOADING,
    )
    json_str = contract.to_json()
    assert "DOWNLOAD_NETWORK_ERROR" in json_str
    assert '"retryable": true' in json_str
    assert '"status": "FAILED"' in json_str


def test_03_downloaded_asset_to_dict() -> None:
    """Test 03: DownloadedAsset serializes completely."""
    asset = DownloadedAsset(
        file_name="test.mp4",
        relative_path="folder/test.mp4",
        size_bytes=500,
        content_type="video",
        sha256="fake_sha",
        width=720,
        height=1280,
        duration_sec=10.0,
    )
    d = asset.to_dict()
    assert d["file_name"] == "test.mp4"
    assert d["size_bytes"] == 500
    assert d["width"] == 720


def test_04_validation_result_serialization() -> None:
    """Test 04: ValidationResult serializes properly and redacts error."""
    val = ValidationResult(
        passed=False,
        ffprobe_verified=False,
        decode_smoke_verified=False,
        error="Decryption failed with sessionid=secret_cookie_val",
    )
    d = val.to_dict()
    assert d["passed"] is False
    assert "secret_cookie_val" not in d["error"]
    assert "sessionid=[REDACTED]" in d["error"]


# =============================================================================
# 2. Secret Scrubbing Invariant Tests
# =============================================================================


def test_05_secret_scrubbing_sessionid() -> None:
    """Test 05: sessionid and sessionid_ss are properly redacted."""
    raw = "Failed on URL with sessionid=abcd1234efgh and sessionid_ss=xyz987"
    scrubbed = scrub_secrets(raw)
    assert "abcd1234efgh" not in scrubbed
    assert "xyz987" not in scrubbed
    assert "sessionid=[REDACTED]" in scrubbed
    assert "sessionid_ss=[REDACTED]" in scrubbed


def test_06_secret_scrubbing_sid_guard_and_sid_tt() -> None:
    """Test 06: sid_guard and sid_tt cookies are redacted."""
    raw = "Request header: sid_guard=token_guard_123; sid_tt=token_tt_456"
    scrubbed = scrub_secrets(raw)
    assert "token_guard_123" not in scrubbed
    assert "token_tt_456" not in scrubbed
    assert "sid_guard=[REDACTED]" in scrubbed
    assert "sid_tt=[REDACTED]" in scrubbed


def test_07_secret_scrubbing_ms_token_and_a_bogus() -> None:
    """Test 07: msToken and a_bogus signature parameters are redacted."""
    raw = "URL query: msToken=abcde12345_XYZ.99&a_bogus=mSgP987+/ab=="
    scrubbed = scrub_secrets(raw)
    assert "abcde12345_XYZ.99" not in scrubbed
    assert "mSgP987+/ab==" not in scrubbed
    assert "msToken=[REDACTED]" in scrubbed
    assert "a_bogus=[REDACTED]" in scrubbed


def test_08_secret_scrubbing_bearer_and_csrf_tokens() -> None:
    """Test 08: Bearer tokens and passport CSRF tokens are redacted."""
    raw = "Authorization: Bearer my_secret_token_123; passport_csrf_token=csrf_val_999"
    scrubbed = scrub_secrets(raw)
    assert "my_secret_token_123" not in scrubbed
    assert "csrf_val_999" not in scrubbed
    assert "Bearer [REDACTED]" in scrubbed
    assert "passport_csrf_token=[REDACTED]" in scrubbed


def test_09_secret_scrubbing_in_contract_post_init() -> None:
    """Test 09: DownloadResultContract automatically scrubs message in post_init."""
    contract = DownloadResultContract(
        source_url="https://douyin.com/1",
        platform_content_id="1",
        status=DownloaderStatus.FAILED,
        message="Backend crash with sessionid=super_secret_cookie_val_12345",
    )
    assert "super_secret_cookie_val_12345" not in contract.message
    assert "sessionid=[REDACTED]" in contract.message


def test_10_secret_scrubbing_command_line_cookie() -> None:
    """Test 10: --cookie arguments are redacted from command line strings and reprs."""
    raw_cli = "f2 dy --cookie sessionid=12345;msToken=67890 --output ./dir"
    scrubbed_cli = scrub_secrets(raw_cli)
    assert "--cookie [REDACTED]" in scrubbed_cli
    assert "sessionid=12345" not in scrubbed_cli

    raw_list = "subprocess args: ['f2', 'dy', '--cookie', 'sessionid=12345; msToken=67890']"
    scrubbed_list = scrub_secrets(raw_list)
    assert "sessionid=12345" not in scrubbed_list
    assert "[REDACTED]" in scrubbed_list



# =============================================================================
# 3. Environment & Architectural Isolation Tests
# =============================================================================


def test_11_main_environment_does_not_import_f2() -> None:
    """Test 11: Production downloader modules NEVER import f2 in main environment."""
    import src.downloader.contracts
    import src.downloader.safe_downloader
    import src.downloader.stubs

    # Assert f2 is not loaded in sys.modules
    assert "f2" not in sys.modules, "Security violation: f2 was imported into the main process!"


def test_12_legacy_adapter_emits_deprecation_warning() -> None:
    """Test 12: Instantiating legacy F2DouyinDownloader emits DeprecationWarning."""
    with pytest.deprecated_call():
        F2DouyinDownloader()


# =============================================================================
# 4. Orchestration Shell Happy Path & Idempotency Tests
# =============================================================================


def test_13_orchestrator_successful_execution_full_stages(sample_task: DownloadTask) -> None:
    """Test 13: Full 8-stage state machine succeeds and produces valid contract."""
    backend = FakeDownloadBackend(success=True, files_to_create=("test_video.mp4",))
    validator = FakeMediaValidator(should_pass=True)
    normalizer = FakeAssetNormalizer()
    promoter = FakeArchivePromoter()
    router = FakeContentRouter()

    downloader = SafeDouyinDownloader(
        backend=backend,
        validator=validator,
        normalizer=normalizer,
        promoter=promoter,
        router=router,
    )

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.SUCCESS
    assert result.stage == ExecutionStage.SUCCESS
    assert result.error_code is None
    assert result.retryable is False
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.file_name == f"{sample_task.platform_content_id}.mp4"
    assert asset.sha256 is not None
    assert result.validation["passed"] is True
    assert result.elapsed_sec >= 0.0


def test_14_orchestrator_idempotent_bypass_skipped(sample_task: DownloadTask) -> None:
    """Test 14: Existing valid canonical asset bypasses download and returns SKIPPED."""
    existing = [
        DownloadedAsset(
            file_name=f"{sample_task.platform_content_id}.mp4",
            relative_path=f"{sample_task.platform_content_id}.mp4",
            size_bytes=2048,
            content_type="video",
            sha256="existing_hash",
        )
    ]
    inspector = FakeAssetStateInspector(return_valid=True, existing_assets=existing)
    backend = FakeDownloadBackend()

    downloader = SafeDouyinDownloader(
        backend=backend,
        asset_inspector=inspector,
    )

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.SKIPPED
    assert result.stage == ExecutionStage.SUCCESS
    assert len(backend.calls) == 0, "Backend must NOT be called on idempotent bypass!"
    assert result.assets == existing
    assert result.validation.get("cached") is True


# =============================================================================
# 5. Stage 1 (RECEIVED) Validation Tests
# =============================================================================


def test_15_invalid_platform_fails_at_received(sample_task: DownloadTask) -> None:
    """Test 15: Non-douyin platform fails at RECEIVED with DOWNLOAD_INVALID_INPUT."""
    invalid_task = DownloadTask(
        task_id="dl_kuaishou_123",
        platform="kuaishou",
        scope_id="kuaishou:test",
        platform_content_id="12345",
        content_type="video",
        source_url="https://kuaishou.com/12345",
    )
    downloader = SafeDouyinDownloader()
    result = downloader.execute(invalid_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.RECEIVED
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_INVALID_INPUT.value
    assert result.retryable is False


def test_16_invalid_source_url_fails_at_received(sample_task: DownloadTask) -> None:
    """Test 16: Illegal URL domain fails at RECEIVED with DOWNLOAD_INVALID_INPUT."""
    invalid_task = DownloadTask(
        task_id="dl_douyin_test",
        platform="douyin",
        scope_id="douyin:test",
        platform_content_id="12345",
        content_type="video",
        source_url="https://malicious-site.com/video/12345",
    )
    downloader = SafeDouyinDownloader()
    result = downloader.execute(invalid_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.RECEIVED
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_INVALID_INPUT.value


def test_17_invalid_content_id_fails_at_received() -> None:
    """Test 17: Non-alphanumeric or empty content ID fails at RECEIVED."""
    invalid_task = DownloadTask(
        task_id="dl_douyin_test",
        platform="douyin",
        scope_id="douyin:test",
        platform_content_id="",
        content_type="video",
        source_url="https://www.douyin.com/video/123",
    )
    downloader = SafeDouyinDownloader()
    result = downloader.execute(invalid_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.RECEIVED
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_INVALID_INPUT.value


# =============================================================================
# 6. Stage 2 (PREFLIGHT) Auth Tests
# =============================================================================


def test_18_preflight_auth_required_fails_when_missing_credentials(sample_task: DownloadTask) -> None:
    """Test 18: Missing credentials fails at PREFLIGHT with DOWNLOAD_AUTH_REQUIRED when require_auth=True."""
    cred_provider = NullCredentialProvider(credentials={})
    downloader = SafeDouyinDownloader(credential_provider=cred_provider, require_auth=True)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.PREFLIGHT
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED.value
    assert result.retryable is False


def test_19_preflight_passes_when_credentials_available(sample_task: DownloadTask) -> None:
    """Test 19: Valid credentials pass PREFLIGHT and pass credentials to backend."""
    cred_provider = NullCredentialProvider(credentials={sample_task.scope_id: {"cookie": "valid_cookie"}})
    backend = FakeDownloadBackend()
    downloader = SafeDouyinDownloader(credential_provider=cred_provider, backend=backend, require_auth=True)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.SUCCESS
    assert len(backend.calls) == 1
    assert backend.calls[0]["credentials"] == {"cookie": "valid_cookie"}


# =============================================================================
# 7. Stage 3 & 4 (SANDBOX & DOWNLOADING) Lifecycle & Failure Tests
# =============================================================================


def test_20_sandbox_cleaned_up_on_successful_download(sample_task: DownloadTask) -> None:
    """Test 20: Sandbox directory is cleaned up upon execution completion."""
    sandbox_provider = DefaultTaskSandboxProvider()
    downloader = SafeDouyinDownloader(sandbox_provider=sandbox_provider)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.SUCCESS
    assert len(sandbox_provider.created_sandboxes) == 1
    sb = sandbox_provider.created_sandboxes[0]
    assert sb.is_cleaned_up is True
    assert not sb.path.exists(), "Sandbox directory must be deleted on completion!"


def test_21_sandbox_cleaned_up_on_download_failure(sample_task: DownloadTask) -> None:
    """Test 21: Sandbox directory is cleaned up even if download fails."""
    sandbox_provider = DefaultTaskSandboxProvider()
    backend = FakeDownloadBackend(success=False, error_message="Network connection reset")
    downloader = SafeDouyinDownloader(sandbox_provider=sandbox_provider, backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert len(sandbox_provider.created_sandboxes) == 1
    sb = sandbox_provider.created_sandboxes[0]
    assert sb.is_cleaned_up is True
    assert not sb.path.exists()


def test_22_backend_failure_classified_and_scrubbed(sample_task: DownloadTask) -> None:
    """Test 22: Backend error is classified into taxonomy and scrubbed of secrets."""
    backend = FakeDownloadBackend(
        success=False,
        error_message="HTTP 502 server error with msToken=secret_123_abc",
        exit_code=1,
    )
    downloader = SafeDouyinDownloader(backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_SERVER_ERROR.value
    assert result.retryable is True
    assert "secret_123_abc" not in result.message
    assert "msToken=[REDACTED]" in result.message


def test_23_backend_success_with_zero_files_fails(sample_task: DownloadTask) -> None:
    """Test 23: Backend reporting success with 0 files fails with DOWNLOAD_TOOL_ERROR."""
    backend = FakeDownloadBackend(success=True, files_to_create=())
    downloader = SafeDouyinDownloader(backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value
    assert result.retryable is True


def test_24_backend_timeout_classified(sample_task: DownloadTask) -> None:
    """Test 24: Backend timeout error classified as DOWNLOAD_TIMEOUT."""
    backend = FakeDownloadBackend(success=False, error_message="Command timed out after 60 seconds")
    downloader = SafeDouyinDownloader(backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_TIMEOUT.value
    assert result.retryable is True
    assert result.retry_after == 5


def test_25_backend_rate_limit_classified(sample_task: DownloadTask) -> None:
    """Test 25: 429 Too Many Requests classified with retry_after=300."""
    backend = FakeDownloadBackend(success=False, error_message="HTTP 429 Too Many Requests: rate limit exceeded")
    downloader = SafeDouyinDownloader(backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_RATE_LIMITED.value
    assert result.retryable is True
    assert result.retry_after == 300


# =============================================================================
# 8. Stage 5 & 6 (NORMALIZING & VALIDATING) Failure Tests
# =============================================================================


def test_26_normalizer_empty_fails_with_tool_error(sample_task: DownloadTask) -> None:
    """Test 26: Empty normalization output fails at NORMALIZING."""
    class EmptyNormalizer(AssetNormalizer):
        def normalize(self, raw_assets: list[Path], platform_content_id: str, content_type: str, metadata_hint: dict[str, Any]) -> list[NormalizedAsset]:
            return []

    downloader = SafeDouyinDownloader(normalizer=EmptyNormalizer())
    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.NORMALIZING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value


def test_27_validator_failure_returns_validation_failed(sample_task: DownloadTask) -> None:
    """Test 27: Media corruption causes DOWNLOAD_VALIDATION_FAILED at VALIDATING."""
    validator = FakeMediaValidator(
        should_pass=False,
        error_message="Container corrupt: missing video stream and sessionid=secret_cookie_leak",
    )
    downloader = SafeDouyinDownloader(validator=validator)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.VALIDATING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value
    assert result.retryable is True
    assert result.retry_after == 2
    assert "secret_cookie_leak" not in result.message
    assert "sessionid=[REDACTED]" in result.message
    assert result.validation["passed"] is False


def test_28_archive_promotion_does_not_occur_on_validation_failure(sample_task: DownloadTask) -> None:
    """Test 28 (Archive Safety Invariant): Corrupt files are NEVER promoted to canonical archive."""
    validator = FakeMediaValidator(should_pass=False)
    promoter = FakeArchivePromoter()
    downloader = SafeDouyinDownloader(validator=validator, promoter=promoter)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert len(result.assets) == 0, "No assets should be returned when validation fails!"


# =============================================================================
# 9. Stage 7 & General Exception Handling Tests
# =============================================================================


def test_29_unexpected_exception_caught_and_scrubbed(sample_task: DownloadTask) -> None:
    """Test 29: Unhandled backend exception is caught, scrubbed, and classified."""
    backend = FakeDownloadBackend(simulate_exception=RuntimeError("Completely anomalous fault with msToken=secret_token_123"))
    downloader = SafeDouyinDownloader(backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert result.stage == ExecutionStage.DOWNLOADING
    assert result.error_code == DownloaderErrorCode.DOWNLOAD_UNKNOWN.value
    assert "secret_token_123" not in result.message
    assert "msToken=[REDACTED]" in result.message


def test_30_error_policy_15_codes_taxonomy_coverage() -> None:
    """Test 30: DefaultDownloadErrorPolicy maps all 15 error scenarios correctly."""
    policy = DefaultDownloadErrorPolicy()

    scenarios = [
        ("malformed url input", DownloaderErrorCode.DOWNLOAD_INVALID_INPUT, False),
        ("live stream unsupported", DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT, False),
        ("socket connection reset", DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR, True),
        ("read timed out", DownloaderErrorCode.DOWNLOAD_TIMEOUT, True),
        ("video 404 not found", DownloaderErrorCode.DOWNLOAD_NOT_FOUND, False),
        ("private video 403 forbidden", DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED, False),
        ("video unavailable deleted by author", DownloaderErrorCode.DOWNLOAD_UNAVAILABLE_DELETED, False),
        ("rate limit 429 reached", DownloaderErrorCode.DOWNLOAD_RATE_LIMITED, True),
        ("HTTP 500 internal server error", DownloaderErrorCode.DOWNLOAD_SERVER_ERROR, True),
        ("login required session expired", DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED, False),
        ("captcha slider challenge", DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE, False),
        ("credential bridge keyring failure", DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED, True),
        ("ffprobe validation corrupt stream", DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED, True),
        ("f2 tool error crash", DownloaderErrorCode.DOWNLOAD_TOOL_ERROR, True),
        ("some strange unknown glitch", DownloaderErrorCode.DOWNLOAD_UNKNOWN, False),
    ]

    for err_text, expected_code, expected_retryable in scenarios:
        classification = policy.classify_error(err_text)
        assert classification.error_code == expected_code, f"Failed for '{err_text}'"
        assert classification.retryable == expected_retryable, f"Failed retryable for '{err_text}'"


def test_31_sandbox_ownership_delegation_preserves_on_failure(sample_task: DownloadTask) -> None:
    """Test 31 (Section E): SafeDouyinDownloader delegates finalization; provider retention policy controls physical deletion."""
    sandbox_provider = DefaultTaskSandboxProvider(preserve_on_failure=True)
    backend = FakeDownloadBackend(success=False, error_message="Network error: connection refused")
    downloader = SafeDouyinDownloader(sandbox_provider=sandbox_provider, backend=backend)

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.FAILED
    assert len(sandbox_provider.created_sandboxes) == 1
    sb = sandbox_provider.created_sandboxes[0]
    # Orchestrator called finalize_failure and cleanup()
    assert sb.was_failed is True
    assert sb.is_cleaned_up is True
    # Because preserve_on_failure is True, provider preserved the physical directory for diagnostics!
    assert sb.path.exists(), "Physical sandbox must be preserved for post-mortem diagnostics per provider retention policy!"

    # Clean up test artifact
    import shutil
    shutil.rmtree(sb.path, ignore_errors=True)

