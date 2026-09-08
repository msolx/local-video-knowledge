"""Production implementation of CanonicalMediaAssetAdapter (M3-01).

Loads, validates, and adapts M2 D07 formal local assets into CanonicalMediaAsset domain models
for downstream consumption by M3 media knowledge processing pipelines.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from .models import (
    AlbumImageArtifact,
    CanonicalMediaAsset,
    CanonicalMediaType,
    InvalidMediaAssetError,
    ManifestInvalidError,
    ManifestNotFoundError,
    MediaFileNotFoundError,
    MediaHashMismatchError,
    UnsupportedContentTypeError,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "asset_manifest.json"


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a local file in 128 KiB chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(128 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


class CanonicalMediaAssetAdapter:
    """Deterministic, read-only adapter that translates formal M2 archive directories into CanonicalMediaAsset models."""

    def __init__(
        self,
        archive_root: Path | str | None = None,
        metadata_db_path: Path | str | None = None,
        validate_hashes: bool = True,
        enable_metadata_enrichment: bool = True,
    ) -> None:
        self.archive_root = Path(archive_root).resolve() if archive_root else None
        self.metadata_db_path = Path(metadata_db_path).resolve() if metadata_db_path else None
        self.validate_hashes = validate_hashes
        self.enable_metadata_enrichment = enable_metadata_enrichment

    def load_from_dir(self, content_dir: Path | str) -> CanonicalMediaAsset:
        """Loads and verifies a formal asset from its content directory."""
        root = Path(content_dir).resolve()
        if not root.exists() or not root.is_dir():
            raise ManifestNotFoundError(f"Content directory '{root}' does not exist or is not a directory.")

        manifest_file = root / MANIFEST_FILENAME
        if not manifest_file.exists() or not manifest_file.is_file():
            raise ManifestNotFoundError(f"Commit marker '{MANIFEST_FILENAME}' not found in '{root}'.")

        try:
            manifest_text = manifest_file.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
        except Exception as exc:
            raise ManifestInvalidError(f"Corrupt JSON in '{manifest_file}': {exc}") from exc

        if not isinstance(manifest, dict):
            raise ManifestInvalidError(f"Manifest root must be a JSON object, got {type(manifest).__name__}.")

        # Required fields
        for field_name in ("schema_version", "platform", "platform_content_id", "assets"):
            if field_name not in manifest:
                raise ManifestInvalidError(f"Manifest missing required field '{field_name}'.")

        platform = str(manifest["platform"])
        platform_content_id = str(manifest["platform_content_id"])
        raw_assets = manifest["assets"]

        if not isinstance(raw_assets, list) or len(raw_assets) == 0:
            raise ManifestInvalidError("Manifest contains zero asset items.")

        # Determine media type (from manifest or asset contents)
        content_type = self._determine_content_type(manifest, raw_assets)

        video_path: Path | None = None
        video_sha256: str | None = None
        video_size_bytes: int | None = None

        album_images: list[AlbumImageArtifact] = []
        audio_path: Path | None = None
        audio_sha256: str | None = None
        audio_size_bytes: int | None = None

        # Parse and validate individual asset artifacts
        for idx, item in enumerate(raw_assets, 1):
            if not isinstance(item, dict):
                raise ManifestInvalidError(f"Asset entry #{idx} is not a valid object.")

            file_name = item.get("file_name")
            if not file_name:
                raise ManifestInvalidError(f"Asset entry #{idx} missing 'file_name'.")

            file_path = (root / file_name).resolve()
            if not file_path.exists() or not file_path.is_file():
                raise MediaFileNotFoundError(
                    f"Manifest referenced file '{file_name}' does not exist in '{root}'.",
                    details={"file_name": file_name, "expected_path": str(file_path)},
                )

            stat = file_path.stat()
            if stat.st_size == 0:
                raise InvalidMediaAssetError(f"Referenced media file '{file_name}' is empty (0 bytes).")

            expected_sha = item.get("sha256")
            if self.validate_hashes:
                actual_sha = _compute_sha256(file_path)
                if expected_sha and actual_sha != expected_sha:
                    raise MediaHashMismatchError(
                        f"SHA-256 hash mismatch for '{file_name}': expected {expected_sha}, got {actual_sha}.",
                        details={"file": file_name, "expected": expected_sha, "actual": actual_sha},
                    )
            else:
                actual_sha = expected_sha or ""

            # Normalize role
            raw_role = str(item.get("role", "")).upper()
            clean_role = raw_role.replace("ARTIFACTROLE.", "")
            item_content_type = str(item.get("content_type", "")).lower()

            if clean_role == "PRIMARY_VIDEO" or item_content_type == "video":
                video_path = file_path
                video_sha256 = actual_sha
                video_size_bytes = stat.st_size
            elif clean_role in ("AUDIO_TRACK", "BGM") or item_content_type == "audio":
                audio_path = file_path
                audio_sha256 = actual_sha
                audio_size_bytes = stat.st_size
            elif clean_role == "ALBUM_IMAGE" or item_content_type == "image":
                seq_idx = item.get("sequence_index")
                if seq_idx is None or not isinstance(seq_idx, int):
                    seq_idx = len(album_images) + 1
                album_images.append(
                    AlbumImageArtifact(
                        sequence_index=seq_idx,
                        file_name=file_name,
                        path=file_path,
                        sha256=actual_sha,
                        byte_size=stat.st_size,
                        content_type="image",
                        media_summary=item.get("media_summary", {}),
                    )
                )

        # Invariant checks for content types
        if content_type == CanonicalMediaType.VIDEO:
            if not video_path:
                raise InvalidMediaAssetError(
                    f"Video formal asset '{root}' does not contain a primary video file.",
                    details={"platform_content_id": platform_content_id},
                )
        elif content_type == CanonicalMediaType.IMAGE_ALBUM:
            if not album_images:
                raise InvalidMediaAssetError(
                    f"Image album formal asset '{root}' contains zero image artifacts.",
                    details={"platform_content_id": platform_content_id},
                )
            # Ensure strictly ordered sequence
            album_images.sort(key=lambda img: img.sequence_index)

        # Retrieve optional collector metadata from SQLite DB
        source_metadata = self._load_collector_metadata(platform, platform_content_id)

        canonical_id = f"{platform}_{platform_content_id}"

        return CanonicalMediaAsset(
            platform=platform,
            platform_content_id=platform_content_id,
            content_type=content_type,
            canonical_id=canonical_id,
            asset_root=root,
            manifest_path=manifest_file,
            video_path=video_path,
            video_sha256=video_sha256,
            video_size_bytes=video_size_bytes,
            album_images=tuple(album_images),
            audio_path=audio_path,
            audio_sha256=audio_sha256,
            audio_size_bytes=audio_size_bytes,
            archived_at=str(manifest.get("archived_at", "")),
            source_provenance=dict(manifest.get("source_provenance", {})),
            source_metadata=source_metadata,
            raw_manifest=manifest,
        )

    def load_from_content_id(self, platform_content_id: str, platform: str = "douyin") -> CanonicalMediaAsset:
        """Loads a formal asset by platform and content ID from the configured archive_root."""
        if not self.archive_root:
            raise MediaAdapterError("Cannot load by content ID: archive_root is not configured.")

        candidate = self.archive_root / platform / platform_content_id
        if not candidate.exists() or not candidate.is_dir():
            raise ManifestNotFoundError(
                f"Formal asset directory for {platform}:{platform_content_id} not found at '{candidate}'."
            )

        return self.load_from_dir(candidate)

    def load_all(self, platform: str = "douyin") -> list[CanonicalMediaAsset]:
        """Scans the configured archive_root for all formal assets belonging to a platform."""
        if not self.archive_root:
            raise MediaAdapterError("Cannot load all: archive_root is not configured.")

        platform_dir = self.archive_root / platform
        if not platform_dir.exists() or not platform_dir.is_dir():
            return []

        results: list[CanonicalMediaAsset] = []
        for child in sorted(platform_dir.iterdir()):
            if child.is_dir() and not child.name.startswith((".", "_")):
                manifest = child / MANIFEST_FILENAME
                if manifest.is_file():
                    try:
                        asset = self.load_from_dir(child)
                        results.append(asset)
                    except Exception as err:
                        logger.warning("Skipping invalid formal asset directory '%s': %s", child, err)
        return results

    def _determine_content_type(self, manifest: dict[str, Any], raw_assets: list[dict[str, Any]]) -> CanonicalMediaType:
        """Determines CanonicalMediaType from manifest attributes or asset composition."""
        explicit_type = manifest.get("content_type")
        if explicit_type:
            try:
                return CanonicalMediaType(explicit_type)
            except ValueError:
                pass

        # Inspect assets roles/types
        has_video = False
        has_images = False

        for a in raw_assets:
            role = str(a.get("role", "")).upper()
            ctype = str(a.get("content_type", "")).lower()
            if "PRIMARY_VIDEO" in role or ctype == "video":
                has_video = True
            elif "ALBUM_IMAGE" in role or ctype == "image":
                has_images = True

        if has_video:
            return CanonicalMediaType.VIDEO
        if has_images:
            return CanonicalMediaType.IMAGE_ALBUM

        raise UnsupportedContentTypeError(
            f"Unable to determine supported media content_type from manifest assets in '{manifest.get('platform_content_id')}'."
        )

    def _load_collector_metadata(self, platform: str, platform_content_id: str) -> dict[str, Any]:
        """Read-only query to fetch canonical collector metadata from SQLite metadata.db if present."""
        if not self.enable_metadata_enrichment:
            return {}

        if not self.metadata_db_path or not self.metadata_db_path.is_file():
            return {}

        try:
            # Open SQLite in read-only URI mode to strictly adhere to read-only guarantees
            uri = f"file:{self.metadata_db_path.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as con:
                cur = con.cursor()
                row = cur.execute(
                    "SELECT canonical_json FROM collection_items WHERE platform_content_id = ? AND platform = ? LIMIT 1",
                    (platform_content_id, platform),
                ).fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
                    if isinstance(data, dict):
                        try:
                            extra = cur.execute(
                                "SELECT first_seen_at, published_at FROM collection_items WHERE platform_content_id = ? AND platform = ? LIMIT 1",
                                (platform_content_id, platform),
                            ).fetchone()
                            if extra:
                                if extra[0] and "first_seen_at" not in data:
                                    data["first_seen_at"] = extra[0]
                                if extra[1] and "published_at" not in data:
                                    data["published_at"] = extra[1]
                        except Exception:
                            pass
                        return data
        except Exception as exc:
            logger.debug("Could not read collector metadata for %s:%s: %s", platform, platform_content_id, exc)

        return {}
