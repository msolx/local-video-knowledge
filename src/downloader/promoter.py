"""DY-D07: Production Atomic Archive Promotion Engine.

Responsible for the final writing barrier in the download safety pipeline:
D04 Sandbox -> D06 Normalize -> D05 Validate -> D07 Atomic Promote -> Canonical Local Asset.

Guarantees:
1. Sandbox Ownership Isolation: Source sandbox files are strictly READ-ONLY INPUT.
   D07 never unlinks, moves, or cleans up sandbox files. D04 owns sandbox lifecycle.
2. Validation-Time Content Binding: Cryptographically asserts that sandbox artifact bytes
   at promotion time exactly match the SHA-256 and byte size verified by D05.
3. Unified Archive-Local Staging: Assets and manifest are staged in an archive-local
   hidden staging folder (.staging) on the target volume before publication.
4. Atomic Directory Publication: Promotes the entire content directory in a single atomic
   directory rename (os.replace). Assets either do not exist or are fully complete.
5. Idempotent Ingest vs Conflict Defense: Byte-identical existing directories return
   IDEMPOTENT_EXISTING; conflicting contents or incomplete assets raise ArchiveConflictError.
6. Formal Asset Commit Marker: A content asset set is a formal local asset iff
   both the final directory and a verified asset_manifest.json exist.
7. Durability Caveat: flush + fsync requests filesystem/OS durability before final
   publication (durability semantics depend on underlying filesystem/OS storage stack).
8. Safe Staging GC: Staging directories record owner PID and state; active processes
   and uncertain owners are never deleted.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Sequence
from uuid import uuid4

from src.downloader.contracts import (
    ArchivePromoter,
    DownloadedAsset,
    DownloaderErrorCode,
    NormalizedAsset,
    ValidationResult,
    scrub_secrets,
)
from src.downloader.normalizer import (
    ArtifactRole,
    NormalizedArtifact,
    sanitize_filename_component,
)

logger = logging.getLogger(__name__)

# =============================================================================
# 1. Constants & Defaults
# =============================================================================

DEFAULT_MAX_PATH_BUDGET: int = 240
DEFAULT_MAX_COMPONENT_BUDGET: int = 64
MANIFEST_FILENAME: str = "asset_manifest.json"
STAGING_DIR_NAME: str = ".staging"
FORBIDDEN_EXTENSIONS: frozenset[str] = frozenset(
    {".part", ".tmp", ".crdownload", ".download", ".incomplete"}
)


class PromotionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    IDEMPOTENT_EXISTING = "IDEMPOTENT_EXISTING"


# =============================================================================
# 2. Error Taxonomy & Exceptions
# =============================================================================


class PromotionError(Exception):
    """Base error for archive promotion failures."""

    def __init__(
        self,
        message: str,
        reason: str = "PROMOTION_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class PromotionPreconditionError(PromotionError):
    """Raised when validation is missing, failed, or preconditions are unmet."""


class ArchiveBudgetExceededError(PromotionError):
    """Raised when target archive path exceeds MAX_PATH budget."""


class ArchiveConflictError(PromotionError):
    """Raised when destination file exists with different content/SHA-256."""


class PromotionCorruptionError(PromotionError):
    """Raised when destination file SHA-256 does not match source SHA-256 after transfer."""


class PromotionContainmentError(PromotionError):
    """Raised when target or source path escapes allowed root directories."""


class InvalidPromotionArtifactError(PromotionError):
    """Raised when candidate artifact is temporary, non-regular, or corrupt."""


# =============================================================================
# 3. Independent Path Budget Shield
# =============================================================================


class FinalArchivePathBudget:
    """Independent path budget validator for canonical persistent archive storage.

    Enforces the application-level conservative path budget (default 240 characters)
    to protect against Win32 MAX_PATH compatibility boundaries on Windows NTFS / SMB.
    """

    def __init__(
        self,
        max_path_budget: int = DEFAULT_MAX_PATH_BUDGET,
        max_component_budget: int = DEFAULT_MAX_COMPONENT_BUDGET,
    ) -> None:
        self.max_path_budget = max_path_budget
        self.max_component_budget = max_component_budget

    def validate_path(self, path: Path, extra_buffer_len: int = 0) -> None:
        """Validates filename component and total absolute path length."""
        name = path.name
        if len(name) > self.max_component_budget:
            raise ArchiveBudgetExceededError(
                f"Archive filename component '{name}' ({len(name)} chars) exceeds component budget ({self.max_component_budget} chars).",
                reason="COMPONENT_BUDGET_EXCEEDED",
                details={"path": str(path), "component": name, "length": len(name), "budget": self.max_component_budget},
            )

        resolved_str = str(path.resolve())
        total_len = len(resolved_str) + extra_buffer_len
        if total_len > self.max_path_budget:
            raise ArchiveBudgetExceededError(
                f"Archive path '{path}' ({total_len} chars with buffer) exceeds MAX_PATH budget ({self.max_path_budget} chars).",
                reason="ARCHIVE_PATH_BUDGET_EXCEEDED",
                details={"path": str(path), "total_length": total_len, "budget": self.max_path_budget},
            )


# =============================================================================
# 4. Data Contracts & Domain Models
# =============================================================================


@dataclass(frozen=True)
class PromotedAsset:
    """Immutable record of an individual promoted canonical asset."""

    file_name: str
    relative_archive_path: str  # e.g. "douyin/7671141177986518318/7671141177986518318.mp4"
    absolute_archive_path: Path
    byte_size: int
    sha256: str
    role: str
    content_type: str
    sequence_index: int | None = None
    media_summary: dict[str, Any] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    source_original_name: str = ""

    def to_downloaded_asset(self) -> DownloadedAsset:
        """Projects PromotedAsset into standard DownloadedAsset contract."""
        return DownloadedAsset(
            file_name=self.file_name,
            relative_path=self.relative_archive_path,
            size_bytes=self.byte_size,
            content_type=self.content_type,
            sha256=self.sha256,
            width=self.media_summary.get("width"),
            height=self.media_summary.get("height"),
            duration_sec=self.media_summary.get("duration_sec"),
        )


@dataclass(frozen=True)
class PromotionResult:
    """Result of an atomic archive promotion transaction.

    Emulates a sequence of DownloadedAsset to provide drop-in compatibility
    with callers expecting list[DownloadedAsset].
    """

    platform: str
    platform_content_id: str
    target_directory: Path
    manifest_path: Path
    promoted_assets: list[PromotedAsset]
    status: PromotionStatus = PromotionStatus.SUCCESS
    idempotent_existing: bool = False
    warnings: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter([pa.to_downloaded_asset() for pa in self.promoted_assets])

    def __len__(self) -> int:
        return len(self.promoted_assets)

    def __getitem__(self, idx: int) -> DownloadedAsset:
        return [pa.to_downloaded_asset() for pa in self.promoted_assets][idx]

    def __bool__(self) -> bool:
        return self.status in (PromotionStatus.SUCCESS, PromotionStatus.IDEMPOTENT_EXISTING) and len(self.promoted_assets) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "target_directory": str(self.target_directory),
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "status": self.status.value,
            "idempotent_existing": self.idempotent_existing,
            "asset_count": len(self.promoted_assets),
            "promoted_assets": [asdict(pa) for pa in self.promoted_assets],
            "warnings": [scrub_secrets(w) for w in self.warnings],
        }


@dataclass(frozen=True)
class ArchivedAssetVerificationResult:
    """Outcome of formal local asset verification via manifest commitment marker."""

    valid: bool
    error: str | None = None
    manifest: dict[str, Any] | None = None
    assets: tuple[DownloadedAsset, ...] = ()


class StagingGcSummary(int):
    """Result of staging garbage collection. Behaves as int (cleaned count) with dict attributes."""

    def __new__(cls, cleaned: int, scanned: int = 0, kept: int = 0):
        obj = super().__new__(cls, cleaned)
        obj.cleaned = cleaned
        obj.scanned = scanned
        obj.kept = kept
        return obj

    def to_dict(self) -> dict[str, int]:
        return {"scanned": self.scanned, "cleaned": self.cleaned, "kept": self.kept}


# =============================================================================
# 5. Helper Utilities
# =============================================================================


def compute_file_sha256(path: Path) -> str:
    """Computes SHA-256 digest of a local file in 128 KiB chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(128 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def safe_copy_and_sync(source: Path, destination: Path) -> None:
    """Copies source file to destination in 128 KiB chunks, flushing and fsyncing.

    Note on durability: flush + fsync requests filesystem/OS durability before final
    publication. Absolute power-loss durability depends on the underlying filesystem
    and OS storage stack.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as fsrc, open(destination, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=128 * 1024)
        fdst.flush()
        try:
            os.fsync(fdst.fileno())
        except OSError:
            # Some virtual or network filesystems don't support fsync on file descriptors
            pass


def is_pid_alive(pid: int) -> bool:
    """Checks whether a process with given PID is currently active.

    Cross-platform safe:
    - Returns False for pid <= 0.
    - On Windows: Uses psutil.pid_exists(pid) if available, with Win32 OpenProcess fallback.
      Never uses os.kill(pid, 0) on Windows to avoid console signal broadcasting.
      PermissionError or ERROR_ACCESS_DENIED is conservatively treated as alive.
    - On POSIX: Uses os.kill(pid, 0).
    """
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import psutil

            return bool(psutil.pid_exists(pid))
        except ImportError:
            pass

        try:
            import ctypes
            from ctypes import wintypes

            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, wintypes.DWORD(pid)
            )
            if handle == 0:
                err = kernel32.GetLastError()
                # ERROR_ACCESS_DENIED (5) -> process exists but cannot be inspected
                if err == 5:
                    return True
                # ERROR_INVALID_PARAMETER (87) -> process does not exist
                return False

            exit_code = wintypes.DWORD()
            res = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            if res == 0:
                # Could not query exit code -> conservatively treat as alive
                return True
            # STILL_ACTIVE = 259
            return exit_code.value == 259
        except Exception:
            # Conservative safety: if inspection fails, do not falsely report dead
            return True

    # POSIX branch
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def verify_archived_asset(content_dir: Path | str) -> ArchivedAssetVerificationResult:
    """Verifies that a content directory constitutes a formal, complete local asset.

    A directory is a formal local asset iff:
    1. Directory exists and is a directory.
    2. asset_manifest.json exists and is valid JSON.
    3. All referenced assets physically exist in the directory.
    4. SHA-256 digest of every asset matches the manifest commitment.
    """
    cdir = Path(content_dir).resolve()
    if not cdir.exists() or not cdir.is_dir():
        return ArchivedAssetVerificationResult(
            valid=False, error=f"Directory '{cdir}' does not exist or is not a directory."
        )

    manifest_path = cdir / MANIFEST_FILENAME
    if not manifest_path.exists() or not manifest_path.is_file():
        return ArchivedAssetVerificationResult(
            valid=False, error=f"Commit marker '{MANIFEST_FILENAME}' is missing in '{cdir}'."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ArchivedAssetVerificationResult(
            valid=False, error=f"Invalid manifest JSON in '{manifest_path}': {exc}"
        )

    raw_assets = manifest.get("assets", [])
    if not raw_assets:
        return ArchivedAssetVerificationResult(
            valid=False, error="Manifest contains zero assets."
        )

    verified_assets: list[DownloadedAsset] = []
    for a in raw_assets:
        fn = a.get("file_name")
        if not fn:
            return ArchivedAssetVerificationResult(
                valid=False, error="Manifest asset entry missing 'file_name'."
            )
        file_path = cdir / fn
        if not file_path.exists() or not file_path.is_file():
            return ArchivedAssetVerificationResult(
                valid=False, error=f"Manifest referenced asset '{fn}' does not exist in '{cdir}'."
            )

        exp_sha = a.get("sha256")
        actual_sha = compute_file_sha256(file_path)
        if exp_sha and actual_sha != exp_sha:
            return ArchivedAssetVerificationResult(
                valid=False,
                error=f"Asset '{fn}' hash mismatch: expected {exp_sha}, got {actual_sha}.",
            )

        verified_assets.append(
            DownloadedAsset(
                file_name=fn,
                relative_path=a.get("relative_archive_path") or f"{cdir.name}/{fn}",
                size_bytes=file_path.stat().st_size,
                content_type=a.get("content_type", "video"),
                sha256=actual_sha,
                width=a.get("media_summary", {}).get("width"),
                height=a.get("media_summary", {}).get("height"),
                duration_sec=a.get("media_summary", {}).get("duration_sec"),
            )
        )

    return ArchivedAssetVerificationResult(
        valid=True, manifest=manifest, assets=tuple(verified_assets)
    )


# =============================================================================
# 6. Production Archive Promoter
# =============================================================================


class ProductionArchivePromoter(ArchivePromoter):
    """Production implementation of ArchivePromoter (Port 6).

    Coordinates transactional staging, validation binding, conflict detection,
    atomic directory publication, and canonical asset manifest commitment.
    """

    def __init__(
        self,
        archive_root: Path | None = None,
        path_budget: FinalArchivePathBudget | None = None,
        require_validation: bool = True,
    ) -> None:
        self.archive_root = (archive_root or Path(tempfile.gettempdir()) / "agy_archive").resolve()
        self.path_budget = path_budget or FinalArchivePathBudget()
        self.require_validation = require_validation

    def resolve_canonical_destination(
        self,
        platform: str,
        platform_content_id: str,
    ) -> Path:
        """Computes the canonical archive destination for physical media assets.

        Invariants:
        1. Sole Authority: D07 owns the formal archive directory hierarchy.
        2. Scope-Agnostic: Physical asset identity is strictly (platform, platform_content_id),
           allowing cross-scope deduplication and idempotent ingest without duplicate media.
        """
        safe_platform = sanitize_filename_component(platform.lower().strip(), max_length=64)
        safe_cid = sanitize_filename_component(platform_content_id.strip(), max_length=64)
        return (self.archive_root / safe_platform / safe_cid).resolve()

    def cleanup_stale_staging(
        self,
        target_base_dir: Path | None = None,
        platform: str = "douyin",
        max_age_seconds: float = 86400,
    ) -> dict[str, int]:
        """Safely cleans up orphaned staging directories.

        Invariants:
        1. Owner PID confirmed alive -> NEVER DELETE.
        2. Ownership uncertain (missing metadata) -> KEEP / QUARANTINE.
        3. Owner dead + expired TTL -> GC.
        """
        staging_root = (target_base_dir or (self.archive_root / platform)) / STAGING_DIR_NAME
        if not staging_root.exists():
            return {"scanned": 0, "cleaned": 0, "kept": 0}

        scanned = 0
        cleaned = 0
        kept = 0
        now = time.time()

        for item in staging_root.iterdir():
            if not item.is_dir():
                continue
            scanned += 1
            meta_path = staging_root / f".meta_{item.name}.json"
            if not meta_path.exists():
                # Ownership uncertain -> KEEP / QUARANTINE
                kept += 1
                continue

            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                owner_pid = int(meta.get("owner_pid", 0))
                created_at = float(meta.get("created_at", 0))
            except Exception:
                # Corrupt metadata -> ownership uncertain -> KEEP
                kept += 1
                continue

            if is_pid_alive(owner_pid):
                # Owner process still running -> NEVER DELETE
                kept += 1
                continue

            # Owner is dead. Check if grace / TTL has expired
            if (now - created_at) > max_age_seconds:
                shutil.rmtree(item, ignore_errors=True)
                meta_path.unlink(missing_ok=True)
                cleaned += 1
            else:
                kept += 1

        return StagingGcSummary(cleaned=cleaned, scanned=scanned, kept=kept)

    def promote(
        self,
        normalized_assets: Sequence[NormalizedAsset | NormalizedArtifact],
        target_directory: Path | None = None,
        validation_result: ValidationResult | dict[str, Any] | None = None,
        platform: str = "douyin",
        platform_content_id: str = "",
        task_id: str = "",
        execution_id: str = "",
        source_sync_run_id: str | None = None,
        scope_id: str = "",
        sandbox_root: Path | None = None,
        **kwargs: Any,
    ) -> PromotionResult:
        """Atomically promotes validated sandbox assets into canonical archive storage.

        Sandbox source files are strictly treated as READ-ONLY INPUT.
        D07 never moves or deletes source files from the sandbox.
        """
        if not normalized_assets:
            raise PromotionPreconditionError(
                "Cannot promote empty normalized asset list.",
                reason="EMPTY_ASSET_LIST",
            )

        # ---------------------------------------------------------------------
        # Step 1: Precondition - Validation Status Check
        # ---------------------------------------------------------------------
        if self.require_validation and validation_result is None:
            raise PromotionPreconditionError(
                "Promotion rejected: media assets have not been validated (require_validation=True).",
                reason="UNVALIDATED_ASSET",
            )

        val_passed = True
        val_summary: dict[str, Any] = {}
        validated_artifacts: list[dict[str, Any]] = []
        val_single_sha: str | None = None

        if validation_result is not None:
            if isinstance(validation_result, ValidationResult):
                val_passed = validation_result.passed
                val_single_sha = validation_result.validated_sha256
                validated_artifacts = list(validation_result.validated_artifacts)
                val_summary = {
                    "passed": validation_result.passed,
                    "ffprobe_verified": validation_result.ffprobe_verified,
                    "decode_smoke_verified": validation_result.decode_smoke_verified,
                    "validated_sha256": validation_result.validated_sha256,
                }
            elif isinstance(validation_result, dict):
                val_passed = bool(validation_result.get("passed", False))
                val_single_sha = validation_result.get("validated_sha256")
                validated_artifacts = list(validation_result.get("validated_artifacts", []))
                val_summary = {
                    "passed": val_passed,
                    "ffprobe_verified": validation_result.get("ffprobe_verified", False),
                    "decode_smoke_verified": validation_result.get("decode_smoke_verified", False),
                    "validated_sha256": val_single_sha,
                }
            else:
                val_passed = getattr(validation_result, "passed", True)
                val_single_sha = getattr(validation_result, "validated_sha256", None)
                validated_artifacts = list(getattr(validation_result, "validated_artifacts", []))

            if not val_passed:
                raise PromotionPreconditionError(
                    f"Promotion rejected: validation failed ({validation_result}).",
                    reason="VALIDATION_FAILED",
                )

        # ---------------------------------------------------------------------
        # Step 2: Infer Identity & Target Directory
        # ---------------------------------------------------------------------
        cid = platform_content_id
        if not cid:
            first_na = normalized_assets[0]
            cid_hint = getattr(first_na, "file_name", "")
            cid = cid_hint.split(".")[0].split("_img_")[0].split("_bgm")[0].split("_cover")[0]
            if not cid and target_directory:
                cid = target_directory.name

        if not cid:
            cid = "unknown_content"

        safe_cid = sanitize_filename_component(cid, max_length=64)

        if target_directory is not None:
            if ".." in str(target_directory) or ".." in target_directory.parts:
                raise PromotionContainmentError(
                    f"Target directory '{target_directory}' contains path traversal sequences.",
                    reason="ARCHIVE_PATH_ESCAPE",
                )
            target_dir = target_directory.resolve()
        else:
            target_dir = self.resolve_canonical_destination(platform, cid)

        # Path budget check on target directory
        self.path_budget.validate_path(target_dir, extra_buffer_len=40)

        if self.archive_root and target_directory is None:
            try:
                target_dir.relative_to(self.archive_root)
            except ValueError:
                raise PromotionContainmentError(
                    f"Target directory '{target_dir}' escapes archive root.",
                    reason="ARCHIVE_PATH_ESCAPE",
                )

        # ---------------------------------------------------------------------
        # Step 3: Source Verification & Cryptographic Validation Binding
        # ---------------------------------------------------------------------
        verified_candidates: list[tuple[NormalizedAsset | NormalizedArtifact, Path, str, int, str]] = []

        for na in normalized_assets:
            src_path = getattr(na, "normalized_path", None)
            if not src_path or src_path == Path() or not src_path.exists():
                src_path = getattr(na, "file_path", None)

            if src_path is None or not isinstance(src_path, Path):
                raise InvalidPromotionArtifactError(
                    f"Normalized asset '{na}' is missing valid file_path.",
                    reason="MISSING_FILE_PATH",
                )

            src_path = src_path.resolve()
            if not src_path.exists() or not src_path.is_file():
                raise InvalidPromotionArtifactError(
                    f"Candidate artifact '{src_path}' does not exist or is not a regular file.",
                    reason="SOURCE_FILE_NOT_FOUND",
                )

            # Rejection firewall for temporary file extensions
            if src_path.suffix.lower() in FORBIDDEN_EXTENSIONS:
                raise InvalidPromotionArtifactError(
                    f"Temporary file '{src_path.name}' is forbidden from archive promotion.",
                    reason="TEMPORARY_FILE_EXTENSION",
                )

            # Sandbox containment verification
            if sandbox_root is not None:
                sb_resolved = sandbox_root.resolve()
                try:
                    src_path.relative_to(sb_resolved)
                except ValueError:
                    raise PromotionContainmentError(
                        f"Source artifact '{src_path}' escapes assigned sandbox root '{sandbox_root}'.",
                        reason="SANDBOX_CONTAINMENT_VIOLATION",
                    )

            stat = src_path.stat()
            if stat.st_size == 0:
                raise PromotionPreconditionError(
                    f"Artifact '{src_path.name}' is zero-byte.",
                    reason="ZERO_BYTE_ARTIFACT",
                )

            # TOCTOU & mutation check against metadata size
            expected_size = getattr(na, "byte_size", None)
            if expected_size is None or expected_size == 0:
                expected_size = getattr(na, "size_bytes", None)
            if (expected_size is None or expected_size == 0) and hasattr(na, "metadata") and isinstance(na.metadata, dict):
                expected_size = na.metadata.get("size_bytes") or na.metadata.get("byte_size")

            if expected_size is not None and expected_size > 0 and stat.st_size != expected_size:
                raise PromotionPreconditionError(
                    f"Artifact '{src_path.name}' mutated post-validation: size changed from {expected_size} to {stat.st_size} bytes.",
                    reason="FILE_MUTATED_POST_VALIDATION",
                    details={"file": src_path.name, "expected_size": expected_size, "current_size": stat.st_size},
                )

            # Read source SHA-256
            src_sha256 = compute_file_sha256(src_path)

            # Cryptographic Validation Binding check
            matching_val_sha: str | None = None
            matching_val_size: int | None = None

            if validated_artifacts:
                for va in validated_artifacts:
                    if va.get("file_name") == na.file_name or va.get("file_path") == str(src_path):
                        matching_val_sha = va.get("validated_sha256")
                        matching_val_size = va.get("size_bytes")
                        break
            elif val_single_sha is not None and len(normalized_assets) == 1:
                matching_val_sha = val_single_sha

            if matching_val_sha is not None and src_sha256 != matching_val_sha:
                raise PromotionCorruptionError(
                    f"VALIDATION_BINDING_MISMATCH: Artifact '{na.file_name}' SHA-256 changed after validation: expected {matching_val_sha}, actual {src_sha256}.",
                    reason="VALIDATION_BINDING_MISMATCH",
                    details={"file": na.file_name, "expected_sha256": matching_val_sha, "current_sha256": src_sha256},
                )

            if matching_val_size is not None and stat.st_size != matching_val_size:
                raise PromotionCorruptionError(
                    f"VALIDATION_BINDING_MISMATCH: Artifact '{na.file_name}' size changed after validation: expected {matching_val_size}, actual {stat.st_size}.",
                    reason="VALIDATION_BINDING_MISMATCH",
                    details={"file": na.file_name, "expected_size": matching_val_size, "current_size": stat.st_size},
                )

            fn = sanitize_filename_component(na.file_name, max_length=64)
            self.path_budget.validate_path(target_dir / fn, extra_buffer_len=30)
            verified_candidates.append((na, src_path, fn, stat.st_size, src_sha256))

        # ---------------------------------------------------------------------
        # Step 4: Existing Target Idempotency & Collision Detection
        # ---------------------------------------------------------------------
        if target_dir.exists():
            verification = verify_archived_asset(target_dir)
            if verification.valid:
                # Check if all candidate assets match the existing assets exactly
                cand_map = {fn: sha for _, _, fn, _, sha in verified_candidates}
                existing_map = {a.file_name: a.sha256 for a in verification.assets}

                if cand_map == existing_map:
                    # Idempotent bypass
                    promoted_list: list[PromotedAsset] = []
                    for na, src_path, fn, size, src_sha256 in verified_candidates:
                        dest_file = target_dir / fn
                        rel_path = f"{platform}/{safe_cid}/{fn}"
                        role_val = getattr(na, "role", ArtifactRole.PRIMARY_VIDEO)
                        role_str = str(role_val.value if hasattr(role_val, "value") else role_val)
                        promoted_list.append(
                            PromotedAsset(
                                file_name=fn,
                                relative_archive_path=rel_path,
                                absolute_archive_path=dest_file,
                                byte_size=size,
                                sha256=src_sha256,
                                role=role_str,
                                content_type=getattr(na, "content_type", "video"),
                                sequence_index=getattr(na, "sequence_index", None),
                                media_summary=getattr(na, "metadata", {}) if hasattr(na, "metadata") else {},
                                validation_summary=val_summary,
                                source_original_name=getattr(na, "source_original_name", src_path.name),
                            )
                        )
                    return PromotionResult(
                        platform=platform,
                        platform_content_id=safe_cid,
                        target_directory=target_dir,
                        manifest_path=target_dir / MANIFEST_FILENAME,
                        promoted_assets=promoted_list,
                        status=PromotionStatus.IDEMPOTENT_EXISTING,
                        idempotent_existing=True,
                        warnings=["Target content directory already exists with identical valid assets. Promotion bypassed."],
                    )

            # If not identical or invalid manifest: raise ArchiveConflictError
            raise ArchiveConflictError(
                f"Target archive collision: directory '{target_dir}' already exists with differing content or invalid manifest.",
                reason="DESTINATION_COLLISION_DIFFERENT_CONTENT",
                details={"target_directory": str(target_dir)},
            )

        # ---------------------------------------------------------------------
        # Step 5: Archive-Local Staging
        # ---------------------------------------------------------------------
        clean_exec = sanitize_filename_component(execution_id or uuid4().hex[:12], max_length=32)
        staging_token = f"{clean_exec}_{safe_cid}"
        staging_base = target_dir.parent / STAGING_DIR_NAME
        staging_content_dir = staging_base / staging_token
        staging_meta_file = staging_base / f".meta_{staging_token}.json"

        staging_content_dir.mkdir(parents=True, exist_ok=True)

        # Write staging ownership metadata for GC safety
        meta_payload = {
            "execution_id": scrub_secrets(execution_id),
            "task_id": scrub_secrets(task_id),
            "platform_content_id": safe_cid,
            "owner_pid": os.getpid(),
            "created_at": time.time(),
            "state": "STAGING",
        }
        staging_meta_file.write_text(json.dumps(meta_payload), encoding="utf-8")

        staged_records: list[PromotedAsset] = []

        try:
            for na, src_path, fn, size, src_sha256 in verified_candidates:
                staged_path = staging_content_dir / fn
                # Copy from sandbox (source remains untouched!)
                safe_copy_and_sync(src_path, staged_path)

                # Verify copy integrity
                dst_sha256 = compute_file_sha256(staged_path)
                if dst_sha256 != src_sha256:
                    raise PromotionCorruptionError(
                        f"Media transfer corrupted for '{fn}': destination SHA-256 ({dst_sha256}) differs from source ({src_sha256}).",
                        reason="TRANSFER_CORRUPTION",
                        details={"file": fn, "source_sha256": src_sha256, "destination_sha256": dst_sha256},
                    )

                final_dest = target_dir / fn
                rel_path = f"{platform}/{safe_cid}/{fn}"
                role_val = getattr(na, "role", ArtifactRole.PRIMARY_VIDEO)
                role_str = str(role_val.value if hasattr(role_val, "value") else role_val)

                staged_records.append(
                    PromotedAsset(
                        file_name=fn,
                        relative_archive_path=rel_path,
                        absolute_archive_path=final_dest,
                        byte_size=size,
                        sha256=dst_sha256,
                        role=role_str,
                        content_type=getattr(na, "content_type", "video"),
                        sequence_index=getattr(na, "sequence_index", None),
                        media_summary=getattr(na, "metadata", {}) if hasattr(na, "metadata") else {},
                        validation_summary=val_summary,
                        source_original_name=getattr(na, "source_original_name", src_path.name),
                    )
                )

            # -----------------------------------------------------------------
            # Step 6: Construct & Fsync Canonical Asset Manifest Inside Staging
            # -----------------------------------------------------------------
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            manifest_payload = {
                "schema_version": "1.0",
                "platform": platform,
                "platform_content_id": safe_cid,
                "archived_at": now_iso,
                "source_provenance": {
                    "task_id": scrub_secrets(task_id),
                    "execution_id": scrub_secrets(execution_id),
                    "source_sync_run_id": scrub_secrets(source_sync_run_id or ""),
                    "scope_id": scrub_secrets(scope_id),
                },
                "asset_count": len(staged_records),
                "assets": [
                    {
                        "file_name": pa.file_name,
                        "relative_archive_path": pa.relative_archive_path,
                        "role": pa.role,
                        "sequence_index": pa.sequence_index,
                        "byte_size": pa.byte_size,
                        "sha256": pa.sha256,
                        "content_type": pa.content_type,
                        "media_summary": pa.media_summary,
                        "validation_summary": pa.validation_summary,
                    }
                    for pa in staged_records
                ],
            }

            staged_manifest = staging_content_dir / MANIFEST_FILENAME
            temp_manifest = staging_content_dir / f".{MANIFEST_FILENAME}.tmp.{uuid4().hex[:8]}"
            manifest_bytes = json.dumps(manifest_payload, indent=2, ensure_ascii=False).encode("utf-8")
            with open(temp_manifest, "wb") as mf:
                mf.write(manifest_bytes)
                mf.flush()
                try:
                    os.fsync(mf.fileno())
                except OSError:
                    pass
            os.replace(temp_manifest, staged_manifest)

            # -----------------------------------------------------------------
            # Step 7: Atomic Directory Publication
            # -----------------------------------------------------------------
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            # Atomic directory rename on the same target volume
            os.replace(staging_content_dir, target_dir)

            # Clean up staging metadata
            staging_meta_file.unlink(missing_ok=True)

            return PromotionResult(
                platform=platform,
                platform_content_id=safe_cid,
                target_directory=target_dir,
                manifest_path=target_dir / MANIFEST_FILENAME,
                promoted_assets=staged_records,
                status=PromotionStatus.SUCCESS,
                idempotent_existing=False,
            )

        except Exception:
            # Transaction aborted: clean up staging directory and metadata completely
            if staging_content_dir.exists():
                shutil.rmtree(staging_content_dir, ignore_errors=True)
            staging_meta_file.unlink(missing_ok=True)
            raise
