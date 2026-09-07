"""Asset Normalizer & MAX_PATH Shield (DY-D06).

Implements:
1. Pre-Download Path Planning:
   - BackendOutputPlan: Safe short output path planning inside task sandbox.
   - MAX_PATH budget shield: Configurable path budget (default 240 chars on Windows)
     to significantly reduce MAX_PATH risks before backend execution.
   - Flat task-local output directory planning (folderize=False, short naming).
2. Post-Download Asset Normalization:
   - Sanitized, deterministic naming based exclusively on platform_content_id:
     - PRIMARY_VIDEO: <platform_content_id><safe_ext>
     - ALBUM_IMAGE: <platform_content_id>_img_{sequence_index:03d}<safe_ext>
     - BGM_AUDIO: <platform_content_id>_bgm<safe_ext>
     - COVER_IMAGE: <platform_content_id>_cover<safe_ext>
   - Sequence-ordered normalization for image albums (stable contract ordering,
     never relying on filesystem enumeration, mtime, or glob order).
   - Strict rejection of temporary/incomplete extensions (.part, .tmp, .crdownload, etc.).
   - Windows reserved device name protection (CON, PRN, AUX, NUL, COM1-9, LPT1-9, trailing dots/spaces).
   - Extension preservation and sanitization (no blind transcoding or extension forcing).
   - Exclusion of diagnostic files (.txt, .json, .log) from the normalized media artifact set.
   - Sandbox path containment validation (rejects any candidate outside the sandbox).
   - Collision detection & prevention (rejects duplicate primary videos or duplicate sequence indices).
   - Idempotent execution within the same task sandbox.
   - Atomic normalization_manifest.json emission (relative paths, zero secrets).
3. Downloader Subsystem Ports:
   - ProductionAssetNormalizer implementing the AssetNormalizer Protocol.
   - Seamless compatibility with SafeDouyinDownloader Stage 5 (NORMALIZING) and D05 (VALIDATING).
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.downloader.contracts import (
    AssetNormalizer,
    DownloaderErrorCode,
    NormalizedAsset,
    scrub_secrets,
)

logger = logging.getLogger(__name__)

# Temporary / incomplete file extensions strictly forbidden as final candidates
_FORBIDDEN_EXTENSIONS = {".part", ".tmp", ".crdownload", ".download", ".incomplete"}

# Diagnostic / sidecar extensions excluded from normalized media artifacts
_DIAGNOSTIC_EXTENSIONS = {".txt", ".json", ".log", ".desc"}

# Windows reserved device names (must not be used as filename stems)
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# Unsafe characters for Windows and POSIX filenames
_ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# =============================================================================
# 1. Enums & Core Data Contracts
# =============================================================================


class ArtifactRole(str, enum.Enum):
    """Semantic role of an artifact produced during download."""

    PRIMARY_VIDEO = "PRIMARY_VIDEO"
    ALBUM_IMAGE = "ALBUM_IMAGE"
    BGM_AUDIO = "BGM_AUDIO"
    COVER_IMAGE = "COVER_IMAGE"
    OTHER_DIAGNOSTIC = "OTHER_DIAGNOSTIC"


@dataclass(frozen=True)
class ArtifactCandidate:
    """Explicitly registered candidate artifact produced by download backend."""

    file_path: Path
    role: ArtifactRole | str = ArtifactRole.PRIMARY_VIDEO
    media_kind: str = "video"  # "video", "image", "audio", "cover", "diagnostic"
    sequence_index: int | None = None  # 1-indexed sequence for albums
    source_extension: str | None = None
    backend_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.file_path, str):
            object.__setattr__(self, "file_path", Path(self.file_path))


@dataclass(frozen=True)
class BackendOutputPlan:
    """Pre-download safe output layout planned for download backend (D06 -> D03)."""

    execution_id: str
    sandbox_output_root: Path
    safe_filename_stem: str
    max_path_budget: int = 240
    allow_subdirectories: bool = False
    platform_content_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializes plan to dictionary for generic backend consumption."""
        return {
            "execution_id": self.execution_id,
            "sandbox_output_root": str(self.sandbox_output_root),
            "safe_filename_stem": self.safe_filename_stem,
            "max_path_budget": self.max_path_budget,
            "allow_subdirectories": self.allow_subdirectories,
            "platform_content_id": self.platform_content_id,
        }

    def compute_safe_output_path(self, filename: str) -> Path:
        """Computes target path and asserts it remains within the max_path_budget."""
        target = (self.sandbox_output_root / filename).resolve()
        target_len = len(str(target))
        if target_len > self.max_path_budget:
            raise PathBudgetExceededError(
                f"Planned path '{target}' ({target_len} chars) exceeds MAX_PATH budget ({self.max_path_budget} chars).",
                details={"target_path": str(target), "path_length": target_len, "budget": self.max_path_budget},
            )
        return target


@dataclass(frozen=True)
class NormalizedArtifact(NormalizedAsset):
    """Individual normalized media asset in sandbox, fully compatible with NormalizedAsset."""

    platform_content_id: str = ""
    role: str = ArtifactRole.PRIMARY_VIDEO.value
    sequence_index: int | None = None
    normalized_path: Path = field(default_factory=Path)
    normalized_relative_path: str = ""
    original_candidate_reference: str = ""
    byte_size: int = 0
    media_kind: str = "video"
    extension: str = ""
    sha256_provisional: str | None = None  # Non-authoritative hash for idempotency/collision check

    def to_dict(self) -> dict[str, Any]:
        """Returns JSON-serializable dictionary representation."""
        return {
            "file_path": str(self.file_path),
            "content_type": self.content_type,
            "file_name": self.file_name,
            "platform_content_id": self.platform_content_id,
            "role": str(self.role),
            "sequence_index": self.sequence_index,
            "normalized_path": str(self.normalized_path),
            "normalized_relative_path": self.normalized_relative_path,
            "original_candidate_reference": self.original_candidate_reference,
            "byte_size": self.byte_size,
            "media_kind": self.media_kind,
            "extension": self.extension,
            "sha256_provisional": self.sha256_provisional,
            "metadata": dict(self.metadata),
        }


@dataclass
class NormalizationResult:
    """Comprehensive output of Stage 5 normalization (D06 -> D05/D07)."""

    artifacts: list[NormalizedArtifact] = field(default_factory=list)
    manifest_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    path_plan: BackendOutputPlan | None = None

    def __iter__(self):
        """Allows direct iteration over normalized artifacts for backward compatibility."""
        return iter(self.artifacts)

    def __len__(self) -> int:
        return len(self.artifacts)

    def __getitem__(self, index: int) -> NormalizedArtifact:
        return self.artifacts[index]

    def __bool__(self) -> bool:
        return len(self.artifacts) > 0

    def to_dict(self) -> dict[str, Any]:
        """Serializes result into structured JSON-compatible dictionary."""
        return {
            "artifact_count": len(self.artifacts),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "warnings": list(self.warnings),
            "path_plan": self.path_plan.to_dict() if self.path_plan else None,
        }


# =============================================================================
# 2. Normalization Exceptions (QW-13 / D01 Mapped)
# =============================================================================


class NormalizationError(Exception):
    """Base exception for asset normalization failures."""

    def __init__(self, message: str, reason: str = "NORMALIZATION_ERROR", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class PathBudgetExceededError(NormalizationError):
    """Raised when sandbox root or full file path exceeds max_path_budget."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason="PATH_BUDGET_EXCEEDED", details=details)


class NormalizationCollisionError(NormalizationError):
    """Raised when multiple candidates map to the same normalized target name."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason="NORMALIZATION_COLLISION", details=details)


class NormalizationContainmentError(NormalizationError):
    """Raised when an artifact candidate resides outside the task sandbox."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason="NORMALIZATION_PATH_ESCAPE", details=details)


class InvalidArtifactExtensionError(NormalizationError):
    """Raised when an artifact has a temporary or illegal extension."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason="INVALID_ARTIFACT_EXTENSION", details=details)


# =============================================================================
# 3. Sanitization & Validation Helpers
# =============================================================================


def sanitize_filename_component(name: str, max_length: int = 255) -> str:
    """Sanitizes an individual path component against Windows and POSIX rules."""
    if not name:
        return "unnamed"

    # Replace illegal characters with underscore
    cleaned = _ILLEGAL_CHARS_PATTERN.sub("_", name)

    # Strip leading/trailing whitespaces and dots (Windows rule)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "unnamed"

    # Check Windows reserved device names
    base_stem = cleaned.split(".")[0].upper()
    if base_stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    # Truncate if exceeding component budget
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")

    return cleaned


def sanitize_extension(raw_ext: str) -> tuple[str, list[str]]:
    """Sanitizes file extension, rejecting temporary files and invalid characters.

    Returns:
        tuple[sanitized_ext_with_dot, warnings]
    """
    warnings: list[str] = []
    if not raw_ext:
        return ("", warnings)

    lowered = raw_ext.lower().strip()
    if not lowered.startswith("."):
        lowered = f".{lowered}"

    # Strict rejection of incomplete / temporary download extensions
    if lowered in _FORBIDDEN_EXTENSIONS:
        raise InvalidArtifactExtensionError(
            f"Candidate has temporary/incomplete extension '{lowered}', cannot normalize.",
            details={"extension": lowered},
        )

    # Check for path separators or directory traversal in extension
    if "/" in lowered or "\\" in lowered or ".." in lowered or "\x00" in lowered:
        raise InvalidArtifactExtensionError(
            f"Extension '{raw_ext}' contains path traversal or separator characters.",
            details={"extension": raw_ext},
        )

    # Extract clean extension: allowed alphanumeric, max 10 chars
    ext_body = lowered[1:]
    if not ext_body.isalnum() or len(ext_body) > 10:
        cleaned_body = re.sub(r"[^a-z0-9]", "", ext_body)[:10]
        if not cleaned_body:
            raise InvalidArtifactExtensionError(f"Extension '{raw_ext}' contains no valid alphanumeric characters.")
        warnings.append(f"Extension '{raw_ext}' sanitized to '.{cleaned_body}'.")
        lowered = f".{cleaned_body}"

    return (lowered, warnings)


def verify_sandbox_containment(target_path: Path, sandbox_root: Path) -> Path:
    """Asserts that target_path is strictly within sandbox_root containment."""
    resolved_target = target_path.resolve()
    resolved_sandbox = sandbox_root.resolve()
    try:
        resolved_target.relative_to(resolved_sandbox)
        return resolved_target
    except ValueError:
        raise NormalizationContainmentError(
            f"Path '{target_path}' escapes sandbox root '{sandbox_root}' containment!",
            details={"target": str(target_path), "sandbox_root": str(sandbox_root)},
        )


# =============================================================================
# 4. Production Asset Normalizer Implementation
# =============================================================================


class ProductionAssetNormalizer(AssetNormalizer):
    """Production Asset Normalizer & MAX_PATH Shield (DY-D06)."""

    def __init__(
        self,
        max_path_budget: int = 240,
        max_component_budget: int = 255,
        default_video_extension: str = ".mp4",
        default_image_extension: str = ".webp",
        default_audio_extension: str = ".mp3",
    ) -> None:
        self.max_path_budget = max_path_budget
        self.max_component_budget = max_component_budget
        self.default_video_extension = default_video_extension
        self.default_image_extension = default_image_extension
        self.default_audio_extension = default_audio_extension

    # -------------------------------------------------------------------------
    # Pre-Download Path Planning
    # -------------------------------------------------------------------------

    def plan_output(
        self,
        sandbox_root: Path,
        platform_content_id: str,
        execution_id: str = "",
        allow_subdirectories: bool = False,
    ) -> BackendOutputPlan:
        """Pre-download path planning.

        Validates sandbox root depth and generates safe short output path instructions
        to significantly reduce MAX_PATH risks before backend driver execution.
        """
        resolved_sandbox = sandbox_root.resolve()
        output_dir = resolved_sandbox / "output"

        # Check sandbox root depth
        # Estimate conservative target length: output_dir + / + content_id + _img_999.webp (approx 35 chars)
        estimated_longest = len(str(output_dir)) + len(platform_content_id) + 35
        if estimated_longest > self.max_path_budget:
            raise PathBudgetExceededError(
                f"Sandbox output root '{output_dir}' is too deep ({len(str(output_dir))} chars). "
                f"Estimated target path ({estimated_longest} chars) exceeds MAX_PATH budget ({self.max_path_budget} chars).",
                details={
                    "sandbox_output_root": str(output_dir),
                    "root_length": len(str(output_dir)),
                    "estimated_longest": estimated_longest,
                    "max_path_budget": self.max_path_budget,
                },
            )

        # Ensure output directory physically exists
        output_dir.mkdir(parents=True, exist_ok=True)

        safe_stem = sanitize_filename_component(platform_content_id, max_length=64)

        return BackendOutputPlan(
            execution_id=execution_id,
            sandbox_output_root=output_dir,
            safe_filename_stem=safe_stem,
            max_path_budget=self.max_path_budget,
            allow_subdirectories=allow_subdirectories,
            platform_content_id=platform_content_id,
        )

    # -------------------------------------------------------------------------
    # Post-Download Asset Normalization
    # -------------------------------------------------------------------------

    def normalize(
        self,
        raw_assets: Sequence[Path | ArtifactCandidate],
        platform_content_id: str,
        content_type: str = "video",
        metadata_hint: dict[str, Any] | None = None,
        sandbox_root: Path | None = None,
        path_plan: BackendOutputPlan | None = None,
        task_id: str = "",
        execution_id: str = "",
    ) -> NormalizationResult:
        """Normalizes candidate artifacts into safe, deterministic sandbox-local layout.

        Satisfies the AssetNormalizer Protocol port in D01 while returning a rich
        NormalizationResult iterable.
        """
        warnings: list[str] = []
        hint = metadata_hint or {}

        # 1. Resolve sandbox boundaries and output root
        if path_plan is not None:
            output_root = path_plan.sandbox_output_root
            sb_root = sandbox_root or path_plan.sandbox_output_root.parent
        elif sandbox_root is not None:
            output_root = (sandbox_root / "output").resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            sb_root = sandbox_root.resolve()
        elif raw_assets:
            # Infer from first candidate
            first_path = raw_assets[0].file_path if isinstance(raw_assets[0], ArtifactCandidate) else raw_assets[0]
            sb_root = first_path.resolve().parent
            output_root = sb_root
        else:
            output_root = Path(tempfile.gettempdir()).resolve()
            sb_root = output_root

        # 2. Ingest and wrap candidates
        candidates: list[ArtifactCandidate] = []
        for idx, item in enumerate(raw_assets):
            if isinstance(item, ArtifactCandidate):
                candidates.append(item)
            elif isinstance(item, Path):
                # Legacy / simple Path input: infer role and kind
                cand_role = ArtifactRole.PRIMARY_VIDEO
                media_kind = content_type or "video"
                seq_idx: int | None = None

                ext = item.suffix.lower()
                if ext in (".webp", ".jpg", ".jpeg", ".png"):
                    cand_role = ArtifactRole.ALBUM_IMAGE
                    media_kind = "image"
                    # Try to extract sequence index from filename e.g. _1.webp
                    seq_match = re.search(r"[-_](\d+)\.[a-zA-Z0-9]+$", item.name)
                    if seq_match:
                        seq_idx = int(seq_match.group(1))
                    else:
                        seq_idx = idx + 1
                elif ext in (".mp3", ".m4a", ".aac", ".flac"):
                    if "bgm" in item.name.lower() or "music" in item.name.lower() or content_type == "image_album":
                        cand_role = ArtifactRole.BGM_AUDIO
                        media_kind = "audio"
                    else:
                        cand_role = ArtifactRole.PRIMARY_VIDEO
                        media_kind = "audio"

                candidates.append(
                    ArtifactCandidate(
                        file_path=item,
                        role=cand_role,
                        media_kind=media_kind,
                        sequence_index=seq_idx,
                        source_extension=ext,
                        backend_metadata={},
                    )
                )
            else:
                raise NormalizationError(f"Unsupported candidate item type: {type(item)}")

        # 3. Filter out diagnostics & validate candidates
        valid_candidates: list[ArtifactCandidate] = []
        for cand in candidates:
            # Containment check: candidate source MUST be within sandbox
            verify_sandbox_containment(cand.file_path, sb_root)

            # Exclude diagnostics (.txt, .json, .log, or role OTHER_DIAGNOSTIC)
            if cand.role == ArtifactRole.OTHER_DIAGNOSTIC or cand.file_path.suffix.lower() in _DIAGNOSTIC_EXTENSIONS:
                logger.debug("Excluding diagnostic candidate from media set: %s", cand.file_path.name)
                continue

            # Reject temporary extensions (.part, .tmp, etc.)
            clean_ext, ext_warns = sanitize_extension(cand.source_extension or cand.file_path.suffix)
            warnings.extend(ext_warns)

            valid_candidates.append(cand)

        # 4. Sort and organize image album candidates strictly by sequence_index
        # CRITICAL INVARIANT (Section N & AG): Never rely on filesystem enumeration order!
        album_candidates = [c for c in valid_candidates if c.role == ArtifactRole.ALBUM_IMAGE]
        non_album_candidates = [c for c in valid_candidates if c.role != ArtifactRole.ALBUM_IMAGE]

        if album_candidates:
            # Assert every album candidate has a sequence_index
            for ac in album_candidates:
                if ac.sequence_index is None:
                    raise NormalizationError(
                        f"Image album candidate '{ac.file_path.name}' is missing sequence_index.",
                        reason="MISSING_SEQUENCE_INDEX",
                    )
            # Check duplicate sequence indices (Collision Protection)
            seen_indices: set[int] = set()
            for ac in album_candidates:
                if ac.sequence_index in seen_indices:
                    raise NormalizationCollisionError(
                        f"Duplicate sequence_index '{ac.sequence_index}' detected in image album candidates!",
                        details={"duplicate_index": ac.sequence_index, "file": ac.file_path.name},
                    )
                seen_indices.add(ac.sequence_index)

            # Sort album candidates strictly by contract sequence_index
            album_candidates.sort(key=lambda c: c.sequence_index or 0)

        # 5. Check primary video collision (multiple PRIMARY_VIDEO candidates)
        primary_videos = [c for c in non_album_candidates if c.role == ArtifactRole.PRIMARY_VIDEO]
        if len(primary_videos) > 1:
            raise NormalizationCollisionError(
                f"Multiple ({len(primary_videos)}) PRIMARY_VIDEO candidates supplied for content '{platform_content_id}'.",
                details={"candidate_count": len(primary_videos)},
            )

        # Re-assemble ordered list
        ordered_candidates = non_album_candidates + album_candidates

        # 6. Generate planned target paths and execute safe normalization
        planned_targets: dict[Path, ArtifactCandidate] = {}
        normalized_artifacts: list[NormalizedArtifact] = []

        safe_content_id = sanitize_filename_component(platform_content_id, max_length=64)

        for cand in ordered_candidates:
            ext, ext_warns = sanitize_extension(cand.source_extension or cand.file_path.suffix)
            warnings.extend(ext_warns)

            # Determine deterministic normalized filename
            if cand.role == ArtifactRole.PRIMARY_VIDEO:
                target_ext = ext if ext else self.default_video_extension
                norm_name = f"{safe_content_id}{target_ext}"
            elif cand.role == ArtifactRole.ALBUM_IMAGE:
                target_ext = ext if ext else self.default_image_extension
                seq = cand.sequence_index or 1
                norm_name = f"{safe_content_id}_img_{seq:03d}{target_ext}"
            elif cand.role == ArtifactRole.BGM_AUDIO:
                target_ext = ext if ext else self.default_audio_extension
                norm_name = f"{safe_content_id}_bgm{target_ext}"
            elif cand.role == ArtifactRole.COVER_IMAGE:
                target_ext = ext if ext else self.default_image_extension
                norm_name = f"{safe_content_id}_cover{target_ext}"
            else:
                target_ext = ext
                norm_name = f"{safe_content_id}_{sanitize_filename_component(str(cand.role).lower())}{target_ext}"

            # Validate component budget
            if len(norm_name) > self.max_component_budget:
                raise NormalizationError(
                    f"Generated filename '{norm_name}' exceeds component budget ({self.max_component_budget} chars).",
                    reason="COMPONENT_BUDGET_EXCEEDED",
                )

            target_path = (output_root / norm_name).resolve()

            # Validate path budget
            if len(str(target_path)) > self.max_path_budget:
                raise PathBudgetExceededError(
                    f"Target path '{target_path}' ({len(str(target_path))} chars) exceeds MAX_PATH budget ({self.max_path_budget} chars).",
                    details={"target_path": str(target_path), "path_length": len(str(target_path)), "budget": self.max_path_budget},
                )

            # Ensure target stays strictly inside sandbox
            verify_sandbox_containment(target_path, sb_root)

            # Collision check against previously planned targets in this run
            if target_path in planned_targets:
                raise NormalizationCollisionError(
                    f"Collision detected: candidate '{cand.file_path.name}' maps to already assigned target '{norm_name}'.",
                    details={"colliding_target": str(target_path)},
                )
            planned_targets[target_path] = cand

            # Physical rename / placement within sandbox (atomic mutation)
            source_path = cand.file_path.resolve()
            if source_path != target_path:
                if target_path.exists():
                    # Idempotency check: if target already exists with identical size, treat as idempotent
                    if source_path.exists() and target_path.stat().st_size == source_path.stat().st_size:
                        warnings.append(f"Idempotent normalization: target '{target_path.name}' already exists.")
                    else:
                        # Conflicting existing target
                        raise NormalizationCollisionError(
                            f"Conflicting target file already exists at '{target_path}'.",
                            details={"target_path": str(target_path)},
                        )
                else:
                    # Execute atomic move within sandbox
                    try:
                        os.replace(source_path, target_path)
                    except Exception as mv_err:
                        raise NormalizationError(
                            f"Failed to normalize asset placement from '{source_path}' to '{target_path}': {mv_err}",
                            reason="FILESYSTEM_MUTATION_FAILED",
                        ) from mv_err

            # Compute relative path to sandbox root
            try:
                rel_path = str(target_path.relative_to(sb_root)).replace("\\", "/")
            except ValueError:
                rel_path = target_path.name

            byte_size = target_path.stat().st_size if target_path.exists() else 0

            norm_art = NormalizedArtifact(
                file_path=target_path,
                content_type=cand.media_kind,
                file_name=target_path.name,
                platform_content_id=platform_content_id,
                role=str(cand.role),
                sequence_index=cand.sequence_index,
                normalized_path=target_path,
                normalized_relative_path=rel_path,
                original_candidate_reference=cand.file_path.name,
                byte_size=byte_size,
                media_kind=cand.media_kind,
                extension=target_path.suffix,
                metadata={"original_name": cand.file_path.name, **hint},
            )
            normalized_artifacts.append(norm_art)

        # 7. Write atomic normalization_manifest.json inside sandbox
        manifest_path: Path | None = None
        if sb_root.is_dir():
            manifest_path = sb_root / "normalization_manifest.json"
            self._write_manifest_atomically(
                manifest_path=manifest_path,
                task_id=task_id,
                execution_id=execution_id,
                platform="douyin",
                platform_content_id=platform_content_id,
                content_type=content_type,
                artifacts=normalized_artifacts,
                warnings=warnings,
            )

        return NormalizationResult(
            artifacts=normalized_artifacts,
            manifest_path=manifest_path,
            warnings=warnings,
            path_plan=path_plan,
        )

    # -------------------------------------------------------------------------
    # Internal Manifest Persistence Helper
    # -------------------------------------------------------------------------

    def _write_manifest_atomically(
        self,
        manifest_path: Path,
        task_id: str,
        execution_id: str,
        platform: str,
        platform_content_id: str,
        content_type: str,
        artifacts: list[NormalizedArtifact],
        warnings: list[str],
    ) -> None:
        """Writes normalization_manifest.json atomically with zero secrets."""
        manifest_data = {
            "schema_version": "1.0",
            "task_id": scrub_secrets(task_id),
            "execution_id": scrub_secrets(execution_id),
            "platform": platform,
            "platform_content_id": platform_content_id,
            "content_type": content_type,
            "artifact_count": len(artifacts),
            "artifacts": [
                {
                    "role": a.role,
                    "sequence_index": a.sequence_index,
                    "normalized_relative_path": a.normalized_relative_path,
                    "original_candidate_reference": a.original_candidate_reference,
                    "extension": a.extension,
                    "byte_size": a.byte_size,
                    "media_kind": a.media_kind,
                }
                for a in artifacts
            ],
            "warnings": [scrub_secrets(w) for w in warnings],
        }

        # Deterministic atomic write: temp file in same directory + replace
        parent_dir = manifest_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        payload_bytes = json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8")

        temp_fd, temp_path_str = tempfile.mkstemp(prefix="norm_manifest_", suffix=".tmp", dir=parent_dir)
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(payload_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path_str, manifest_path)
        except Exception:
            if os.path.exists(temp_path_str):
                try:
                    os.unlink(temp_path_str)
                except OSError:
                    pass
            raise
        return manifest_path


def plan_backend_output(
    platform_content_id: str,
    sandbox_root: Path,
    execution_id: str = "",
    allow_subdirectories: bool = False,
    max_path_budget: int = 240,
) -> BackendOutputPlan:
    """Convenience helper to plan backend output using ProductionAssetNormalizer."""
    normalizer = ProductionAssetNormalizer(max_path_budget=max_path_budget)
    return normalizer.plan_output(
        sandbox_root=sandbox_root,
        platform_content_id=platform_content_id,
        execution_id=execution_id,
        allow_subdirectories=allow_subdirectories,
    )
