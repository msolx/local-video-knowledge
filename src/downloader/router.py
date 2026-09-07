"""Content Routing Engine & Execution Planner (DY-D09).

Pure deterministic routing engine that maps a DownloadTask into a fully-specified
DownloadExecutionPlan (download-execution-plan-v1) and enforces strict artifact-route
conformity.

Key Invariants:
1. Strict Determinism & Isolation:
   - Zero network calls, zero browser sessions, zero credential acquisitions.
   - Zero F2 imports, zero filesystem scans, zero time.sleep(), zero queue polling,
     zero thread / worker pools.
2. Single Authority:
   - Route selection is 100% determined by task.content_type ('video' or 'image_album').
   - NEVER guesses routes by file extension (.mp4, .webp).
3. Separation of Archive Path Ownership (DY-D07 Invariant):
   - D09 ContentRouter DOES NOT own or compute formal filesystem archive destinations.
   - Formal archive layout is owned exclusively by D07 ArchivePromoter.
   - Physical asset identity is strictly (platform, platform_content_id), NOT bound to scope_id.
   - DownloadExecutionPlan contains zero filesystem target paths.
4. Explicit Validation Profile Binding:
   - Production SafeDouyinDownloader must NEVER use ValidationProfile.AUTO.
   - video route strictly binds ValidationProfile.VIDEO.
   - image_album route strictly binds ValidationProfile.IMAGE_SET.
5. Unsupported Content Handling:
   - Unsupported types ('live', 'note', 'article', 'story', 'unknown', '', None)
     raise UnsupportedContentTypeError and map strictly to DOWNLOAD_UNSUPPORTED_CONTENT
     (which D08 treats as TERMINAL).
6. Comprehensive Conformity Rules:
   - Video Route:
     * PRIMARY_VIDEO required (strictly 1).
     * COVER_IMAGE optional.
     * Audio stream/artifact optional by default; only strictly required if canonical
       metadata explicitly flags has_audio=True / expect_audio=True / audio_required=True.
     * Rejects album images.
   - Image Album Route:
     * ALBUM_IMAGE required (>= 1, matching expected image count if specified).
     * 1-based sequence indices strictly required without duplicates.
     * BGM_AUDIO optional by default.
     * COVER_IMAGE optional.
     * Rejects primary video.
   - Route Mismatch & Contradiction Detection:
     * Video plan receiving only images -> DOWNLOAD_MEDIA_INCOMPLETE mismatch.
     * Album plan receiving video -> DOWNLOAD_MEDIA_INCOMPLETE mismatch.
     * Mixed primaries (both PRIMARY_VIDEO and ALBUM_IMAGE) -> DOWNLOAD_MEDIA_INCOMPLETE rejection.
7. Scope Isolation:
   - scope_id is preserved solely for execution provenance (D02 credentials, D08 pause_scope, D10).
   - scope_id NEVER enters physical archive storage identity.
"""

from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.collector.download_models import DownloadTask
from src.downloader.contracts import (
    ContentRouter,
    DownloaderErrorCode,
    NormalizedAsset,
    scrub_secrets,
)
from src.downloader.normalizer import ArtifactCandidate, ArtifactRole
from src.downloader.validator import ValidationProfile

logger = logging.getLogger(__name__)

# Canonical supported content types for Douyin downloads
SUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset({"video", "image_album"})

# Common video file extensions for fallback filename analysis
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".flv", ".webm", ".avi", ".ts"})
_IMAGE_EXTENSIONS = frozenset({".webp", ".jpg", ".jpeg", ".png", ".bmp", ".heic"})
_AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"})


# =============================================================================
# 1. Enums & Core Exceptions
# =============================================================================


class ExecutionMode(str, enum.Enum):
    """Execution mode determined exclusively by DownloadTask.content_type."""

    VIDEO = "video"
    IMAGE_ALBUM = "image_album"


class RoutingError(Exception):
    """Base exception for routing engine failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(scrub_secrets(message))
        self.details = details or {}


class UnsupportedContentTypeError(RoutingError):
    """Raised when a DownloadTask has an unknown or unsupported content_type."""

    def __init__(
        self,
        message: str,
        content_type: str = "",
        task_id: str = "",
        platform_content_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details)
        self.content_type = content_type
        self.task_id = task_id
        self.platform_content_id = platform_content_id
        self.error_code = DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT


class ArtifactRouteMismatchError(RoutingError):
    """Raised when candidate artifacts do not match the expected execution route."""

    def __init__(
        self,
        message: str,
        error_code: DownloaderErrorCode = DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details)
        self.error_code = error_code


# =============================================================================
# 2. Plan Data Models (Schema: download-execution-plan-v1)
# =============================================================================


@dataclass(frozen=True)
class ExpectedArtifactExpectation:
    """Explicit expectation contract for artifacts produced by a download route."""

    primary_role: ArtifactRole | str
    min_primary_count: int
    max_primary_count: int | None = None
    allow_cover: bool = True
    require_audio: bool = False
    allow_audio: bool = True
    strict_sequence_indices: bool = False
    min_images: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_role": (
                self.primary_role.value
                if isinstance(self.primary_role, ArtifactRole)
                else str(self.primary_role)
            ),
            "min_primary_count": self.min_primary_count,
            "max_primary_count": self.max_primary_count,
            "allow_cover": self.allow_cover,
            "require_audio": self.require_audio,
            "allow_audio": self.allow_audio,
            "strict_sequence_indices": self.strict_sequence_indices,
            "min_images": self.min_images,
        }


@dataclass(frozen=True)
class DownloadExecutionPlan:
    """Deterministic execution plan generated by ContentRouter for a DownloadTask.

    Schema: download-execution-plan-v1.
    Contains zero filesystem archive paths (D07 owns archive publication).
    """

    task_id: str
    platform: str
    scope_id: str
    platform_content_id: str
    content_type: str
    mode: ExecutionMode
    validation_profile: ValidationProfile
    primary_artifact_role: ArtifactRole
    expected_artifacts: ExpectedArtifactExpectation
    require_video_stream: bool = False
    require_audio_stream: bool = False
    min_images: int = 0
    metadata_hint: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "download-execution-plan-v1"

    @property
    def backend_acquisition_mode(self) -> str:
        """Explicit alias for backend acquisition mode."""
        return self.mode.value if isinstance(self.mode, ExecutionMode) else str(self.mode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "platform": self.platform,
            "scope_id": self.scope_id,
            "platform_content_id": self.platform_content_id,
            "content_type": self.content_type,
            "mode": self.mode.value if isinstance(self.mode, ExecutionMode) else str(self.mode),
            "backend_acquisition_mode": self.backend_acquisition_mode,
            "validation_profile": (
                self.validation_profile.value
                if isinstance(self.validation_profile, ValidationProfile)
                else str(self.validation_profile)
            ),
            "primary_artifact_role": (
                self.primary_artifact_role.value
                if isinstance(self.primary_artifact_role, ArtifactRole)
                else str(self.primary_artifact_role)
            ),
            "expected_artifacts": self.expected_artifacts.to_dict(),
            "require_video_stream": self.require_video_stream,
            "require_audio_stream": self.require_audio_stream,
            "min_images": self.min_images,
            "metadata_hint": dict(self.metadata_hint),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class ConformityResult:
    """Output contract for artifact-route conformity verification."""

    passed: bool
    error_code: DownloaderErrorCode | None = None
    reason: str | None = None
    primary_count: int = 0
    cover_count: int = 0
    audio_count: int = 0
    other_count: int = 0
    sequence_indices: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "error_code": self.error_code.value if self.error_code else None,
            "reason": scrub_secrets(self.reason) if self.reason else None,
            "primary_count": self.primary_count,
            "cover_count": self.cover_count,
            "audio_count": self.audio_count,
            "other_count": self.other_count,
            "sequence_indices": list(self.sequence_indices),
        }


# =============================================================================
# 3. Artifact Parser Helper
# =============================================================================


def _parse_candidate_info(
    candidate: Any,
) -> tuple[ArtifactRole | str, int | None, str]:
    """Extracts (role, sequence_index, filename) from any candidate artifact representation."""
    # Case 1: ArtifactCandidate dataclass
    if isinstance(candidate, ArtifactCandidate):
        role = candidate.role
        seq = candidate.sequence_index
        name = candidate.file_path.name if candidate.file_path else ""
        return role, seq, name

    # Case 2: NormalizedAsset dataclass
    if isinstance(candidate, NormalizedAsset):
        name = candidate.file_name or (candidate.file_path.name if candidate.file_path else "")
        ct = (candidate.content_type or "").lower()
        seq = candidate.metadata.get("sequence_index") if candidate.metadata else None

        if ct == "video":
            return ArtifactRole.PRIMARY_VIDEO, None, name
        elif ct in ("image_album", "image"):
            if seq is None:
                m_img = re.search(r"(?:_img_|_image_)(\d+)", name.lower())
                if m_img:
                    seq = int(m_img.group(1))
            return ArtifactRole.ALBUM_IMAGE, seq, name
        elif ct in ("audio", "bgm"):
            return ArtifactRole.BGM_AUDIO, None, name
        elif ct == "cover":
            return ArtifactRole.COVER_IMAGE, None, name

        # Fallback to name pattern
        return _parse_from_name(name, seq)

    # Case 3: Path or string
    if isinstance(candidate, (Path, str)):
        path = Path(candidate)
        return _parse_from_name(path.name, None)

    # Case 4: Dict representation
    if isinstance(candidate, dict):
        role_raw = candidate.get("role") or candidate.get("content_type")
        seq = candidate.get("sequence_index")
        name = candidate.get("file_name") or candidate.get("file_path", "")
        if isinstance(name, str):
            name = Path(name).name
        if role_raw:
            try:
                return ArtifactRole(role_raw), seq, str(name)
            except ValueError:
                pass
        return _parse_from_name(str(name), seq)

    # Unknown
    return ArtifactRole.OTHER_DIAGNOSTIC, None, ""


def _parse_from_name(
    filename: str, seq: int | None
) -> tuple[ArtifactRole | str, int | None, str]:
    """Infers semantic role and sequence from canonical D06 naming patterns."""
    fname_lower = filename.lower()

    # Image album item: <id>_img_<seq>.<ext>
    m_img = re.search(r"_img_(\d+)", fname_lower)
    if m_img:
        parsed_seq = seq if seq is not None else int(m_img.group(1))
        return ArtifactRole.ALBUM_IMAGE, parsed_seq, filename

    # BGM Audio: <id>_bgm.<ext>
    if "_bgm" in fname_lower:
        return ArtifactRole.BGM_AUDIO, None, filename

    # Cover image: <id>_cover.<ext>
    if "_cover" in fname_lower:
        return ArtifactRole.COVER_IMAGE, None, filename

    # File extension check
    ext = Path(filename).suffix.lower()
    if ext in _VIDEO_EXTENSIONS:
        return ArtifactRole.PRIMARY_VIDEO, None, filename
    elif ext in _IMAGE_EXTENSIONS:
        return ArtifactRole.ALBUM_IMAGE, seq, filename
    elif ext in _AUDIO_EXTENSIONS:
        return ArtifactRole.BGM_AUDIO, None, filename

    return ArtifactRole.OTHER_DIAGNOSTIC, None, filename


# =============================================================================
# 4. Production Content Router Engine
# =============================================================================


class ProductionContentRouter(ContentRouter):
    """Production routing engine and execution planner (DY-D09).

    Determines execution mode, validation profiles, and artifact expectations
    based strictly on DownloadTask.content_type.
    Does NOT generate filesystem archive destinations (D07 owns archive publication).
    """

    def is_supported(self, content_type: str | None) -> bool:
        """Checks if a content_type is supported by the routing engine."""
        if not content_type or not isinstance(content_type, str):
            return False
        return content_type.strip().lower() in SUPPORTED_CONTENT_TYPES

    def plan_execution(
        self,
        task: DownloadTask,
        execution_id: str | None = None,
    ) -> DownloadExecutionPlan:
        """Generates a deterministic DownloadExecutionPlan for a DownloadTask.

        Single Authority:
        - Governed exclusively by task.content_type.
        - Never guesses or mutates plan based on source_url extension.
        - Contains zero filesystem archive paths (D07 owns archive publication).

        Unsupported types:
        - Raises UnsupportedContentTypeError (mapped to DOWNLOAD_UNSUPPORTED_CONTENT).
        """
        ct_raw = (task.content_type or "").strip().lower()

        if not self.is_supported(ct_raw):
            raise UnsupportedContentTypeError(
                f"Unsupported content_type '{task.content_type}' for task '{task.task_id}'. "
                f"Expected one of: {sorted(SUPPORTED_CONTENT_TYPES)}.",
                content_type=task.content_type or "",
                task_id=task.task_id,
                platform_content_id=task.platform_content_id,
                details={
                    "task_id": task.task_id,
                    "platform_content_id": task.platform_content_id,
                    "attempted_content_type": task.content_type,
                    "supported_types": sorted(SUPPORTED_CONTENT_TYPES),
                },
            )

        metadata_hint = dict(task.download_input)
        if hasattr(task, "metadata_ref") and isinstance(task.metadata_ref, dict):
            # Canonical metadata takes precedence if provided
            metadata_hint.update(task.metadata_ref)

        if ct_raw == "video":
            return self._plan_video_route(task, metadata_hint)
        elif ct_raw == "image_album":
            return self._plan_image_album_route(task, metadata_hint)
        else:
            raise UnsupportedContentTypeError(
                f"Unhandled supported content_type: {ct_raw}",
                content_type=ct_raw,
                task_id=task.task_id,
                platform_content_id=task.platform_content_id,
            )

    def _plan_video_route(
        self,
        task: DownloadTask,
        metadata_hint: dict[str, Any],
    ) -> DownloadExecutionPlan:
        """Plans execution for video content."""
        has_audio_flag = (
            metadata_hint.get("has_audio") is True
            or metadata_hint.get("expect_audio") is True
            or metadata_hint.get("audio_required") is True
        )

        expectation = ExpectedArtifactExpectation(
            primary_role=ArtifactRole.PRIMARY_VIDEO,
            min_primary_count=1,
            max_primary_count=1,
            allow_cover=True,
            require_audio=has_audio_flag,
            allow_audio=True,
            strict_sequence_indices=False,
            min_images=0,
        )

        return DownloadExecutionPlan(
            task_id=task.task_id,
            platform=task.platform,
            scope_id=task.scope_id,
            platform_content_id=task.platform_content_id,
            mode=ExecutionMode.VIDEO,
            content_type="video",
            validation_profile=ValidationProfile.VIDEO,
            primary_artifact_role=ArtifactRole.PRIMARY_VIDEO,
            expected_artifacts=expectation,
            require_video_stream=True,
            require_audio_stream=has_audio_flag,
            min_images=0,
            metadata_hint=metadata_hint,
        )

    def _plan_image_album_route(
        self,
        task: DownloadTask,
        metadata_hint: dict[str, Any],
    ) -> DownloadExecutionPlan:
        """Plans execution for image album content."""
        raw_count = metadata_hint.get("image_count")
        expected_min = 1
        if isinstance(raw_count, int) and raw_count > 0:
            expected_min = raw_count

        expectation = ExpectedArtifactExpectation(
            primary_role=ArtifactRole.ALBUM_IMAGE,
            min_primary_count=expected_min,
            max_primary_count=None,
            allow_cover=True,
            require_audio=False,  # BGM is optional by default
            allow_audio=True,     # BGM is permitted
            strict_sequence_indices=True,
            min_images=expected_min,
        )

        return DownloadExecutionPlan(
            task_id=task.task_id,
            platform=task.platform,
            scope_id=task.scope_id,
            platform_content_id=task.platform_content_id,
            mode=ExecutionMode.IMAGE_ALBUM,
            content_type="image_album",
            validation_profile=ValidationProfile.IMAGE_SET,
            primary_artifact_role=ArtifactRole.ALBUM_IMAGE,
            expected_artifacts=expectation,
            require_video_stream=False,
            require_audio_stream=False,
            min_images=expected_min,
            metadata_hint=metadata_hint,
        )

    # -------------------------------------------------------------------------
    # Artifact Conformity Verification
    # -------------------------------------------------------------------------

    def validate_artifact_conformity(
        self,
        plan: DownloadExecutionPlan,
        candidates: Sequence[Any],
    ) -> ConformityResult:
        """Verifies whether candidate artifacts conform to the DownloadExecutionPlan.

        Detects:
        - Route mismatches (e.g. video plan with only images, or album plan with video).
        - Contradictory mixed primaries (both video and album images present).
        - Missing or insufficient primary assets.
        - Sequence index errors in image albums (missing, negative, or duplicate indices).
        """
        if not candidates:
            return ConformityResult(
                passed=False,
                error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                reason="No candidate artifacts provided for conformity validation.",
            )

        video_count = 0
        album_image_count = 0
        bgm_audio_count = 0
        cover_count = 0
        other_count = 0
        sequence_indices: list[int] = []
        has_invalid_seq = False
        invalid_seq_val: Any = None

        for item in candidates:
            role, seq, name = _parse_candidate_info(item)

            if role == ArtifactRole.PRIMARY_VIDEO:
                video_count += 1
            elif role == ArtifactRole.ALBUM_IMAGE:
                album_image_count += 1
                if seq is None or seq <= 0:
                    has_invalid_seq = True
                    invalid_seq_val = seq
                else:
                    sequence_indices.append(seq)
            elif role == ArtifactRole.BGM_AUDIO:
                bgm_audio_count += 1
            elif role == ArtifactRole.COVER_IMAGE:
                cover_count += 1
            else:
                other_count += 1

        # Check 1: Contradictory mixed primaries
        if video_count > 0 and album_image_count > 0:
            return ConformityResult(
                passed=False,
                error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                reason=(
                    f"Contradictory media: both primary video ({video_count}) and "
                    f"album images ({album_image_count}) present in artifact set."
                ),
                primary_count=video_count + album_image_count,
                cover_count=cover_count,
                audio_count=bgm_audio_count,
                other_count=other_count,
            )

        # Check 2: Video Route Specific Validation
        if plan.mode == ExecutionMode.VIDEO:
            # Route mismatch check: only images in video plan
            if album_image_count > 0 and video_count == 0:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason=(
                        f"Route mismatch: Video execution plan received {album_image_count} "
                        f"album image artifacts and 0 video artifacts."
                    ),
                    primary_count=0,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if video_count == 0:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason="Missing required primary video artifact for video route.",
                    primary_count=0,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if video_count > 1:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason=(
                        f"Multiple primary video artifacts detected ({video_count}). "
                        f"Expected exactly 1."
                    ),
                    primary_count=video_count,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if plan.expected_artifacts.require_audio and bgm_audio_count == 0:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason="Required audio artifact is missing for video plan.",
                    primary_count=video_count,
                    cover_count=cover_count,
                    audio_count=0,
                    other_count=other_count,
                )

            return ConformityResult(
                passed=True,
                primary_count=video_count,
                cover_count=cover_count,
                audio_count=bgm_audio_count,
                other_count=other_count,
            )

        # Check 3: Image Album Route Specific Validation
        elif plan.mode == ExecutionMode.IMAGE_ALBUM:
            # Route mismatch check: video present in album plan
            if video_count > 0:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason=(
                        f"Route mismatch: Image album execution plan received {video_count} "
                        f"video artifacts and {album_image_count} album images."
                    ),
                    primary_count=album_image_count,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if album_image_count == 0:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason="Missing required album image artifacts for image_album route.",
                    primary_count=0,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if album_image_count < plan.expected_artifacts.min_primary_count:
                return ConformityResult(
                    passed=False,
                    error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                    reason=(
                        f"Insufficient album images: expected at least "
                        f"{plan.expected_artifacts.min_primary_count}, found {album_image_count}."
                    ),
                    primary_count=album_image_count,
                    cover_count=cover_count,
                    audio_count=bgm_audio_count,
                    other_count=other_count,
                )

            if plan.expected_artifacts.strict_sequence_indices:
                if has_invalid_seq:
                    return ConformityResult(
                        passed=False,
                        error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
                        reason=(
                            f"Album image has missing or non-positive sequence index: {invalid_seq_val}."
                        ),
                        primary_count=album_image_count,
                        cover_count=cover_count,
                        audio_count=bgm_audio_count,
                        other_count=other_count,
                    )

                # Duplicate sequence indices check
                if len(sequence_indices) != len(set(sequence_indices)):
                    seen = set()
                    dups = set()
                    for s in sequence_indices:
                        if s in seen:
                            dups.add(s)
                        seen.add(s)
                    return ConformityResult(
                        passed=False,
                        error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
                        reason=f"Duplicate album image sequence indices detected: {sorted(dups)}.",
                        primary_count=album_image_count,
                        cover_count=cover_count,
                        audio_count=bgm_audio_count,
                        other_count=other_count,
                        sequence_indices=tuple(sequence_indices),
                    )

            return ConformityResult(
                passed=True,
                primary_count=album_image_count,
                cover_count=cover_count,
                audio_count=bgm_audio_count,
                other_count=other_count,
                sequence_indices=tuple(sorted(sequence_indices)),
            )

        # Fallback
        return ConformityResult(
            passed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT,
            reason=f"Unrecognized execution mode: {plan.mode}",
        )
