from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..storage import atomic_write_json, copy_file_atomically, load_json, sha256_file, utc_now
from .media_pairing import PairDecision, pair_streams
from .media_probe import MediaInfo, is_valid_complete_av, probe_media


@dataclass(frozen=True)
class MediaAsset:
    video_id: str
    content_hash: str
    title: str
    video_dir: Path
    normalized_source: Path
    probe: MediaInfo
    media: dict[str, Any]


def _asset_identity(kind: str, files: list[Path]) -> tuple[str, str]:
    hashes = [sha256_file(path) for path in files]
    digest = hashlib.sha256((kind + "\n" + "\n".join(hashes)).encode("ascii")).hexdigest()
    return digest[:16], digest


def _run_mux(ffmpeg: Path, video: Path, audio: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("source.muxing.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        str(ffmpeg), "-y", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-movflags", "+faststart", str(temporary),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        error = result.stderr.decode("utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"FFmpeg stream-copy mux failed ({result.returncode}):\n{error}")
    os.replace(temporary, output)


def _copy_originals(video_dir: Path, video: Path, audio: Path | None = None) -> tuple[Path, Path | None]:
    originals = video_dir / "originals"
    archived_video = originals / f"video_stream{video.suffix.lower()}"
    copy_file_atomically(video, archived_video)
    archived_audio = None
    if audio:
        archived_audio = originals / f"audio_stream{audio.suffix.lower()}"
        copy_file_atomically(audio, archived_audio)
    return archived_video, archived_audio


def _write_media_record(video_dir: Path, record: dict[str, Any]) -> None:
    atomic_write_json(video_dir / "media.json", {"media": record, "updated_at": utc_now()})


def _prepare_pair(config: AppConfig, decision: PairDecision, force: bool) -> MediaAsset:
    assert decision.selected is not None
    video, audio = decision.video, decision.selected.audio
    video_id, content_hash = _asset_identity("separate_streams", [video.path, audio.path])
    video_dir = config.data_root / "processed" / video_id
    normalized = video_dir / "normalized" / "source.mp4"
    archived_video, archived_audio = _copy_originals(video_dir, video.path, audio.path)
    previous = load_json(video_dir / "media.json", {}).get("media", {})
    valid_existing = normalized.exists() and is_valid_complete_av(probe_media(config.ffprobe, normalized))
    if force or not (valid_existing and previous.get("content_hash") == content_hash and previous.get("status") == "mux_completed"):
        _run_mux(config.ffmpeg, archived_video, archived_audio, normalized)
    normalized_probe = probe_media(config.ffprobe, normalized)
    if not is_valid_complete_av(normalized_probe):
        raise RuntimeError("Mux output verification failed: normalized source does not contain both audio and video streams.")
    record = {
        "input_type": "separate_streams", "status": "mux_completed", "content_hash": content_hash,
        "video_source": str(video.path), "audio_source": str(audio.path), "normalized_source": str(normalized),
        "originals": {"video": str(archived_video), "audio": str(archived_audio)},
        "pairing": {"status": "pair_found", "selected": decision.selected.as_dict(), "candidates": [item.as_dict() for item in decision.candidates]},
        "mux": {"mode": "stream_copy", "command": "-map 0:v:0 -map 1:a:0 -c copy", "verified_media_type": normalized_probe.media_type},
        "input_probe": {"video": video.as_dict(), "audio": audio.as_dict()}, "normalized_probe": normalized_probe.as_dict(),
    }
    _write_media_record(video_dir, record)
    return MediaAsset(video_id, content_hash, video.path.stem, video_dir, normalized, normalized_probe, record)


def _prepare_complete(config: AppConfig, info: MediaInfo) -> MediaAsset:
    video_id, content_hash = _asset_identity("complete_av", [info.path])
    video_dir = config.data_root / "processed" / video_id
    archived_video, _ = _copy_originals(video_dir, info.path)
    normalized = video_dir / "normalized" / "source.mp4"
    copy_file_atomically(archived_video, normalized)
    normalized_probe = probe_media(config.ffprobe, normalized)
    if not is_valid_complete_av(normalized_probe):
        raise RuntimeError("Complete A/V input failed normalized-source verification.")
    record = {
        "input_type": "complete_av", "status": "complete_av", "content_hash": content_hash,
        "video_source": str(info.path), "audio_source": None, "normalized_source": str(normalized),
        "originals": {"source": str(archived_video)}, "normalized_probe": normalized_probe.as_dict(),
    }
    _write_media_record(video_dir, record)
    return MediaAsset(video_id, content_hash, info.path.stem, video_dir, normalized, normalized_probe, record)


def _record_unpaired(config: AppConfig, decision: PairDecision) -> dict[str, Any]:
    video_id, content_hash = _asset_identity("video_only", [decision.video.path])
    video_dir = config.data_root / "processed" / video_id
    archived_video, _ = _copy_originals(video_dir, decision.video.path)
    record = {
        "input_type": "video_only", "status": decision.status, "content_hash": content_hash,
        "video_source": str(decision.video.path), "audio_source": None, "normalized_source": None,
        "originals": {"video": str(archived_video)}, "pairing": {"status": decision.status, "selected": None,
        "candidates": [item.as_dict() for item in decision.candidates]}, "input_probe": decision.video.as_dict(),
    }
    _write_media_record(video_dir, record)
    return record


def prepare_assets(config: AppConfig, files: list[Path], force: bool = False) -> tuple[list[MediaAsset], list[dict[str, Any]]]:
    """Probe a batch, normalize reliable A/V inputs, and persist non-terminal media states."""
    infos = [probe_media(config.ffprobe, file.resolve()) for file in files]
    assets = [_prepare_complete(config, info) for info in infos if info.media_type == "audio_video"]
    decisions = pair_streams(
        infos,
        modified_window_seconds=float(config.raw["intake"].get("modified_time_window_seconds", 900)),
        ambiguity_margin=float(config.raw["intake"].get("ambiguity_margin", 0.04)),
    )
    reports: list[dict[str, Any]] = []
    selected_audio_paths: set[Path] = set()
    for decision in decisions:
        if decision.status == "pair_found":
            asset = _prepare_pair(config, decision, force)
            assets.append(asset)
            assert decision.selected is not None
            selected_audio_paths.add(decision.selected.audio.path)
        else:
            reports.append(_record_unpaired(config, decision))
    for info in infos:
        if info.media_type == "audio_only" and info.path not in selected_audio_paths:
            reports.append({"input_type": "audio_only", "status": "audio_only", "audio_source": str(info.path), "input_probe": info.as_dict()})
        elif info.media_type == "invalid":
            reports.append({"input_type": "invalid", "status": "invalid", "source": str(info.path), "input_probe": info.as_dict()})
    return assets, reports
