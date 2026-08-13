from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


MediaType = Literal["audio_video", "video_only", "audio_only", "invalid"]


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    media_type: MediaType
    duration: float
    size_bytes: int
    modified_at: float
    created_at: float
    format_name: str | None
    video: dict[str, Any] | None
    audio: dict[str, Any] | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "media_type": self.media_type, "duration": self.duration,
            "size_bytes": self.size_bytes, "modified_at": self.modified_at, "created_at": self.created_at,
            "format": self.format_name, "video": self.video, "audio": self.audio, "error": self.error,
        }


def probe_media(ffprobe: Path, path: Path) -> MediaInfo:
    """Classify a file using ffprobe; output decoding is locale-independent."""
    stat = path.stat()
    command = [str(ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return MediaInfo(path, "invalid", 0.0, stat.st_size, stat.st_mtime, stat.st_ctime, None, None, None,
                         result.stderr.decode("utf-8", errors="replace").strip()[-1000:])
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        duration = float(payload.get("format", {}).get("duration", 0.0))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return MediaInfo(path, "invalid", 0.0, stat.st_size, stat.st_mtime, stat.st_ctime, None, None, None, str(error))
    video_stream = next((item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None)
    audio_stream = next((item for item in payload.get("streams", []) if item.get("codec_type") == "audio"), None)
    video = None if video_stream is None else {key: video_stream.get(key) for key in ("codec_name", "width", "height", "r_frame_rate")}
    audio = None if audio_stream is None else {key: audio_stream.get(key) for key in ("codec_name", "sample_rate", "channels")}
    if duration <= 0:
        media_type: MediaType = "invalid"
    elif video and audio:
        media_type = "audio_video"
    elif video:
        media_type = "video_only"
    elif audio:
        media_type = "audio_only"
    else:
        media_type = "invalid"
    return MediaInfo(path, media_type, round(duration, 3), stat.st_size, stat.st_mtime, stat.st_ctime,
                     payload.get("format", {}).get("format_name"), video, audio)


def is_valid_complete_av(info: MediaInfo) -> bool:
    return info.media_type == "audio_video" and info.duration > 0
