"""Domain models and exceptions for Milestone M3 CanonicalMediaAssetAdapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.intake import MediaAsset


# =============================================================================
# 1. Error Taxonomy
# =============================================================================


class MediaAdapterError(Exception):
    """Base exception for all Media Adapter operations."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ManifestNotFoundError(MediaAdapterError):
    """Raised when asset_manifest.json is missing in the asset root."""


class ManifestInvalidError(MediaAdapterError):
    """Raised when asset_manifest.json is malformed or violates schema invariants."""


class MediaFileNotFoundError(MediaAdapterError):
    """Raised when a referenced media artifact does not exist on disk."""


class MediaHashMismatchError(MediaAdapterError):
    """Raised when a media file's SHA-256 digest differs from manifest commitment."""


class UnsupportedContentTypeError(MediaAdapterError):
    """Raised when an asset content_type is unknown or unsupported."""


class InvalidMediaAssetError(MediaAdapterError):
    """Raised when a media asset directory has inconsistent or corrupt structure."""


# =============================================================================
# 2. Enums
# =============================================================================


class CanonicalMediaType(str, Enum):
    """Discrete media types recognized by M3 Media Knowledge Integration."""

    VIDEO = "video"
    IMAGE_ALBUM = "image_album"


# =============================================================================
# 3. Artifact & Asset Domain Models
# =============================================================================


@dataclass(frozen=True)
class AlbumImageArtifact:
    """Ordered image artifact belonging to an image_album asset."""

    sequence_index: int
    file_name: str
    path: Path
    sha256: str
    byte_size: int
    content_type: str = "image"
    media_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        return self.byte_size


    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "file_name": self.file_name,
            "path": str(self.path),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "content_type": self.content_type,
            "media_summary": self.media_summary,
        }


@dataclass(frozen=True)
class CanonicalMediaAsset:
    """Canonical domain representation of a verified M2 formal local asset.

    Serves as the decoupled intermediate boundary between M2 storage
    (archive/<platform>/<content_id>/asset_manifest.json) and M3 processing.
    """

    platform: str
    platform_content_id: str
    content_type: CanonicalMediaType
    canonical_id: str
    asset_root: Path
    manifest_path: Path
    video_path: Path | None = None
    video_sha256: str | None = None
    video_size_bytes: int | None = None
    album_images: tuple[AlbumImageArtifact, ...] = ()
    audio_path: Path | None = None
    audio_sha256: str | None = None
    audio_size_bytes: int | None = None
    archived_at: str = ""
    source_provenance: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    raw_manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def is_video(self) -> bool:
        return self.content_type == CanonicalMediaType.VIDEO

    @property
    def is_album(self) -> bool:
        return self.content_type == CanonicalMediaType.IMAGE_ALBUM

    @property
    def has_audio(self) -> bool:
        return self.audio_path is not None

    @property
    def title(self) -> str:
        return self.source_metadata.get("title") or self.platform_content_id

    @property
    def author_name(self) -> str | None:
        author = self.source_metadata.get("author")
        if isinstance(author, dict):
            return author.get("display_name")
        return None

    @property
    def primary_media_path(self) -> Path:
        if self.is_video and self.video_path:
            return self.video_path
        if self.is_album and self.album_images:
            return self.album_images[0].path
        raise InvalidMediaAssetError("No primary media file available in CanonicalMediaAsset.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "content_type": self.content_type.value,
            "canonical_id": self.canonical_id,
            "asset_root": str(self.asset_root),
            "manifest_path": str(self.manifest_path),
            "video_path": str(self.video_path) if self.video_path else None,
            "video_sha256": self.video_sha256,
            "video_size_bytes": self.video_size_bytes,
            "album_images": [img.to_dict() for img in self.album_images],
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "audio_sha256": self.audio_sha256,
            "audio_size_bytes": self.audio_size_bytes,
            "archived_at": self.archived_at,
            "source_provenance": self.source_provenance,
            "source_metadata": self.source_metadata,
        }

    def to_pipeline_media_asset(self, config: AppConfig, working_dir: Path | None = None) -> MediaAsset:
        """Projects a video CanonicalMediaAsset into the legacy pipeline MediaAsset.

        Allows existing pipeline stages (ASR, visual reference extraction, knowledge extraction)
        to process the formal local video without manual incoming-directory copying or remuxing.
        """
        if not self.is_video or not self.video_path:
            raise UnsupportedContentTypeError(
                f"Cannot project non-video asset ({self.content_type.value}) to pipeline MediaAsset."
            )

        from src.intake.media_probe import probe_media
        from src.intake.media_mux import MediaAsset

        probe = probe_media(config.ffprobe, self.video_path)
        content_hash = self.video_sha256 or hashlib.sha256(self.video_path.read_bytes()).hexdigest()
        video_dir = working_dir or (config.data_root / "processed" / self.canonical_id)

        media_record = {
            "input_type": "canonical_formal_asset",
            "status": "complete_av" if (probe.video and probe.audio) else "video_only",
            "content_hash": content_hash,
            "video_source": str(self.video_path),
            "audio_source": str(self.audio_path) if self.audio_path else None,
            "normalized_source": str(self.video_path),
            "originals": {"source": str(self.video_path)},
            "normalized_probe": probe.as_dict(),
            "source_metadata": self.source_metadata,
            "source_provenance": self.source_provenance,
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "canonical_id": self.canonical_id,
        }

        return MediaAsset(
            video_id=self.canonical_id,
            content_hash=content_hash,
            title=self.title,
            video_dir=video_dir,
            normalized_source=self.video_path,
            probe=probe,
            media=media_record,
        )

    def process_asr(self, config: AppConfig, force: bool = False) -> Path:
        """Processes this video formal asset through the existing ASR pipeline.

        Extracts 16kHz mono audio from PRIMARY_VIDEO and produces verified transcript evidence.
        """
        from src.pipeline import process_canonical_asset
        return process_canonical_asset(config, self, force=force, stop_after="asr")

    def process_visual(self, config: AppConfig, force: bool = False, stop_after: str | None = None) -> Path:
        """Processes this canonical asset through the visual & OCR pipeline.

        For image albums, extracts ordered OCR lines with full provenance and runs optional VLM.
        For videos, invokes video visual evidence pipeline.
        """
        if self.is_album:
            from src.pipeline import process_canonical_album
            return process_canonical_album(config, self, force=force, stop_after=stop_after)
        elif self.is_video:
            from src.pipeline import process_canonical_asset
            return process_canonical_asset(config, self, force=force, stop_after=stop_after)
        else:
            raise UnsupportedContentTypeError(f"Unsupported content type: {self.content_type}")

    def bind_evidence(
        self,
        config: AppConfig | None = None,
        force: bool = False,
        processed_dir: Path | None = None,
    ) -> Path:
        """Binds source metadata, formal asset manifest, and derived media evidence into evidence_manifest.json.

        Guarantees deterministic ordering, immutability of formal archive, and sub-second idempotency.
        """
        from src.provenance import write_evidence_manifest
        return write_evidence_manifest(self, config=config, processed_dir=processed_dir, force=force)

    def chunk_evidence(
        self,
        config: AppConfig | None = None,
        policy: Any | None = None,
        force: bool = False,
        processed_dir: Path | None = None,
    ) -> Path:
        """Chunks verified evidence manifest into deterministic, grounded Evidence Chunks (M3-05).

        Guarantees sub-second cache hit on matching fingerprint, 100% unique evidence coverage,
        and strict formal archive immutability.
        """
        from src.chunking import write_evidence_chunks
        return write_evidence_chunks(
            self,
            config=config,
            policy=policy,
            force=force,
            processed_dir=processed_dir,
        )


