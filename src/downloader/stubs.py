"""Test fakes and default protocol implementations for Downloader Subsystem (DY-D01).

These stubs allow:
1. Full end-to-end execution of SafeDouyinDownloader without real network, browser, or external tools.
2. Complete test coverage of stage state transitions, retry policies, and error classifications.
3. Verification that NO external libraries (such as f2) are required in the main environment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

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
    DownloadErrorPolicy,
    ErrorClassification,
    MediaValidator,
    NormalizedAsset,
    TaskSandbox,
    TaskSandboxProvider,
    ValidationResult,
)


class NullCredentialProvider(CredentialProvider):
    """Provides empty credentials (unauthenticated or public access)."""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self._credentials = credentials or {}

    def get_credentials(self, scope_id: str) -> dict[str, str] | None:
        return self._credentials.get(scope_id) if scope_id in self._credentials else None


class InMemoryTaskSandbox(TaskSandbox):
    """Real filesystem temporary directory for sandboxing with tracked cleanup."""

    def __init__(
        self,
        base_dir: Path | None = None,
        task_id: str = "test",
        preserve_on_failure: bool = False,
    ) -> None:
        self._root = Path(tempfile.mkdtemp(prefix=f"agy_sandbox_{task_id}_", dir=base_dir))
        self._work_dir = self._root / "work"
        self._output_dir = self._root / "output"
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._cleaned_up = False
        self.preserve_on_failure = preserve_on_failure
        self.was_failed = False
        self.was_succeeded = False

    @property
    def path(self) -> Path:
        return self._root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def is_cleaned_up(self) -> bool:
        return self._cleaned_up

    def register_artifact(self, path: Path) -> Path:
        return path

    def list_artifacts(self) -> list[Path]:
        return [p for p in self._output_dir.rglob("*") if p.is_file()]

    def finalize_success(self) -> None:
        self.was_succeeded = True

    def finalize_failure(self, reason: str = "") -> None:
        self.was_failed = True

    def cleanup(self) -> None:
        if not self._cleaned_up:
            if self.preserve_on_failure and self.was_failed:
                # Retention policy: preserve on failure for post-mortem analysis
                self._cleaned_up = True
                return
            shutil.rmtree(self._root, ignore_errors=True)
            self._cleaned_up = True


class DefaultTaskSandboxProvider(TaskSandboxProvider):
    """Factory creating isolated TaskSandbox instances."""

    def __init__(self, base_dir: Path | None = None, preserve_on_failure: bool = False) -> None:
        self._base_dir = base_dir
        self.preserve_on_failure = preserve_on_failure
        self.created_sandboxes: list[InMemoryTaskSandbox] = []

    def create_sandbox(self, task: Any) -> TaskSandbox:
        tid = task.task_id if hasattr(task, "task_id") else str(task)
        sb = InMemoryTaskSandbox(
            base_dir=self._base_dir,
            task_id=tid,
            preserve_on_failure=self.preserve_on_failure,
        )
        self.created_sandboxes.append(sb)
        return sb

    def scan_orphans(self) -> list[Any]:
        return []

    def gc_orphans(self, max_age_seconds: float = 3600.0) -> Any:
        return {"scanned": 0, "cleaned": 0}


class FakeDownloadBackend(DownloadBackend):
    """Configurable fake download backend for testing all execution paths."""

    def __init__(
        self,
        success: bool = True,
        files_to_create: tuple[str, ...] = ("video.mp4",),
        file_content: bytes = b"DUMMY_MP4_CONTENT_1234567890",
        exit_code: int = 0,
        error_message: str | None = None,
        simulate_exception: Exception | None = None,
    ) -> None:
        self.success = success
        self.files_to_create = files_to_create
        self.file_content = file_content
        self.exit_code = exit_code
        self.error_message = error_message
        self.simulate_exception = simulate_exception
        self.calls: list[dict[str, Any]] = []

    def execute_download(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> BackendDownloadResult:
        self.calls.append({
            "source_url": source_url,
            "sandbox_dir": sandbox_dir,
            "download_input": download_input,
            "credentials": credentials,
        })

        if self.simulate_exception is not None:
            raise self.simulate_exception

        created: list[Path] = []
        if self.success:
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            for fname in self.files_to_create:
                fpath = sandbox_dir / fname
                fpath.write_bytes(self.file_content)
                created.append(fpath)

        return BackendDownloadResult(
            success=self.success,
            raw_files=tuple(created),
            error_message=self.error_message,
            exit_code=self.exit_code,
            raw_diagnostics={"call_count": len(self.calls)},
        )


class FakeMediaValidator(MediaValidator):
    """Configurable media validator for testing integrity passes and failures."""

    def __init__(
        self,
        should_pass: bool = True,
        error_message: str | None = None,
        streams: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        self.should_pass = should_pass
        self.error_message = error_message
        self.streams = streams or (
            {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
            {"codec_type": "audio", "codec_name": "aac"},
        )
        self.validated_paths: list[list[Path]] = []

    def validate_assets(self, asset_paths: list[Path]) -> ValidationResult:
        self.validated_paths.append(asset_paths)
        if not self.should_pass:
            return ValidationResult(
                passed=False,
                ffprobe_verified=False,
                decode_smoke_verified=False,
                streams=(),
                error=self.error_message or "Media validation failed: corrupt container or missing video stream",
            )
        return ValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            streams=self.streams,
            error=None,
        )


class FakeAssetNormalizer(AssetNormalizer):
    """Standardizes filenames and assigns media types."""

    def normalize(
        self,
        raw_assets: list[Path],
        platform_content_id: str,
        content_type: str,
        metadata_hint: dict[str, Any],
    ) -> list[NormalizedAsset]:
        results: list[NormalizedAsset] = []
        for p in raw_assets:
            # Deterministic naming: <content_id>.<ext>
            ext = p.suffix if p.suffix else ".mp4"
            norm_name = f"{platform_content_id}{ext}"
            results.append(
                NormalizedAsset(
                    file_path=p,
                    content_type=content_type or "video",
                    file_name=norm_name,
                    metadata={"original_name": p.name, **metadata_hint},
                )
            )
        return results


class FakeArchivePromoter(ArchivePromoter):
    """Promotes normalized assets into target directory with SHA-256 computation."""

    def __init__(self, archive_root: Path | None = None) -> None:
        self.archive_root = archive_root or Path("archive")

    def resolve_canonical_destination(self, platform: str, platform_content_id: str) -> Path:
        return self.archive_root / platform / platform_content_id

    def promote(
        self,
        normalized_assets: list[NormalizedAsset],
        target_directory: Path | None = None,
    ) -> list[DownloadedAsset]:
        target_dir = target_directory or self.resolve_canonical_destination("douyin", "default")
        target_dir.mkdir(parents=True, exist_ok=True)
        promoted: list[DownloadedAsset] = []
        for na in normalized_assets:
            dest = target_dir / na.file_name
            # Atomic promotion simulation via rename or copy
            if na.file_path.exists():
                shutil.copy2(na.file_path, dest)
            else:
                dest.write_bytes(b"")

            data = dest.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            rel_path = dest.name

            promoted.append(
                DownloadedAsset(
                    file_name=dest.name,
                    relative_path=rel_path,
                    size_bytes=len(data),
                    content_type=na.content_type,
                    sha256=digest,
                    width=na.metadata.get("width", 1080),
                    height=na.metadata.get("height", 1920),
                    duration_sec=na.metadata.get("duration_sec", 15.0),
                )
            )
        return promoted


class DefaultDownloadErrorPolicy(DownloadErrorPolicy):
    """Default classification policy mapping errors to the 15-code taxonomy."""

    def classify_error(self, error: Exception | str) -> ErrorClassification:
        msg = str(error).lower()

        # Category 1: Input Validation
        if "invalid_input" in msg or "malformed" in msg or "illegal domain" in msg or "id mismatch" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_INVALID_INPUT, retryable=False, user_action="Fix caller input")
        if "unsupported" in msg or "live stream" in msg or "vip" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT, retryable=False, user_action="Pipeline schema update")

        # Category 4: Auth & Security (check early)
        if "auth_required" in msg or "login" in msg or "session expired" in msg or "unauthenticated" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED, retryable=False, user_action="Operator Re-login")
        if "captcha" in msg or "challenge" in msg or "slider" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE, retryable=False, user_action="Operator Solve Captcha")
        if "keyring" in msg or "credential bridge" in msg or "credential_bridge" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED, retryable=True, retry_after=1, user_action="None (Auto-heals)")

        # Category 3: Platform & Remote
        if "deleted" in msg or "taken down" in msg or "unavailable_deleted" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_UNAVAILABLE_DELETED, retryable=False, user_action="Mark terminal")
        if "404" in msg or "not found" in msg or "notice_code == 2048" in msg or "empty detail" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_NOT_FOUND, retryable=False, user_action="Mark terminal")
        if "403" in msg or "permission denied" in msg or "private" in msg or "forbidden" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED, retryable=False, user_action="Inspect account ACL")
        if "429" in msg or "rate limit" in msg or "too many requests" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_RATE_LIMITED, retryable=True, retry_after=300, user_action="Optional pause")
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg or "server error" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_SERVER_ERROR, retryable=True, retry_after=2, user_action="None (Auto-heals)")

        # Category 2: Network & Infra
        if "timeout" in msg or "timed out" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_TIMEOUT, retryable=True, retry_after=5, user_action="None (Auto-heals)")
        if "connection" in msg or "network" in msg or "socket" in msg or "dns" in msg or "ssl" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR, retryable=True, retry_after=2, user_action="None (Auto-heals)")

        # Category 5: Tool & Pipeline
        if "validation" in msg or "corrupt" in msg or "ffprobe" in msg or "decode" in msg or "promotion" in msg or "archive" in msg or "budget" in msg or "collision" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED, retryable=True, retry_after=2, user_action="Re-resolve CDN URL & retry once")
        if "f2" in msg or "tool error" in msg or "apiresponseerror" in msg or "crash" in msg or "zero files" in msg:
            return ErrorClassification(DownloaderErrorCode.DOWNLOAD_TOOL_ERROR, retryable=True, retry_after=2, user_action="None / Monitor")

        # Fallback
        return ErrorClassification(DownloaderErrorCode.DOWNLOAD_UNKNOWN, retryable=False, user_action="Developer Post-mortem")


class FakeContentRouter(ContentRouter):
    """Resolves local archive directory under a base folder."""

    def __init__(self, base_archive_dir: Path | None = None) -> None:
        self.base_dir = base_archive_dir or Path(tempfile.gettempdir()) / "agy_archive"

    def resolve_target_directory(
        self,
        platform: str,
        scope_id: str,
        platform_content_id: str,
    ) -> Path:
        return self.base_dir / platform / platform_content_id

    def plan_execution(self, task: Any, execution_id: str | None = None) -> Any:
        from src.downloader.router import ProductionContentRouter
        return ProductionContentRouter().plan_execution(task, execution_id)

    def validate_artifact_conformity(self, plan: Any, candidates: Sequence[Any]) -> Any:
        from src.downloader.router import ProductionContentRouter
        return ProductionContentRouter().validate_artifact_conformity(plan, candidates)


class FakeAssetStateInspector(AssetStateInspector):
    """Configurable inspector for idempotency checks."""

    def __init__(self, return_valid: bool = False, existing_assets: list[DownloadedAsset] | None = None) -> None:
        self.return_valid = return_valid
        self.existing_assets = existing_assets or []

    def inspect_assets(
        self,
        target_directory: Path,
        platform_content_id: str,
    ) -> tuple[bool, list[DownloadedAsset]]:
        return self.return_valid, self.existing_assets
