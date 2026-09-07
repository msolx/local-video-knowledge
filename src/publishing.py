from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .intake.media_probe import MediaInfo, is_valid_complete_av, probe_media


class PublishMediaError(RuntimeError):
    """Raised when a completed-media target cannot be safely published."""


@dataclass(frozen=True)
class PublishedMedia:
    path: Path
    relative_path: str
    publish_mode: str
    skipped: bool

    def as_knowledge_reference(self, video_id: str) -> dict[str, str]:
        return {
            "video_id": video_id,
            "filename": self.path.name,
            "relative_path": self.relative_path,
            "publish_mode": self.publish_mode,
        }

    def as_metadata_reference(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "absolute_path": str(self.path),
            "publish_mode": self.publish_mode,
        }


def completed_media_root(data_root: Path, settings: dict[str, Any]) -> Path:
    """Resolve the publishing root, accepting paths relative to data_root.

    ``completed_media`` is the portable recommended value.  The older-looking
    ``./data/completed_media`` form is also accepted when data_root itself is
    named ``data``; it resolves to the same directory instead of data/data.
    """
    configured = Path(str(settings.get("completed_media_root", "completed_media")))
    if configured.is_absolute():
        return configured
    parts = [part for part in configured.parts if part not in {".", ""}]
    if parts and parts[0].lower() == data_root.name.lower():
        return data_root.parent.joinpath(*parts)
    return data_root.joinpath(*parts)


def _duration_matches(source: MediaInfo, published: MediaInfo) -> bool:
    tolerance = max(1.0, source.duration * 0.002)
    return abs(source.duration - published.duration) <= tolerance


def _validate(source: Path, destination: Path, ffprobe: Path) -> None:
    source_info = probe_media(ffprobe, source)
    destination_info = probe_media(ffprobe, destination)
    if not is_valid_complete_av(source_info):
        raise PublishMediaError(f"Authoritative normalized source is not valid audio+video media: {source}")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise PublishMediaError(f"Published media is missing or empty: {destination}")
    if not is_valid_complete_av(destination_info):
        raise PublishMediaError(f"Published media does not contain verified audio and video streams: {destination}")
    if not _duration_matches(source_info, destination_info):
        raise PublishMediaError(
            f"Published media duration differs from normalized source: {source_info.duration}s vs {destination_info.duration}s"
        )


def _existing_mode(source: Path, destination: Path) -> str:
    try:
        return "hardlink" if source.samefile(destination) else "copy"
    except OSError:
        return "copy"


def _copy_atomically(source: Path, destination: Path) -> None:
    """Copy to a sibling temporary file, exposing the final name only when complete."""
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.copying")
    try:
        shutil.copy2(source, temporary)
        # The per-video pipeline lock serializes publishers.  We still refuse to
        # overwrite a target that appeared unexpectedly between validation and rename.
        if destination.exists():
            raise PublishMediaError(f"Completed-media target appeared while publishing; refusing to overwrite: {destination}")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish_completed_media(
    source: Path,
    video_id: str,
    data_root: Path,
    settings: dict[str, Any],
    ffprobe: Path,
) -> PublishedMedia:
    """Publish verified normalized media via hardlink, with an atomic-copy fallback.

    The authoritative normalized source remains untouched.  An existing target
    is never overwritten: it must already be a valid A/V file with a matching
    duration or publishing fails explicitly.
    """
    root = completed_media_root(data_root, settings)
    destination = root / video_id / "source.mp4"
    try:
        relative_path = destination.resolve().relative_to(data_root.resolve()).as_posix()
    except ValueError as error:
        raise PublishMediaError("completed_media_root must be located under data_root so a portable relative_path can be recorded.") from error

    if destination.exists():
        _validate(source, destination, ffprobe)
        return PublishedMedia(destination, relative_path, _existing_mode(source, destination), skipped=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    mode: str
    created = False
    hardlink_error: OSError | None = None
    if bool(settings.get("prefer_hardlink", True)):
        try:
            os.link(source, destination)
            mode, created = "hardlink", True
        except OSError as error:
            hardlink_error = error
    if not created:
        if not bool(settings.get("copy_fallback", True)):
            detail = f" ({hardlink_error})" if hardlink_error else ""
            raise PublishMediaError(f"Hardlink publishing failed and copy fallback is disabled{detail}")
        _copy_atomically(source, destination)
        mode, created = "copy", True
    try:
        _validate(source, destination, ffprobe)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return PublishedMedia(destination, relative_path, mode, skipped=False)
