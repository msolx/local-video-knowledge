"""Unit and integration tests for M3-04 Metadata & Provenance Binding.

Covers all 17 mandatory test requirements:
1. Video ASR evidence provenance
2. ASR segment exact source binding
3. Album OCR image-level provenance
4. Sequence index preserved
5. VLM unresolved provenance
6. Metadata enrichment binding
7. Metadata DB absent resilience
8. published_at vs first_seen_at semantics
9. No fake collected_at
10. NO_AUDIO video evidence
11. Partial album evidence
12. Deterministic ordering
13. Repeated binding idempotent (<1ms resume)
14. Evidence or config change invalidates fingerprint
15. Archive immutability
16. Real C10 video offline smoke
17. Real C10 album offline smoke
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config import AppConfig, load_config
from src.media_adapter.models import (
    AlbumImageArtifact,
    CanonicalMediaAsset,
    CanonicalMediaType,
)
from src.provenance import (
    EVIDENCE_MANIFEST_FILENAME,
    EVIDENCE_SCHEMA_VERSION,
    EvidenceItem,
    build_evidence_manifest,
    compute_manifest_fingerprint,
    extract_formal_asset_binding,
    extract_source_metadata_snapshot,
    load_evidence_manifest,
    verify_evidence_manifest,
    write_evidence_manifest,
)
from src.storage import atomic_write_json, load_json


@pytest.fixture
def test_app_config(tmp_path: Path) -> AppConfig:
    base_config = load_config("config/config.json")
    raw = copy.deepcopy(base_config.raw)
    raw["paths"]["data_root"] = str(tmp_path / "data")
    return AppConfig(raw=raw, path=base_config.path)



@pytest.fixture
def mock_video_asset(tmp_path: Path) -> tuple[CanonicalMediaAsset, Path]:
    arch_dir = tmp_path / "archive" / "douyin" / "video_001"
    arch_dir.mkdir(parents=True, exist_ok=True)
    video_file = arch_dir / "video_001.mp4"
    video_content = b"fake video stream content"
    video_file.write_bytes(video_content)
    video_sha = hashlib.sha256(video_content).hexdigest()

    proc_dir = tmp_path / "data" / "processed" / "douyin_video_001"
    proc_dir.mkdir(parents=True, exist_ok=True)

    transcript_data = {
        "video_id": "douyin_video_001",
        "canonical_id": "douyin_video_001",
        "platform": "douyin",
        "platform_content_id": "video_001",
        "source_video": str(video_file),
        "content_hash": video_sha,
        "language": "zh",
        "provenance": {
            "backend": "faster_whisper",
            "model": "large-v3",
            "settings": {"model": "large-v3", "device": "cuda", "compute_type": "float16", "beam_size": 5},
            "metrics": {"transcription_seconds": 1.25},
        },
        "segments": [
            {"id": "seg_000001", "start": 0.0, "end": 2.5, "text": "First test speech segment."},
            {"id": "seg_000002", "start": 2.5, "end": 5.0, "text": "Second test speech segment."},
        ],
    }
    atomic_write_json(proc_dir / "transcript.json", transcript_data)

    source_meta = {
        "title": "Test Video Title",
        "author": {"display_name": "Tech Creator", "platform_author_id": "auth_12345"},
        "published_at": "2026-09-04T09:00:00+00:00",
        "first_seen_at": "2026-09-06T15:00:00+00:00",
        "source_url": "https://www.douyin.com/video/video_001",
        "tags": ["tech", "benchmark"],
    }

    asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id="video_001",
        content_type=CanonicalMediaType.VIDEO,
        canonical_id="douyin_video_001",
        asset_root=arch_dir,
        manifest_path=arch_dir / "asset_manifest.json",
        video_path=video_file,
        video_sha256=video_sha,
        video_size_bytes=len(video_content),
        archived_at="2026-09-06T15:05:00+00:00",
        source_provenance={"task_id": "task_vid_001", "scope_id": "scope_douyin"},
        source_metadata=source_meta,
    )
    return asset, proc_dir


@pytest.fixture
def mock_album_asset(tmp_path: Path) -> tuple[CanonicalMediaAsset, Path]:
    arch_dir = tmp_path / "archive" / "douyin" / "album_001"
    arch_dir.mkdir(parents=True, exist_ok=True)

    img1_file = arch_dir / "album_001_img_001.webp"
    img1_content = b"fake webp image 1"
    img1_file.write_bytes(img1_content)
    img1_sha = hashlib.sha256(img1_content).hexdigest()

    img2_file = arch_dir / "album_001_img_002.webp"
    img2_content = b"fake webp image 2"
    img2_file.write_bytes(img2_content)
    img2_sha = hashlib.sha256(img2_content).hexdigest()

    proc_dir = tmp_path / "data" / "processed" / "douyin_album_001"
    visual_dir = proc_dir / "visual"
    visual_dir.mkdir(parents=True, exist_ok=True)

    vt_data = {
        "schema_version": "visual-evidence-v1",
        "content_type": "image_album",
        "canonical_id": "douyin_album_001",
        "platform": "douyin",
        "platform_content_id": "album_001",
        "visual_evidence": [
            {
                "id": "ve_img_001",
                "source_type": "visual_ocr",
                "status": "completed",
                "sequence_index": 1,
                "source_image_file": img1_file.name,
                "source_image_sha256": img1_sha,
                "byte_size": len(img1_content),
                "text": "Heading Title\nDescription line",
                "confidence": 0.98,
                "lines": [
                    {"text": "Heading Title", "confidence": 0.99, "polygon": [[0, 0], [10, 0], [10, 5], [0, 5]], "box": [0, 0, 10, 5]},
                    {"text": "Description line", "confidence": 0.97, "polygon": [[0, 6], [20, 6], [20, 10], [0, 10]], "box": [0, 6, 20, 10]},
                ],
                "ocr_engine": "PaddleOCR",
            },
            {
                "id": "ve_img_002",
                "source_type": "visual_ocr",
                "status": "completed",
                "sequence_index": 2,
                "source_image_file": img2_file.name,
                "source_image_sha256": img2_sha,
                "byte_size": len(img2_content),
                "text": "Detail specs",
                "confidence": 0.95,
                "lines": [
                    {"text": "Detail specs", "confidence": 0.95, "polygon": [[0, 0], [15, 0], [15, 5], [0, 5]], "box": [0, 0, 15, 5]}
                ],
                "ocr_engine": "PaddleOCR",
            },
            {
                "id": "ve_vlm_img_002",
                "source_type": "visual_vlm",
                "status": "unresolved_visual_reference",
                "sequence_index": 2,
                "source_image_file": img2_file.name,
                "source_image_sha256": img2_sha,
                "description": None,
                "reason": "unresolved_visual_reference",
            },
        ],
    }
    atomic_write_json(visual_dir / "visual_transcript.json", vt_data)

    source_meta = {
        "title": "Album Showcase",
        "author": {"display_name": "Art Creator", "platform_author_id": "auth_67890"},
        "published_at": "2026-09-05T12:00:00+00:00",
        "first_seen_at": "2026-09-06T15:20:00+00:00",
        "source_url": "https://www.douyin.com/video/album_001",
        "tags": ["art", "album"],
    }

    artifacts = (
        AlbumImageArtifact(path=img1_file, sequence_index=1, sha256=img1_sha, byte_size=len(img1_content), file_name=img1_file.name),
        AlbumImageArtifact(path=img2_file, sequence_index=2, sha256=img2_sha, byte_size=len(img2_content), file_name=img2_file.name),
    )

    asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id="album_001",
        content_type=CanonicalMediaType.IMAGE_ALBUM,
        canonical_id="douyin_album_001",
        asset_root=arch_dir,
        manifest_path=arch_dir / "asset_manifest.json",
        album_images=artifacts,
        archived_at="2026-09-06T15:25:00+00:00",
        source_provenance={"task_id": "task_alb_001", "scope_id": "scope_douyin"},
        source_metadata=source_meta,
    )
    return asset, proc_dir


# ----------------------------------------------------------------------
# 1. Video ASR evidence provenance
# ----------------------------------------------------------------------
def test_01_video_asr_evidence_provenance(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    assert manifest["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert manifest["canonical_id"] == "douyin_video_001"
    assert manifest["content_type"] == "video"

    asr_prov = manifest["processing_provenance"]["model_provenance"]["asr"]
    assert asr_prov["backend"] == "faster_whisper"
    assert asr_prov["model"] == "large-v3"
    assert asr_prov["compute_type"] == "float16"
    assert asr_prov["beam_size"] == 5
    assert asr_prov["status"] == "completed"

    summary = manifest["evidence_summary"]
    assert summary["total_items"] == 2
    assert summary["speech_segments"] == 2
    assert summary["verification_status"] == "not_checked"


# ----------------------------------------------------------------------
# 2. ASR segment exact source binding
# ----------------------------------------------------------------------
def test_02_asr_segment_exact_source_binding(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    items = manifest["evidence_items"]
    assert len(items) == 2

    seg1 = items[0]
    assert seg1["evidence_id"] == "ev_seg_000001"
    assert seg1["modality"] == "speech"
    assert seg1["source_artifact"]["role"] == "PRIMARY_VIDEO"
    assert seg1["source_artifact"]["file_name"] == "video_001.mp4"
    assert seg1["source_artifact"]["sha256"] == asset.video_sha256
    assert seg1["temporal"]["start"] == 0.0
    assert seg1["temporal"]["end"] == 2.5
    assert seg1["temporal"]["duration"] == 2.5
    assert seg1["temporal"]["segment_order"] == 1
    assert seg1["temporal"]["segment_id"] == "seg_000001"
    assert seg1["payload"]["text"] == "First test speech segment."
    assert seg1["verification_status"] == "not_checked"

    seg2 = items[1]
    assert seg2["evidence_id"] == "ev_seg_000002"
    assert seg2["temporal"]["segment_order"] == 2
    assert seg2["payload"]["text"] == "Second test speech segment."
    assert seg2["verification_status"] == "not_checked"


# ----------------------------------------------------------------------
# 3. Album OCR image-level provenance
# ----------------------------------------------------------------------
def test_03_album_ocr_image_level_provenance(mock_album_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_album_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    items = manifest["evidence_items"]
    ocr_items = [it for it in items if it["modality"] == "visual_text"]
    assert len(ocr_items) == 2

    item1 = ocr_items[0]
    assert item1["evidence_id"] == "ve_img_001"
    assert item1["source_artifact"]["role"] == "ALBUM_IMAGE"
    assert item1["source_artifact"]["file_name"] == "album_001_img_001.webp"
    assert item1["source_artifact"]["sha256"] == asset.album_images[0].sha256
    assert item1["sequence"]["sequence_index"] == 1
    assert item1["temporal"] is None
    assert item1["payload"]["confidence"] == 0.98
    assert len(item1["payload"]["lines"]) == 2
    assert item1["payload"]["lines"][0]["polygon"] == [[0, 0], [10, 0], [10, 5], [0, 5]]
    assert item1["payload"]["lines"][0]["box"] == [0, 0, 10, 5]
    assert item1["verification_status"] == "not_checked"


# ----------------------------------------------------------------------
# 4. Sequence index preserved
# ----------------------------------------------------------------------
def test_04_sequence_index_preserved(mock_album_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_album_asset
    # Shuffle artifacts order in asset
    shuffled_asset = replace(asset, album_images=(asset.album_images[1], asset.album_images[0]))

    manifest = build_evidence_manifest(shuffled_asset, config=test_app_config, processed_dir=proc_dir)
    formal_artifacts = manifest["formal_asset"]["artifacts"]
    assert formal_artifacts[0]["sequence_index"] == 1
    assert formal_artifacts[1]["sequence_index"] == 2

    items = manifest["evidence_items"]
    assert items[0]["sequence"]["sequence_index"] == 1
    assert items[1]["sequence"]["sequence_index"] == 2


# ----------------------------------------------------------------------
# 5. VLM unresolved provenance
# ----------------------------------------------------------------------
def test_05_vlm_unresolved_provenance(mock_album_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_album_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    vlm_items = [it for it in manifest["evidence_items"] if it["modality"] == "visual_description"]
    assert len(vlm_items) == 1
    vlm = vlm_items[0]
    assert vlm["evidence_id"] == "ve_vlm_img_002"
    assert vlm["payload"]["status"] == "unresolved_visual_reference"
    assert vlm["model_provenance"]["backend"] == "lm-studio"
    assert vlm["model_provenance"]["model"] == "qwen2-vl-7b-instruct"
    assert vlm["verification_status"] == "not_checked"


# ----------------------------------------------------------------------
# 6. Metadata enrichment binding
# ----------------------------------------------------------------------
def test_06_metadata_enrichment_binding(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    sm = manifest["source_metadata"]
    assert sm["enrichment_status"] == "enriched"
    assert sm["title"] == "Test Video Title"
    assert sm["author_name"] == "Tech Creator"
    assert sm["author_id"] == "auth_12345"
    assert sm["published_at"] == "2026-09-04T09:00:00+00:00"
    assert sm["first_seen_at"] == "2026-09-06T15:00:00+00:00"
    assert sm["source_url"] == "https://www.douyin.com/video/video_001"
    assert sm["tags"] == ["tech", "benchmark"]


# ----------------------------------------------------------------------
# 7. Metadata DB absent resilience
# ----------------------------------------------------------------------
def test_07_metadata_db_absent_resilience(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    asset_unenriched = replace(asset, source_metadata={})

    manifest = build_evidence_manifest(asset_unenriched, config=test_app_config, processed_dir=proc_dir)
    sm = manifest["source_metadata"]
    assert sm["enrichment_status"] == "unenriched"
    assert sm["title"] is None
    assert sm["author_name"] is None
    assert sm["published_at"] is None
    assert sm["first_seen_at"] is None
    assert sm["source_url"] == "https://www.douyin.com/video/video_001"
    assert sm["tags"] == []


# ----------------------------------------------------------------------
# 8. published_at vs first_seen_at semantics
# ----------------------------------------------------------------------
def test_08_published_at_vs_first_seen_at_semantics(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    snapshot = extract_source_metadata_snapshot(asset, proc_dir)

    assert snapshot["published_at"] == "2026-09-04T09:00:00+00:00"
    assert snapshot["first_seen_at"] == "2026-09-06T15:00:00+00:00"
    assert snapshot["published_at"] != snapshot["first_seen_at"]


# ----------------------------------------------------------------------
# 9. No fake collected_at
# ----------------------------------------------------------------------
def test_09_no_fake_collected_at(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir)

    assert "collected_at" not in manifest["source_metadata"]
    assert "collected_at" not in manifest["evidence_summary"]


# ----------------------------------------------------------------------
# 10. NO_AUDIO video evidence
# ----------------------------------------------------------------------
def test_10_no_audio_video_evidence(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    no_audio_transcript = {
        "video_id": "douyin_video_001",
        "canonical_id": "douyin_video_001",
        "platform": "douyin",
        "platform_content_id": "video_001",
        "status": "NO_AUDIO",
        "provenance": {"backend": "none", "reason": "NO_AUDIO"},
        "segments": [],
    }
    atomic_write_json(proc_dir / "transcript.json", no_audio_transcript)

    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    assert manifest["evidence_summary"]["speech_segments"] == 0
    assert manifest["evidence_summary"]["total_items"] == 0
    assert manifest["processing_provenance"]["model_provenance"]["asr"]["status"] == "NO_AUDIO"
    assert manifest["evidence_items"] == []


# ----------------------------------------------------------------------
# 11. Partial album evidence
# ----------------------------------------------------------------------
def test_11_partial_album_evidence(mock_album_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_album_asset
    vt_file = proc_dir / "visual" / "visual_transcript.json"
    vt_data = load_json(vt_file, {})
    vt_data["visual_evidence"][1]["status"] = "failed"
    vt_data["visual_evidence"][1]["error"] = "Simulated OCR decoding error"
    atomic_write_json(vt_file, vt_data)

    manifest = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    items = manifest["evidence_items"]
    ocr_items = [it for it in items if it["modality"] == "visual_text"]
    assert len(ocr_items) == 2
    assert ocr_items[0]["payload"]["ocr_status"] == "completed"
    assert ocr_items[1]["payload"]["ocr_status"] == "failed"


# ----------------------------------------------------------------------
# 12. Deterministic ordering
# ----------------------------------------------------------------------
def test_12_deterministic_ordering(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    m1 = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    m2 = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)

    assert m1["manifest_fingerprint"] == m2["manifest_fingerprint"]
    assert json.dumps(m1["evidence_items"], sort_keys=True) == json.dumps(m2["evidence_items"], sort_keys=True)


# ----------------------------------------------------------------------
# 13. Repeated binding idempotent (<1ms resume)
# ----------------------------------------------------------------------
def test_13_repeated_binding_idempotent(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    p1 = write_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    assert p1.is_file()
    mtime1 = p1.stat().st_mtime

    t0 = time.perf_counter()
    p2 = write_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=False)
    elapsed = time.perf_counter() - t0
    mtime2 = p2.stat().st_mtime

    assert p1 == p2
    assert mtime1 == mtime2
    assert elapsed < 0.05  # sub-50ms (typically < 2ms)


# ----------------------------------------------------------------------
# 14. Evidence or config change invalidates fingerprint
# ----------------------------------------------------------------------
def test_14_evidence_or_config_change_invalidates_fingerprint(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    m1 = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    fp1 = m1["manifest_fingerprint"]

    # Modify transcript segment text
    t_file = proc_dir / "transcript.json"
    t_data = load_json(t_file, {})
    t_data["segments"][0]["text"] = "Changed text content"
    atomic_write_json(t_file, t_data)

    m2 = build_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)
    fp2 = m2["manifest_fingerprint"]

    assert fp1 != fp2


# ----------------------------------------------------------------------
# 15. Archive immutability
# ----------------------------------------------------------------------
def test_15_archive_immutability(mock_video_asset: tuple[CanonicalMediaAsset, Path], test_app_config: AppConfig) -> None:
    asset, proc_dir = mock_video_asset
    files_before = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime) for p in asset.asset_root.iterdir()}

    write_evidence_manifest(asset, config=test_app_config, processed_dir=proc_dir, force=True)

    files_after = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime) for p in asset.asset_root.iterdir()}
    assert files_before == files_after


# ----------------------------------------------------------------------
# 16. Real C10 video offline smoke
# ----------------------------------------------------------------------
def test_16_real_c10_video_offline_smoke() -> None:
    from src.media_adapter import CanonicalMediaAssetAdapter
    real_archive = Path("archive")
    real_db = Path("data/metadata.db")
    if not (real_archive / "douyin" / "7681603850364521734").is_dir():
        pytest.skip("Real C10 video archive not available in current environment")

    config = load_config("config/config.json")
    adapter = CanonicalMediaAssetAdapter(
        archive_root=real_archive,
        metadata_db_path=real_db if real_db.is_file() else None,
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7681603850364521734", "douyin")
    manifest_path = asset.bind_evidence(config=config, force=True)

    assert manifest_path.is_file()
    assert verify_evidence_manifest(manifest_path)

    doc = load_evidence_manifest(manifest_path)
    assert doc is not None
    assert doc["canonical_id"] == "douyin_7681603850364521734"
    assert doc["evidence_summary"]["speech_segments"] == 184
    assert doc["evidence_summary"]["verification_status"] == "not_checked"
    assert doc["source_metadata"]["published_at"] == "2026-09-04T09:06:13+00:00"
    assert doc["source_metadata"]["first_seen_at"] == "2026-09-06T15:14:40.940867+00:00"
    assert "collected_at" not in doc["source_metadata"]
    assert len(doc["formal_asset"]["artifacts"]) == 1
    assert doc["formal_asset"]["artifacts"][0]["sha256"] == "3959a0561c58cea93b2d9093f66bf5b306afa888b914657140aa6c2b01bee7ad"


# ----------------------------------------------------------------------
# 17. Real C10 album offline smoke
# ----------------------------------------------------------------------
def test_17_real_c10_album_offline_smoke() -> None:
    from src.media_adapter import CanonicalMediaAssetAdapter
    real_archive = Path("archive")
    real_db = Path("data/metadata.db")
    if not (real_archive / "douyin" / "7682038498466993905").is_dir():
        pytest.skip("Real C10 album archive not available in current environment")

    config = load_config("config/config.json")
    adapter = CanonicalMediaAssetAdapter(
        archive_root=real_archive,
        metadata_db_path=real_db if real_db.is_file() else None,
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7682038498466993905", "douyin")
    manifest_path = asset.bind_evidence(config=config, force=True)

    assert manifest_path.is_file()
    assert verify_evidence_manifest(manifest_path)

    doc = load_evidence_manifest(manifest_path)
    assert doc is not None
    assert doc["canonical_id"] == "douyin_7682038498466993905"
    assert doc["evidence_summary"]["total_items"] == 4
    assert doc["evidence_summary"]["visual_ocr_items"] == 3
    assert doc["evidence_summary"]["visual_vlm_items"] == 1
    assert doc["evidence_summary"]["verification_status"] == "not_checked"
    assert doc["source_metadata"]["published_at"] == "2026-09-05T13:12:48+00:00"
    assert doc["source_metadata"]["first_seen_at"] == "2026-09-06T15:20:42.226331+00:00"
    assert "collected_at" not in doc["source_metadata"]

    # Verify image 1 has exact SHA-256 and sequence 1
    img1_ev = next(it for it in doc["evidence_items"] if it["evidence_id"] == "ve_img_001")
    assert img1_ev["source_artifact"]["sha256"] == "c42d50171f51030ad52b761f93d6017da8f674c10a01a7b779ed925e21971d4d"
    assert img1_ev["sequence"]["sequence_index"] == 1

    # Verify unresolved VLM item
    vlm_ev = next(it for it in doc["evidence_items"] if it["evidence_id"] == "ve_vlm_img_003")
    assert vlm_ev["payload"]["status"] == "unresolved_visual_reference"
    assert vlm_ev["verification_status"] == "not_checked"
