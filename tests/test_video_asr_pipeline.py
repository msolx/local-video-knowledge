"""Unit and integration tests for Milestone M3 Video / ASR Integration (M3-02).

Validates:
1. Canonical video enters ASR pipeline without manual incoming drops.
2. Formal archive remains strictly untouched and immutable.
3. Audio extraction source is strictly PRIMARY_VIDEO.
4. No-audio video produces explicit NO_AUDIO status and no fake transcript.
5. Transcript segment ordering is strictly chronological with valid sequential IDs.
6. Timestamp validity (0 <= start <= end <= duration).
7. Provenance binding (platform, content_id, canonical_id, source_video, content_hash, ASR model).
8. Repeated processing and resume behavior (force=False skips, force=True re-runs).
9. Missing ASR dependency / failure behavior.
10. Invalid / non-video CanonicalMediaAsset rejection.
11. Real C10 video offline ASR integration.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import pytest

from src.config import load_config, AppConfig
from src.media_adapter import (
    CanonicalMediaAsset,
    CanonicalMediaType,
    CanonicalMediaAssetAdapter,
    UnsupportedContentTypeError,
)
from src.pipeline import process_canonical_asset, process_asset


def _make_test_config(tmp_path: Path) -> AppConfig:
    """Helper to create AppConfig with data_root pointed at tmp_path."""
    cfg = load_config("config/config.json")
    raw = copy.deepcopy(cfg.raw)
    raw["paths"]["data_root"] = str(tmp_path)
    return AppConfig(raw=raw, path=cfg.path)


def _create_synthetic_video(output_path: Path, ffmpeg_path: Path, has_audio: bool = True, duration: int = 1) -> Path:
    """Create a minimal valid synthetic MP4 file with or without audio track."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if has_audio:
        cmd = [
            str(ffmpeg_path), "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
            "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(output_path),
        ]
    else:
        cmd = [
            str(ffmpeg_path), "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return output_path


def _create_formal_video_asset(
    archive_root: Path,
    content_id: str,
    ffmpeg_path: Path,
    has_audio: bool = True,
    duration: int = 1,
) -> tuple[CanonicalMediaAsset, str]:
    """Create a formal archive structure matching M2 D07."""
    content_dir = archive_root / "douyin" / content_id
    video_file = content_dir / f"{content_id}.mp4"
    _create_synthetic_video(video_file, ffmpeg_path, has_audio=has_audio, duration=duration)

    sha = hashlib.sha256(video_file.read_bytes()).hexdigest()
    size = video_file.stat().st_size

    manifest = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": content_id,
        "archived_at": "2026-09-08T12:00:00Z",
        "source_provenance": {
            "task_id": f"dl_{content_id}",
            "scope_id": "douyin:test_scope",
        },
        "asset_count": 1,
        "assets": [
            {
                "file_name": f"{content_id}.mp4",
                "role": "PRIMARY_VIDEO",
                "byte_size": size,
                "sha256": sha,
                "content_type": "video",
            }
        ],
    }
    manifest_path = content_dir / "asset_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(archive_root=archive_root, validate_hashes=True)
    asset = adapter.load_from_dir(content_dir)
    return asset, sha


# =============================================================================
# 1. Canonical Video Enters ASR Pipeline & Untouched Archive
# =============================================================================


def test_01_canonical_video_enters_asr_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)
    processed_root = tmp_path / "processed"

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_01", config.ffmpeg, has_audio=True)

    def mock_transcribe(audio_path: Path, asr_config: dict, temp_out: Path):
        return [
            {"start": 0.0, "end": 0.5, "text": "你好"},
            {"start": 0.5, "end": 1.0, "text": "世界"},
        ], {"backend": "mock_whisper", "model": "mock-v1"}

    monkeypatch.setattr("src.pipeline.transcribe", mock_transcribe)

    video_dir = process_canonical_asset(config, asset, force=True, stop_after="asr")
    assert video_dir.exists()
    assert video_dir == processed_root / "douyin_vid_01"

    assert (video_dir / "audio.wav").exists()
    assert (video_dir / "transcript.json").exists()
    assert (video_dir / "transcript.md").exists()
    assert (video_dir / "metadata.json").exists()
    assert (video_dir / "processing.json").exists()

    state = json.loads((video_dir / "processing.json").read_text(encoding="utf-8"))
    assert state["stages"]["source"]["status"] == "completed"
    assert state["stages"]["audio"]["status"] == "completed"
    assert state["stages"]["asr"]["status"] == "completed"
    assert state["stages"]["visual"]["status"] == "pending"
    assert state["stages"]["knowledge"]["status"] == "pending"


def test_02_formal_archive_remains_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha_before = _create_formal_video_asset(tmp_path / "archive", "vid_immutability", config.ffmpeg, has_audio=True)

    archive_dir = asset.asset_root
    files_before = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()) for p in archive_dir.iterdir()}

    monkeypatch.setattr(
        "src.pipeline.transcribe",
        lambda a, c, t: ([{"start": 0.0, "end": 1.0, "text": "测试"}], {"backend": "mock"}),
    )

    process_canonical_asset(config, asset, force=True, stop_after="asr")

    files_after = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()) for p in archive_dir.iterdir()}

    assert files_before == files_after
    assert hashlib.sha256(asset.video_path.read_bytes()).hexdigest() == sha_before


# =============================================================================
# 3. Audio Extraction Source is PRIMARY_VIDEO
# =============================================================================


def test_03_audio_extraction_source_is_primary_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_audio_source", config.ffmpeg, has_audio=True)

    actual_inputs = []
    real_run = __import__("src.pipeline", fromlist=["_run"])._run

    def intercept_run(cmd: list[str]):
        if "-vn" in cmd:
            for i, arg in enumerate(cmd):
                if arg == "-i":
                    actual_inputs.append(cmd[i + 1])
        return real_run(cmd)

    monkeypatch.setattr("src.pipeline._run", intercept_run)
    monkeypatch.setattr(
        "src.pipeline.transcribe",
        lambda a, c, t: ([{"start": 0.0, "end": 1.0, "text": "测试"}], {"backend": "mock"}),
    )

    process_canonical_asset(config, asset, force=True, stop_after="asr")

    assert len(actual_inputs) == 1
    assert Path(actual_inputs[0]).resolve() == asset.video_path.resolve()


# =============================================================================
# 4. No-Audio Video Behavior
# =============================================================================


def test_04_no_audio_video_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_silent", config.ffmpeg, has_audio=False)

    def fail_if_transcribed(*args, **kwargs):
        raise AssertionError("ASR transcribe should never be called for video without audio track!")

    monkeypatch.setattr("src.pipeline.transcribe", fail_if_transcribed)

    video_dir = process_canonical_asset(config, asset, force=True, stop_after="asr")

    state = json.loads((video_dir / "processing.json").read_text(encoding="utf-8"))
    assert state["stages"]["audio"]["status"] == "skipped"
    assert state["stages"]["audio"]["reason"] == "NO_AUDIO"
    assert state["stages"]["asr"]["status"] == "skipped"
    assert state["stages"]["asr"]["reason"] == "NO_AUDIO"

    transcript = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["status"] == "NO_AUDIO"
    assert transcript["segments"] == []
    assert transcript["language"] is None

    transcript_md = (video_dir / "transcript.md").read_text(encoding="utf-8")
    assert "NO_AUDIO" in transcript_md


# =============================================================================
# 5. Transcript Segment Ordering & 6. Timestamp Validity
# =============================================================================


def test_05_and_06_segment_ordering_and_timestamp_validity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_timing", config.ffmpeg, has_audio=True)

    monkeypatch.setattr(
        "src.pipeline.transcribe",
        lambda a, c, t: (
            [
                {"start": 0.0, "end": 0.3, "text": "第1句"},
                {"start": 0.3, "end": 0.7, "text": "第2句"},
                {"start": 0.7, "end": 1.0, "text": "第3句"},
            ],
            {"backend": "mock"},
        ),
    )

    video_dir = process_canonical_asset(config, asset, force=True, stop_after="asr")
    transcript = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    segments = transcript["segments"]

    assert len(segments) == 3
    for i, seg in enumerate(segments):
        assert seg["id"] == f"seg_{i+1:06d}"
        assert 0.0 <= seg["start"] <= seg["end"] <= 1.5
        if i > 0:
            assert seg["start"] >= segments[i - 1]["start"]


# =============================================================================
# 7. Provenance Binding
# =============================================================================


def test_07_provenance_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_prov", config.ffmpeg, has_audio=True)

    monkeypatch.setattr(
        "src.pipeline.transcribe",
        lambda a, c, t: ([{"start": 0.0, "end": 1.0, "text": "内容"}], {"backend": "mock_fw", "model": "large-v3"}),
    )

    video_dir = process_canonical_asset(config, asset, force=True, stop_after="asr")
    transcript = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    metadata = json.loads((video_dir / "metadata.json").read_text(encoding="utf-8"))

    assert transcript["canonical_id"] == "douyin_vid_prov"
    assert transcript["platform"] == "douyin"
    assert transcript["platform_content_id"] == "vid_prov"
    assert transcript["content_hash"] == sha
    assert transcript["source_video"] == str(asset.video_path)
    assert transcript["provenance"]["backend"] == "mock_fw"

    assert metadata["source"]["platform"] == "douyin"
    assert metadata["source"]["platform_content_id"] == "vid_prov"
    assert metadata["source"]["source_type"] == "canonical_formal_asset"


# =============================================================================
# 8. Repeated Processing / Resume Behavior
# =============================================================================


def test_08_repeated_processing_and_resume_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_resume", config.ffmpeg, has_audio=True)

    call_count = 0
    asr_settings = config.raw["asr"].get("faster_whisper", {})

    def counting_transcribe(*args):
        nonlocal call_count
        call_count += 1
        return [{"start": 0.0, "end": 1.0, "text": "转写"}], {"backend": "faster_whisper", "model": "large-v3", "settings": asr_settings}

    monkeypatch.setattr("src.pipeline.transcribe", counting_transcribe)

    video_dir = process_canonical_asset(config, asset, force=False, stop_after="asr")
    assert call_count == 1
    t_mtime1 = (video_dir / "transcript.json").stat().st_mtime_ns

    video_dir_2 = process_canonical_asset(config, asset, force=False, stop_after="asr")
    assert call_count == 1
    t_mtime2 = (video_dir_2 / "transcript.json").stat().st_mtime_ns
    assert t_mtime1 == t_mtime2

    process_canonical_asset(config, asset, force=True, stop_after="asr")
    assert call_count == 2


# =============================================================================
# 9. Missing ASR Dependency / Failure Behavior
# =============================================================================


def test_09_missing_asr_dependency_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path)

    asset, sha = _create_formal_video_asset(tmp_path / "archive", "vid_fail", config.ffmpeg, has_audio=True)

    def failing_transcribe(*args):
        raise RuntimeError("Mock faster-whisper CUDA out of memory error!")

    monkeypatch.setattr("src.pipeline.transcribe", failing_transcribe)

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        process_canonical_asset(config, asset, force=True, stop_after="asr")

    video_dir = tmp_path / "processed" / "douyin_vid_fail"
    state = json.loads((video_dir / "processing.json").read_text(encoding="utf-8"))
    assert state["status"] == "FAILED"
    assert state["stages"]["asr"]["status"] == "failed"
    assert "CUDA out of memory" in state["stages"]["asr"]["error"]


# =============================================================================
# 10. Invalid / Non-Video CanonicalMediaAsset Rejection
# =============================================================================


def test_10_invalid_or_non_video_canonical_asset_rejection(tmp_path: Path) -> None:
    config = load_config("config/config.example.json")
    non_video_asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id="album_999",
        content_type=CanonicalMediaType.IMAGE_ALBUM,
        canonical_id="douyin_album_999",
        asset_root=tmp_path,
        manifest_path=tmp_path / "asset_manifest.json",
    )

    with pytest.raises(UnsupportedContentTypeError, match="Cannot run video pipeline on non-video asset"):
        process_canonical_asset(config, non_video_asset)


# =============================================================================
# 11. Real C10 Video Offline ASR Integration Smoke Test
# =============================================================================


@pytest.mark.skipif(
    not Path("archive/douyin/7681603850364521734/asset_manifest.json").exists(),
    reason="Real C10 formal video asset not found in local workspace.",
)
def test_11_real_c10_video_offline_asr_integration() -> None:
    config = load_config("config/config.json")
    adapter = CanonicalMediaAssetAdapter(
        archive_root=Path("archive"),
        metadata_db_path=Path("data/metadata.db"),
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7681603850364521734")

    assert asset.is_video is True
    sha_before = asset.video_sha256

    video_dir = asset.process_asr(config, force=False)
    assert video_dir.exists()

    audio_path = video_dir / "audio.wav"
    transcript_path = video_dir / "transcript.json"
    transcript_md_path = video_dir / "transcript.md"
    metadata_path = video_dir / "metadata.json"

    assert audio_path.exists()
    assert transcript_path.exists()
    assert transcript_md_path.exists()
    assert metadata_path.exists()

    transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert transcript_payload["canonical_id"] == "douyin_7681603850364521734"
    assert transcript_payload["language"] == "zh"
    assert len(transcript_payload["segments"]) > 100
    assert transcript_payload["segments"][0]["id"] == "seg_000001"
    assert transcript_payload["segments"][0]["start"] >= 0.0

    sha_after = hashlib.sha256(asset.video_path.read_bytes()).hexdigest()
    assert sha_before == sha_after
