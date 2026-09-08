"""Unit and integration tests for M3-03 Image Album OCR / VLM Integration.

Covers all 14 mandatory test requirements:
1. Canonical album enters visual pipeline
2. Sequence order preserved (1..N)
3. Formal archive immutability
4. OCR evidence tied to exact image provenance
5. OCR empty-text / low-confidence behavior
6. OCR backend unavailable & fallback behavior
7. Single-image failure / partial behavior
8. Optional VLM disabled
9. Optional VLM success
10. Repeated processing / cache hit (sub-second resume)
11. Image hash/config change invalidates cache
12. Video CanonicalMediaAsset rejected from album pipeline
13. Optional BGM does not affect OCR
14. Real C10 album offline smoke test
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.config import AppConfig, load_config
from src.media_adapter.models import (
    AlbumImageArtifact,
    CanonicalMediaAsset,
    CanonicalMediaType,
    UnsupportedContentTypeError,
)
from src.pipeline import process_canonical_album, run
from src.storage import load_json


class FakeOCRBackend:
    name = "fake_ocr_backend"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.fail_on_frame_id = config.get("fail_on_frame_id")
        self.empty_on_frame_id = config.get("empty_on_frame_id")

    def read_detail(self, frame: dict[str, Any]) -> dict[str, Any]:
        fid = frame.get("frame_id", "")
        if self.fail_on_frame_id and fid == self.fail_on_frame_id:
            raise RuntimeError(f"Simulated OCR failure on {fid}")
        if self.empty_on_frame_id and fid == self.empty_on_frame_id:
            return {"texts": [], "scores": [], "polygons": [], "boxes": [], "inference_seconds": 0.001}
        return {
            "texts": [f"Text in {fid}", "Confidence 0.99"],
            "scores": [0.95, 0.99],
            "polygons": [[[10, 10], [50, 10], [50, 30], [10, 30]], [[10, 40], [60, 40], [60, 60], [10, 60]]],
            "boxes": [[10, 10, 50, 30], [10, 40, 60, 60]],
            "inference_seconds": 0.005,
        }

    def read(self, frame: dict[str, Any]) -> tuple[list[str], list[float]]:
        detail = self.read_detail(frame)
        return detail["texts"], detail["scores"]

    def warmup(self, frame: dict[str, Any]) -> tuple[list[str], list[float]]:
        return self.read(frame)

    def timing(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "initialization_seconds": 0.01,
            "warmup_seconds": 0.005,
            "inference_seconds": 0.01,
            "inference_calls": 2,
        }


def _create_dummy_image(path: Path, content: bytes = b"DUMMY_IMAGE_DATA") -> tuple[Path, str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    return path, sha, len(content)


def _build_test_album_asset(
    tmp_path: Path,
    image_count: int = 3,
    cid: str = "test_album_1001",
    audio_bgm: bool = False,
) -> tuple[CanonicalMediaAsset, Path]:
    archive_dir = tmp_path / "archive" / "douyin" / cid
    images = []
    manifest_assets = []
    for i in range(1, image_count + 1):
        img_path = archive_dir / f"{cid}_img_{i:03d}.webp"
        _, sha, size = _create_dummy_image(img_path, f"IMAGE_CONTENT_{i}".encode("utf-8"))
        artifact = AlbumImageArtifact(
            sequence_index=i,
            file_name=img_path.name,
            path=img_path,
            sha256=sha,
            byte_size=size,
        )
        images.append(artifact)
        manifest_assets.append({
            "file_name": img_path.name,
            "relative_archive_path": f"douyin/{cid}/{img_path.name}",
            "role": "ArtifactRole.ALBUM_IMAGE",
            "sequence_index": i,
            "byte_size": size,
            "sha256": sha,
            "content_type": "image",
        })

    audio_path = None
    audio_sha = None
    if audio_bgm:
        audio_file = archive_dir / f"{cid}_bgm.mp3"
        _, audio_sha, audio_size = _create_dummy_image(audio_file, b"DUMMY_AUDIO_TRACK")
        audio_path = audio_file
        manifest_assets.append({
            "file_name": audio_file.name,
            "relative_archive_path": f"douyin/{cid}/{audio_file.name}",
            "role": "ArtifactRole.AUDIO_TRACK",
            "sequence_index": 0,
            "byte_size": audio_size,
            "sha256": audio_sha,
            "content_type": "audio",
        })

    manifest = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": cid,
        "archived_at": "2026-09-08T00:00:00Z",
        "source_provenance": {"task_id": "test_task", "scope_id": "test_scope"},
        "asset_count": len(manifest_assets),
        "assets": manifest_assets,
    }
    manifest_path = archive_dir / "asset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id=cid,
        canonical_id=f"douyin_{cid}",
        content_type=CanonicalMediaType.IMAGE_ALBUM,
        asset_root=archive_dir,
        manifest_path=manifest_path,
        album_images=images,
        audio_path=audio_path,
        audio_sha256=audio_sha,
        source_provenance={"task_id": "test_task", "scope_id": "test_scope"},
        source_metadata={"title": f"Test Title for {cid}", "author": "Test Author"},
        archived_at="2026-09-08T00:00:00Z",
    )
    return asset, archive_dir


def _build_test_config(tmp_path: Path) -> AppConfig:
    base_config = load_config("config/config.json")
    raw = copy.deepcopy(base_config.raw)
    raw["paths"]["data_root"] = str(tmp_path / "data")
    raw["visual_evidence"]["ocr"]["backend"] = "fake_ocr_backend"
    raw["visual_evidence"]["vlm"]["backend"] = "disabled"
    return AppConfig(raw=raw, path=base_config.path)



# =============================================================================
# Test Cases
# =============================================================================


def test_01_canonical_album_enters_visual_pipeline(tmp_path: Path) -> None:
    """Requirement 1: Canonical album asset enters visual pipeline directly."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req1")
    config = _build_test_config(tmp_path)

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    assert result_dir.is_dir()
    assert (result_dir / "processing.json").is_file()
    assert (result_dir / "metadata.json").is_file()
    assert (result_dir / "media.json").is_file()
    assert (result_dir / "visual" / "visual_transcript.json").is_file()
    assert (result_dir / "visual" / "ocr.json").is_file()
    assert (result_dir / "visual" / "requests.json").is_file()
    assert (result_dir / "visual" / "visual.md").is_file()

    proc = load_json(result_dir / "processing.json")
    assert proc["status"] == "PROCESSING"
    assert proc["stages"]["source"]["status"] == "completed"
    assert proc["stages"]["visual"]["status"] == "completed"
    assert proc["stages"]["audio"]["status"] == "skipped"
    assert proc["stages"]["asr"]["status"] == "skipped"


def test_02_sequence_order_preserved(tmp_path: Path) -> None:
    """Requirement 2: Album images are processed in strict sequence order 1..N even if shuffled."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=3, cid="album_req2")
    config = _build_test_config(tmp_path)

    # Shuffled images input
    shuffled_images = [asset.album_images[2], asset.album_images[0], asset.album_images[1]]
    shuffled_asset = CanonicalMediaAsset(
        platform=asset.platform,
        platform_content_id=asset.platform_content_id,
        canonical_id=asset.canonical_id,
        content_type=CanonicalMediaType.IMAGE_ALBUM,
        asset_root=asset.asset_root,
        manifest_path=asset.manifest_path,
        album_images=shuffled_images,
    )

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, shuffled_asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    requests = vt["requests"]
    assert len(requests) == 3
    assert [r["sequence_index"] for r in requests] == [1, 2, 3]
    assert [r["id"] for r in requests] == ["vr_img_001", "vr_img_002", "vr_img_003"]

    evidence = [e for e in vt["visual_evidence"] if e["source_type"] == "visual_ocr"]
    assert [e["sequence_index"] for e in evidence] == [1, 2, 3]


def test_03_archive_immutable(tmp_path: Path) -> None:
    """Requirement 3: Source archive files remain strictly read-only and unmodified."""
    asset, archive_dir = _build_test_album_asset(tmp_path, image_count=3, cid="album_req3")
    config = _build_test_config(tmp_path)

    files_before = {f.name: (f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest()) for f in archive_dir.glob("*")}

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        process_canonical_album(config, asset, force=True)

    files_after = {f.name: (f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest()) for f in archive_dir.glob("*")}
    assert files_before == files_after
    assert len(list(archive_dir.glob("*"))) == 4  # 3 images + 1 manifest


def test_04_ocr_evidence_tied_to_exact_image(tmp_path: Path) -> None:
    """Requirement 4: Each OCR evidence item has unambiguous provenance tying to exact image file & SHA."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req4")
    config = _build_test_config(tmp_path)

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    for item in vt["visual_evidence"]:
        if item["source_type"] != "visual_ocr":
            continue
        assert item["canonical_id"] == asset.canonical_id
        assert item["platform"] == "douyin"
        assert item["platform_content_id"] == "album_req4"
        assert item["sequence_index"] in (1, 2)
        assert item["source_image_file"].endswith(".webp")
        assert len(item["source_image_sha256"]) == 64
        assert Path(item["source_image_path"]).is_file()
        assert "lines" in item
        assert len(item["lines"]) > 0
        assert "polygon" in item["lines"][0]
        assert "box" in item["lines"][0]


def test_05_ocr_empty_text_behavior(tmp_path: Path) -> None:
    """Requirement 5: Images with no text or low confidence produce insufficient_ocr status gracefully."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req5")
    config = _build_test_config(tmp_path)
    config.raw["visual_evidence"]["ocr"]["empty_on_frame_id"] = "img_002"

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    e1 = next(e for e in vt["visual_evidence"] if e["sequence_index"] == 1 and e["source_type"] == "visual_ocr")
    e2 = next(e for e in vt["visual_evidence"] if e["sequence_index"] == 2 and e["source_type"] == "visual_ocr")
    assert e1["status"] == "completed"
    assert e2["status"] == "insufficient_ocr"
    assert e2["text"] == ""


def test_06_ocr_backend_unavailable_behavior(tmp_path: Path) -> None:
    """Requirement 6: When OCR backend executable is missing and no fallback, raises cleanly."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=1, cid="album_req6")
    config = _build_test_config(tmp_path)
    config.raw["visual_evidence"]["ocr"]["backend"] = "paddleocr_gpu_worker"
    config.raw["visual_evidence"]["ocr"]["python_executable"] = "non_existent_python_path.exe"
    config.raw["visual_evidence"]["ocr"]["fallback"] = None

    with pytest.raises(RuntimeError, match="GPU OCR Python executable was not found"):
        process_canonical_album(config, asset, force=True)


def test_07_single_image_failure_partial_behavior(tmp_path: Path) -> None:
    """Requirement 7: If 1 image fails OCR, remaining images succeed and stage marks partial."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=3, cid="album_req7")
    config = _build_test_config(tmp_path)
    config.raw["visual_evidence"]["ocr"]["fail_on_frame_id"] = "img_002"

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    summary = vt["ocr_summary"]
    assert summary["total_images"] == 3
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["overall_status"] == "partial"

    e2 = next(e for e in vt["visual_evidence"] if e["sequence_index"] == 2 and e["source_type"] == "visual_ocr")
    assert e2["status"] == "failed"
    assert "Simulated OCR failure" in e2["error"]


def test_08_optional_vlm_disabled(tmp_path: Path) -> None:
    """Requirement 8: Optional VLM disabled results in 0 VLM calls and clean OCR completion."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req8")
    config = _build_test_config(tmp_path)
    config.raw["visual_evidence"]["vlm"] = {"backend": "disabled"}

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    assert vt["timing"]["vlm"]["calls"] == 0
    vlm_evidence = [e for e in vt["visual_evidence"] if e["source_type"] == "visual_vlm"]
    assert len(vlm_evidence) == 0


def test_09_optional_vlm_success_mock(tmp_path: Path) -> None:
    """Requirement 9: Optional VLM triggered and produces structured understanding evidence."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req9")
    config = _build_test_config(tmp_path)
    config.raw["visual_evidence"]["vlm"] = {
        "backend": "openai_compatible",
        "base_url": "http://mock-vlm:1234/v1",
        "identifier": "mock-vlm-model",
    }
    config.raw["visual_evidence"]["vlm_policy"] = "always"

    mock_vlm_instance = MagicMock()
    mock_vlm_instance.answer.return_value = (
        {"status": "resolved", "answer": "Mock VLM semantic description of image", "confidence": 0.96},
        0.12,
    )
    mock_vlm_instance.unload.return_value = {}

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend), patch(
        "src.visual.album.OpenAICompatibleVLMBackend", return_value=mock_vlm_instance
    ):
        result_dir = process_canonical_album(config, asset, force=True)

    vt = load_json(result_dir / "visual" / "visual_transcript.json")
    vlm_evidence = [e for e in vt["visual_evidence"] if e["source_type"] == "visual_vlm"]
    assert len(vlm_evidence) == 2
    assert vlm_evidence[0]["status"] == "resolved"
    assert "Mock VLM semantic description" in vlm_evidence[0]["answer"]
    assert vlm_evidence[0]["confidence"] == 0.96


def test_10_repeated_processing_and_cache(tmp_path: Path) -> None:
    """Requirement 10: Repeated execution with unchanged inputs skips inference in sub-second time."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req10")
    config = _build_test_config(tmp_path)

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        # First run
        result_dir = process_canonical_album(config, asset, force=True)

        # Second run (resume)
        t0 = time.perf_counter()
        res2 = process_canonical_album(config, asset, force=False)
        elapsed = time.perf_counter() - t0

    assert res2 == result_dir
    assert elapsed < 0.2  # sub-second resume


def test_11_image_hash_or_config_change_invalidates_cache(tmp_path: Path) -> None:
    """Requirement 11: Modifying OCR config invalidates cache and triggers re-run."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req11")
    config = _build_test_config(tmp_path)

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        process_canonical_album(config, asset, force=True)
        vt_before = load_json(config.data_root / "processed" / asset.canonical_id / "visual" / "visual_transcript.json")

        # Change config
        config.raw["visual_evidence"]["ocr"]["minimum_confidence"] = 0.88
        process_canonical_album(config, asset, force=False)
        vt_after = load_json(config.data_root / "processed" / asset.canonical_id / "visual" / "visual_transcript.json")

    assert vt_before["pipeline_fingerprint"] != vt_after["pipeline_fingerprint"]


def test_12_video_canonical_asset_rejected(tmp_path: Path) -> None:
    """Requirement 12: Passing video asset to process_canonical_album raises UnsupportedContentTypeError."""
    config = _build_test_config(tmp_path)
    video_asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id="vid_123",
        canonical_id="douyin_vid_123",
        content_type=CanonicalMediaType.VIDEO,
        asset_root=tmp_path,
        manifest_path=tmp_path / "asset_manifest.json",
        video_path=tmp_path / "sample.mp4",
        video_sha256="abc",
        video_size_bytes=1000,
    )

    with pytest.raises(UnsupportedContentTypeError, match="Cannot run album visual pipeline on non-album asset"):
        process_canonical_album(config, video_asset)


def test_13_optional_bgm_does_not_affect_ocr(tmp_path: Path) -> None:
    """Requirement 13: Optional BGM audio track is preserved in metadata/media without triggering ASR."""
    asset, _ = _build_test_album_asset(tmp_path, image_count=2, cid="album_req13", audio_bgm=True)
    config = _build_test_config(tmp_path)

    with patch("src.visual.album.PaddleOCRBackend", FakeOCRBackend):
        result_dir = process_canonical_album(config, asset, force=True)

    metadata = load_json(result_dir / "metadata.json")
    media = load_json(result_dir / "media.json")
    assert metadata["audio_path"] is not None
    assert metadata["audio_sha256"] is not None
    assert media["audio_track"]["path"] is not None

    proc = load_json(result_dir / "processing.json")
    assert proc["stages"]["asr"]["status"] == "skipped"
    assert proc["stages"]["asr"]["reason"] == "image_album"


def test_14_real_c10_album_offline_smoke() -> None:
    """Requirement 14: Real C10 formal image album processes offline on real GPU via CanonicalMediaAssetAdapter."""
    from src.downloader.promoter import verify_archived_asset
    from src.media_adapter import CanonicalMediaAssetAdapter

    c10_archive_dir = Path("archive/douyin/7682038498466993905")
    if not c10_archive_dir.is_dir():
        pytest.skip("Real C10 image album directory is not present.")

    # Archive immutability check before
    ver_before = verify_archived_asset(c10_archive_dir)
    assert ver_before.valid is True

    adapter = CanonicalMediaAssetAdapter(
        archive_root=Path("archive"),
        metadata_db_path=Path("data/metadata.db"),
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7682038498466993905", platform="douyin")
    assert asset.is_album is True
    assert len(asset.album_images) == 3

    config = load_config("config/config.json")
    result_dir = process_canonical_album(config, asset, force=False)

    assert result_dir.is_dir()
    vt_path = result_dir / "visual" / "visual_transcript.json"
    assert vt_path.is_file()

    vt = load_json(vt_path)
    assert vt["content_type"] == "image_album"
    assert vt["image_count"] == 3
    assert vt["ocr_summary"]["total_images"] == 3
    assert vt["ocr_summary"]["completed"] == 2

    # Check that image 1 extracted text contains logitech / INAMAX
    e1 = next(e for e in vt["visual_evidence"] if e["sequence_index"] == 1 and e["source_type"] == "visual_ocr")
    assert "logitech" in e1["text"].lower() or "inamax" in e1["text"].lower()

    # Archive immutability check after
    ver_after = verify_archived_asset(c10_archive_dir)
    assert ver_after.valid is True
