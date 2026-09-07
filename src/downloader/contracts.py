"""Downloader Subsystem Contracts and Protocols (DY-D01).

Implements:
1. DownloadResultContract v1 (strictly compliant with QW-13 schema).
2. DownloaderStatus, ExecutionStage, DownloaderErrorCode (15 codes across 5 categories).
3. Secret scrubbing invariants (removing sessionid, sid_guard, msToken, a_bogus, tokens).
4. Dependency Injection Protocols for all downstream execution ports.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# =============================================================================
# 1. Enums and Status Codes
# =============================================================================


class DownloaderStatus(str, enum.Enum):
    """Execution status for DownloadResultContract v1."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ExecutionStage(str, enum.Enum):
    """Stage state machine progression for SafeDouyinDownloader."""

    RECEIVED = "RECEIVED"
    PREFLIGHT = "PREFLIGHT"
    SANDBOX_READY = "SANDBOX_READY"
    DOWNLOADING = "DOWNLOADING"
    NORMALIZING = "NORMALIZING"
    VALIDATING = "VALIDATING"
    PROMOTING = "PROMOTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class DownloaderErrorCode(str, enum.Enum):
    """Downloader Error Taxonomy v1 (15 discrete codes across 5 operational categories)."""

    # Category 1: Input Validation
    DOWNLOAD_INVALID_INPUT = "DOWNLOAD_INVALID_INPUT"
    DOWNLOAD_UNSUPPORTED_CONTENT = "DOWNLOAD_UNSUPPORTED_CONTENT"

    # Category 2: Network & Infra
    DOWNLOAD_NETWORK_ERROR = "DOWNLOAD_NETWORK_ERROR"
    DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"

    # Category 3: Platform & Remote
    DOWNLOAD_NOT_FOUND = "DOWNLOAD_NOT_FOUND"
    DOWNLOAD_PERMISSION_DENIED = "DOWNLOAD_PERMISSION_DENIED"
    DOWNLOAD_UNAVAILABLE_DELETED = "DOWNLOAD_UNAVAILABLE_DELETED"
    DOWNLOAD_RATE_LIMITED = "DOWNLOAD_RATE_LIMITED"
    DOWNLOAD_SERVER_ERROR = "DOWNLOAD_SERVER_ERROR"

    # Category 4: Auth & Security
    DOWNLOAD_AUTH_REQUIRED = "DOWNLOAD_AUTH_REQUIRED"
    DOWNLOAD_AUTH_CHALLENGE = "DOWNLOAD_AUTH_CHALLENGE"
    DOWNLOAD_CREDENTIAL_BRIDGE_FAILED = "DOWNLOAD_CREDENTIAL_BRIDGE_FAILED"

    # Category 5: Tool & Pipeline
    DOWNLOAD_MEDIA_INCOMPLETE = "DOWNLOAD_MEDIA_INCOMPLETE"
    DOWNLOAD_VALIDATION_FAILED = "DOWNLOAD_VALIDATION_FAILED"
    DOWNLOAD_TOOL_ERROR = "DOWNLOAD_TOOL_ERROR"
    DOWNLOAD_UNKNOWN = "DOWNLOAD_UNKNOWN"
    SANDBOX_CREATE_FAILED = "SANDBOX_CREATE_FAILED"
    SANDBOX_PATH_ESCAPE = "SANDBOX_PATH_ESCAPE"
    SANDBOX_METADATA_CORRUPT = "SANDBOX_METADATA_CORRUPT"
    SANDBOX_CLEANUP_FAILED = "SANDBOX_CLEANUP_FAILED"


# =============================================================================
# 2. Secret Redaction / Scrubbing
# =============================================================================

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(sessionid(?:_ss)?=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sid_guard=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sid_tt=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(msToken=)[a-zA-Z0-9%_\./+-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(a_bogus=)[a-zA-Z0-9%_\./+-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(passport_csrf_token(?:_default)?=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9%_\.\-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:bearer|token|access_token)=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(--cookie\s+[\"']?)[^\"'\s]+([\"']?)", re.IGNORECASE), r"\1[REDACTED]\2"),
    (re.compile(r"(['\"]--cookie['\"],\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED]\2"),
]


def scrub_secrets(text: str) -> str:
    """Scrub sensitive credentials and tokens from text using regex substitution."""
    if not text:
        return ""
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# =============================================================================
# 3. Data Contracts
# =============================================================================


@dataclass(frozen=True)
class DownloadedAsset:
    """Individual downloaded and promoted media asset."""

    file_name: str
    relative_path: str
    size_bytes: int
    content_type: str  # "video", "image_album", "audio", "cover"
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    """Summary of media integrity and container checks."""

    passed: bool
    ffprobe_verified: bool = False
    decode_smoke_verified: bool = False
    streams: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    validated_sha256: str | None = None
    validated_artifacts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "ffprobe_verified": self.ffprobe_verified,
            "decode_smoke_verified": self.decode_smoke_verified,
            "streams": list(self.streams),
            "error": scrub_secrets(self.error) if self.error else None,
            "validated_sha256": self.validated_sha256,
            "validated_artifacts": list(self.validated_artifacts),
        }


@dataclass
class DownloadResultContract:
    """Uniform Downloader Result Contract v1 (QW-13 compliant).

    Guarantees:
    - 100% JSON-serializable.
    - Secret-free: message and validation errors are scrubbed of credentials.
    - Strict schema matching QW-13 formal specification.
    """

    source_url: str
    platform_content_id: str
    status: DownloaderStatus
    error_code: str | None = None
    retryable: bool = False
    retry_after: int | None = None
    message: str = ""
    assets: list[DownloadedAsset] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    diagnostics_ref: str | None = None
    elapsed_sec: float = 0.0
    schema_version: str = "download-result-v1"
    task_id: str | None = None
    scope_id: str | None = None
    stage: ExecutionStage = ExecutionStage.SUCCESS

    def __post_init__(self) -> None:
        if self.message:
            self.message = scrub_secrets(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_url": self.source_url,
            "platform_content_id": self.platform_content_id,
            "task_id": self.task_id,
            "scope_id": self.scope_id,
            "status": self.status.value if isinstance(self.status, DownloaderStatus) else str(self.status),
            "error_code": self.error_code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "message": scrub_secrets(self.message),
            "assets": [a.to_dict() for a in self.assets],
            "validation": self.validation,
            "diagnostics_ref": self.diagnostics_ref,
            "elapsed_sec": round(self.elapsed_sec, 4),
            "stage": self.stage.value if isinstance(self.stage, ExecutionStage) else str(self.stage),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# =============================================================================
# 4. Stage & Subsystem Model Contracts
# =============================================================================


@dataclass(frozen=True)
class BackendDownloadResult:
    """Output returned by the download backend driver."""

    success: bool
    raw_files: tuple[Path, ...] = ()
    error_message: str | None = None
    exit_code: int = 0
    raw_diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedAsset:
    """Normalized media asset inside sandbox before archive promotion."""

    file_path: Path
    content_type: str
    file_name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorClassification:
    """Taxonomy categorization output for an exception or error message."""

    error_code: DownloaderErrorCode
    retryable: bool
    retry_after: int | None = None
    user_action: str = ""


# =============================================================================
# 5. Dependency Injection Protocols (Ports)
# =============================================================================


@runtime_checkable
class CredentialProvider(Protocol):
    """Port 1: Resolves sanitized runtime credentials for account scope."""

    def get_credentials(self, scope_id: str) -> dict[str, str] | None:
        """Returns credential mapping or None if unauthenticated."""
        ...


@runtime_checkable
class TaskSandbox(Protocol):
    """Scoped temporary execution directory for a single download task attempt."""

    @property
    def path(self) -> Path:
        """Root path of the sandbox directory."""
        ...

    @property
    def root(self) -> Path:
        """Alias for root path of the sandbox."""
        ...

    @property
    def work_dir(self) -> Path:
        """Working directory for downloads."""
        ...

    @property
    def output_dir(self) -> Path:
        """Output directory for generated media assets."""
        ...

    def register_artifact(self, path: Path) -> Path:
        """Registers a newly produced artifact within sandbox containment."""
        ...

    def list_artifacts(self) -> list[Path]:
        """Lists produced artifacts within current sandbox."""
        ...

    def finalize_success(self) -> None:
        """Marks execution state as SUCCESS according to retention policy."""
        ...

    def finalize_failure(self, reason: str = "") -> None:
        """Marks execution state as FAILED according to retention policy."""
        ...

    def cleanup(self) -> None:
        """Applies retention policy / directory deletion."""
        ...


@runtime_checkable
class TaskSandboxProvider(Protocol):
    """Port 2: Creates isolated sandbox environments and manages lifecycle & GC."""

    def create_sandbox(self, task: Any) -> TaskSandbox:
        """Creates and returns an isolated TaskSandbox for a task or task_id."""
        ...

    def scan_orphans(self) -> list[Any]:
        """Scans sandbox directory for orphaned workspaces."""
        ...

    def gc_orphans(self, max_age_seconds: float = ...) -> Any:
        """Cleans up eligible orphaned and expired sandboxes."""
        ...


@runtime_checkable
class DownloadBackend(Protocol):
    """Port 3: Underlying execution driver (e.g. F2 subprocess, curl, direct HTTP)."""

    def execute_download(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> BackendDownloadResult:
        """Executes raw download into sandbox_dir."""
        ...


@runtime_checkable
class MediaValidator(Protocol):
    """Port 4: Validates container integrity and streams of media files."""

    def validate_assets(self, asset_paths: list[Path]) -> ValidationResult:
        """Runs ffprobe/integrity validation on downloaded media files."""
        ...


@runtime_checkable
class AssetNormalizer(Protocol):
    """Port 5: Normalizes media filenames and metadata sidecars in sandbox."""

    def normalize(
        self,
        raw_assets: list[Path],
        platform_content_id: str,
        content_type: str,
        metadata_hint: dict[str, Any],
    ) -> list[NormalizedAsset]:
        """Returns sanitized and structured NormalizedAsset list."""
        ...


@runtime_checkable
class ArchivePromoter(Protocol):
    """Port 6: Atomically promotes assets from sandbox to canonical storage."""

    def promote(
        self,
        normalized_assets: list[NormalizedAsset],
        target_directory: Path | None = None,
        **kwargs: Any,
    ) -> list[DownloadedAsset]:
        """Atomically moves assets to target_directory and computes hashes."""
        ...

    def resolve_canonical_destination(
        self,
        platform: str,
        platform_content_id: str,
    ) -> Path:
        """Returns the canonical archive destination path for formal asset publication."""
        ...


@runtime_checkable
class DownloadErrorPolicy(Protocol):
    """Port 7: Classifies errors into DownloaderErrorCode and retry policy."""

    def classify_error(self, error: Exception | str) -> ErrorClassification:
        """Maps an error to its taxonomy classification and retry parameters."""
        ...


@runtime_checkable
class ContentRouter(Protocol):
    """Port 8: Pure content routing engine mapping DownloadTasks to ExecutionPlans."""

    def plan_execution(
        self,
        task: Any,
        execution_id: str | None = None,
    ) -> Any:
        """Generates deterministic execution plan for a download task."""
        ...

    def validate_artifact_conformity(
        self,
        plan: Any,
        candidates: Sequence[Any],
    ) -> Any:
        """Validates that candidate artifacts conform to the route execution plan."""
        ...


@runtime_checkable
class AssetStateInspector(Protocol):
    """Port 9: Checks if canonical storage already contains complete valid assets."""

    def inspect_assets(
        self,
        target_directory: Path,
        platform_content_id: str,
    ) -> tuple[bool, list[DownloadedAsset]]:
        """Returns (is_valid, existing_assets). If valid, task may be SKIPPED."""
        ...
