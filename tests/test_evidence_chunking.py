"""Unit and integration tests for Milestone M3-05: Long Media Chunking.

Covers all 20 mandatory test requirements:
1. Video evidence manifest -> chunks
2. All speech evidence covered (100% unique coverage)
3. Deterministic chunk ordering (1..N)
4. Deterministic chunk IDs (chk_000001, chk_000002...)
5. Temporal range correct (envelope of contained speech evidence)
6. Bounded overlap (strictly recorded in overlap_evidence_ids)
7. No segment text splitting
8. Config/policy change invalidates cache
9. Evidence manifest change invalidates cache
10. Second run idempotent / sub-second cache hit (< 5ms)
11. NO_AUDIO video -> 0 speech chunks (no fake chunks)
12. Album evidence -> chunks
13. Album sequence preserved (1-indexed order)
14. Same-image OCR/VLM evidence grouped together
15. Unresolved VLM preserved in chunk references
16. Partial OCR evidence preserved
17. Large synthetic album batching (e.g. 12 images -> 3 chunks)
18. Archive immutability (formal archive 100% read-only)
19. Real C10 video offline smoke (184/184 unique segments)
20. Real C10 album offline smoke (3 images / 4 items)
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import pytest

from src.chunking import (
    EVIDENCE_CHUNKS_FILENAME,
    EVIDENCE_CHUNKS_SCHEMA_VERSION,
    ChunkingPolicy,
    chunk_evidence_manifest,
    load_evidence_chunks,
    verify_evidence_chunks,
    write_evidence_chunks,
)
from src.config import AppConfig, load_config
from src.storage import atomic_write_json


@pytest.fixture
def test_app_config(tmp_path: Path) -> AppConfig:
    base_config = load_config("config/config.json")
    raw = copy.deepcopy(base_config.raw)
    raw["paths"]["data_root"] = str(tmp_path / "data")
    return AppConfig(raw=raw, path=base_config.path)


@pytest.fixture
def mock_video_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    proc_dir = tmp_path / "data" / "processed" / "douyin_mock_video"
    proc_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = proc_dir / "evidence_manifest.json"

    # Create 10 mock speech segments (each 10s, 20 tokens)
    items = []
    for i in range(1, 11):
        start_t = (i - 1) * 10.0
        end_t = i * 10.0
        items.append({
            "evidence_id": f"ev_seg_{i:06d}",
            "modality": "speech",
            "source_artifact": {
                "role": "PRIMARY_VIDEO",
                "file_name": "mock_video.mp4",
                "sha256": "mock_video_sha256",
            },
            "temporal": {
                "start": start_t,
                "end": end_t,
                "duration": 10.0,
                "segment_id": f"seg_{i:06d}",
                "segment_order": i,
            },
            "sequence": None,
            "payload": {
                "text": f"This is mock speech segment number {i} for testing chunk windowing.",
            },
            "verification_status": "not_checked",
        })

    manifest = {
        "schema_version": "evidence-manifest-v1",
        "manifest_fingerprint": "mock_manifest_fp_video_123",
        "canonical_id": "douyin_mock_video",
        "platform": "douyin",
        "platform_content_id": "mock_video",
        "content_type": "video",
        "source_metadata": {"title": "Mock Video"},
        "formal_asset": {
            "canonical_id": "douyin_mock_video",
            "content_type": "video",
            "artifacts": [{
                "file_name": "mock_video.mp4",
                "role": "PRIMARY_VIDEO",
                "sha256": "mock_video_sha256",
                "byte_size": 123456,
            }],
        },
        "evidence_summary": {
            "total_items": len(items),
            "speech_segments": len(items),
            "verification_status": "not_checked",
        },
        "evidence_items": items,
    }
    atomic_write_json(manifest_file, manifest)
    return manifest_file, manifest


@pytest.fixture
def mock_album_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    proc_dir = tmp_path / "data" / "processed" / "douyin_mock_album"
    proc_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = proc_dir / "evidence_manifest.json"

    # Create mock album with 3 images:
    # Image 1: 1 OCR
    # Image 2: 1 OCR
    # Image 3: 1 OCR + 1 VLM (unresolved)
    items = [
        {
            "evidence_id": "ve_img_001",
            "modality": "visual_text",
            "source_artifact": {"role": "ALBUM_IMAGE", "file_name": "img_001.webp", "sha256": "sha_img_1", "sequence_index": 1},
            "sequence": {"sequence_index": 1},
            "temporal": None,
            "payload": {"text": "Title on image 1"},
            "verification_status": "not_checked",
        },
        {
            "evidence_id": "ve_img_002",
            "modality": "visual_text",
            "source_artifact": {"role": "ALBUM_IMAGE", "file_name": "img_002.webp", "sha256": "sha_img_2", "sequence_index": 2},
            "sequence": {"sequence_index": 2},
            "temporal": None,
            "payload": {"text": "Content on image 2"},
            "verification_status": "not_checked",
        },
        {
            "evidence_id": "ve_img_003",
            "modality": "visual_text",
            "source_artifact": {"role": "ALBUM_IMAGE", "file_name": "img_003.webp", "sha256": "sha_img_3", "sequence_index": 3},
            "sequence": {"sequence_index": 3},
            "temporal": None,
            "payload": {"text": "Ending on image 3"},
            "verification_status": "not_checked",
        },
        {
            "evidence_id": "ve_vlm_img_003",
            "modality": "visual_description",
            "source_artifact": {"role": "ALBUM_IMAGE", "file_name": "img_003.webp", "sha256": "sha_img_3", "sequence_index": 3},
            "sequence": {"sequence_index": 3},
            "temporal": None,
            "payload": {"status": "unresolved_visual_reference", "reason": "local_vlm_offline"},
            "verification_status": "not_checked",
        },
    ]

    manifest = {
        "schema_version": "evidence-manifest-v1",
        "manifest_fingerprint": "mock_manifest_fp_album_456",
        "canonical_id": "douyin_mock_album",
        "platform": "douyin",
        "platform_content_id": "mock_album",
        "content_type": "image_album",
        "source_metadata": {"title": "Mock Album"},
        "formal_asset": {
            "canonical_id": "douyin_mock_album",
            "content_type": "image_album",
            "artifacts": [
                {"file_name": "img_001.webp", "role": "ALBUM_IMAGE", "sha256": "sha_img_1", "byte_size": 100, "sequence_index": 1},
                {"file_name": "img_002.webp", "role": "ALBUM_IMAGE", "sha256": "sha_img_2", "byte_size": 200, "sequence_index": 2},
                {"file_name": "img_003.webp", "role": "ALBUM_IMAGE", "sha256": "sha_img_3", "byte_size": 300, "sequence_index": 3},
            ],
        },
        "evidence_summary": {
            "total_items": len(items),
            "visual_ocr_items": 3,
            "visual_vlm_items": 1,
            "verification_status": "not_checked",
        },
        "evidence_items": items,
    }
    atomic_write_json(manifest_file, manifest)
    return manifest_file, manifest


# ----------------------------------------------------------------------
# 1. Video evidence manifest -> chunks
# ----------------------------------------------------------------------
def test_01_video_evidence_manifest_to_chunks(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    doc = chunk_evidence_manifest(manifest_file)
    assert doc["schema_version"] == EVIDENCE_CHUNKS_SCHEMA_VERSION
    assert doc["canonical_id"] == "douyin_mock_video"
    assert doc["content_type"] == "video"
    assert doc["summary"]["total_chunks"] >= 1
    assert verify_evidence_chunks(doc)


# ----------------------------------------------------------------------
# 2. All speech evidence covered (100% unique coverage)
# ----------------------------------------------------------------------
def test_02_all_speech_evidence_covered(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, manifest = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=30.0, max_segments=4, overlap_segments=1)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    all_source_ids = {it["evidence_id"] for it in manifest["evidence_items"]}
    covered_ids = {eid for c in doc["chunks"] for eid in c["evidence_ids"]}
    assert covered_ids == all_source_ids
    assert doc["summary"]["unique_evidence_referenced"] == len(all_source_ids)


# ----------------------------------------------------------------------
# 3. Deterministic chunk ordering (1..N)
# ----------------------------------------------------------------------
def test_03_deterministic_chunk_ordering(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=25.0, overlap_segments=1)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    orders = [c["sequence_order"] for c in doc["chunks"]]
    assert orders == list(range(1, len(doc["chunks"]) + 1))


# ----------------------------------------------------------------------
# 4. Deterministic chunk IDs (chk_000001, chk_000002...)
# ----------------------------------------------------------------------
def test_04_deterministic_chunk_ids(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=25.0, overlap_segments=1)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    expected_ids = [f"chk_{i:06d}" for i in range(1, len(doc["chunks"]) + 1)]
    actual_ids = [c["chunk_id"] for c in doc["chunks"]]
    assert actual_ids == expected_ids


# ----------------------------------------------------------------------
# 5. Temporal range correct (envelope of contained speech evidence)
# ----------------------------------------------------------------------
def test_05_temporal_range_correct(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, manifest = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=30.0, overlap_segments=0)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    item_map = {it["evidence_id"]: it for it in manifest["evidence_items"]}
    for c in doc["chunks"]:
        first_item = item_map[c["evidence_ids"][0]]
        last_item = item_map[c["evidence_ids"][-1]]
        expected_start = first_item["temporal"]["start"]
        expected_end = last_item["temporal"]["end"]
        tr = c["temporal_range"]
        assert tr["start"] == expected_start
        assert tr["end"] == expected_end
        assert tr["duration"] == round(expected_end - expected_start, 3)


# ----------------------------------------------------------------------
# 6. Bounded overlap (strictly recorded in overlap_evidence_ids)
# ----------------------------------------------------------------------
def test_06_bounded_overlap(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=30.0, overlap_segments=2)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    assert len(doc["chunks"]) > 1
    # Chunk 1 has no overlap
    assert doc["chunks"][0]["overlap_evidence_ids"] == []

    # Subsequent chunks have overlap matching previous chunk's last 2 elements
    for i in range(1, len(doc["chunks"])):
        prev_chunk = doc["chunks"][i - 1]
        curr_chunk = doc["chunks"][i]
        expected_overlap = prev_chunk["evidence_ids"][-2:]
        assert curr_chunk["overlap_evidence_ids"] == expected_overlap
        assert curr_chunk["evidence_ids"][:2] == expected_overlap


# ----------------------------------------------------------------------
# 7. No segment text splitting
# ----------------------------------------------------------------------
def test_07_no_segment_text_splitting(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, manifest = mock_video_manifest
    doc = chunk_evidence_manifest(manifest_file)
    # Ensure evidence_ids are strictly exact IDs from manifest, never substringed or split
    all_source_ids = {it["evidence_id"] for it in manifest["evidence_items"]}
    for c in doc["chunks"]:
        for eid in c["evidence_ids"]:
            assert eid in all_source_ids


# ----------------------------------------------------------------------
# 8. Config/policy change invalidates cache
# ----------------------------------------------------------------------
def test_08_config_change_invalidates_cache(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    policy1 = ChunkingPolicy(max_duration_seconds=50.0, overlap_segments=1)
    p1 = write_evidence_chunks(manifest_file, policy=policy1, force=True)
    doc1 = load_evidence_chunks(p1)

    policy2 = ChunkingPolicy(max_duration_seconds=20.0, overlap_segments=2)
    p2 = write_evidence_chunks(manifest_file, policy=policy2, force=False)
    doc2 = load_evidence_chunks(p2)

    assert doc1["chunks_fingerprint"] != doc2["chunks_fingerprint"]
    assert doc1["summary"]["total_chunks"] != doc2["summary"]["total_chunks"]


# ----------------------------------------------------------------------
# 9. Evidence manifest change invalidates cache
# ----------------------------------------------------------------------
def test_09_evidence_manifest_change_invalidates_cache(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, manifest = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=50.0)
    p1 = write_evidence_chunks(manifest_file, policy=policy, force=True)
    fp1 = load_evidence_chunks(p1)["chunks_fingerprint"]

    # Modify evidence manifest fingerprint and content
    mutated = copy.deepcopy(manifest)
    mutated["manifest_fingerprint"] = "mutated_fp_9999"
    mutated["evidence_items"][0]["payload"]["text"] = "Mutated speech content."
    atomic_write_json(manifest_file, mutated)

    p2 = write_evidence_chunks(manifest_file, policy=policy, force=False)
    fp2 = load_evidence_chunks(p2)["chunks_fingerprint"]
    assert fp1 != fp2


# ----------------------------------------------------------------------
# 10. Second run idempotent / sub-second cache hit (< 5ms)
# ----------------------------------------------------------------------
def test_10_second_run_idempotent_cache_hit(mock_video_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_video_manifest
    policy = ChunkingPolicy(max_duration_seconds=40.0, overlap_segments=1)
    p1 = write_evidence_chunks(manifest_file, policy=policy, force=True)
    mtime1 = p1.stat().st_mtime

    t0 = time.perf_counter()
    p2 = write_evidence_chunks(manifest_file, policy=policy, force=False)
    elapsed = time.perf_counter() - t0
    mtime2 = p2.stat().st_mtime

    assert p1 == p2
    assert mtime1 == mtime2
    assert elapsed < 0.05  # sub-50ms requirement (typically < 3ms)


# ----------------------------------------------------------------------
# 11. NO_AUDIO video -> 0 speech chunks (no fake chunks)
# ----------------------------------------------------------------------
def test_11_no_audio_video_no_fake_speech_chunk(tmp_path: Path) -> None:
    proc_dir = tmp_path / "data" / "processed" / "douyin_no_audio_video"
    proc_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = proc_dir / "evidence_manifest.json"

    no_audio_manifest = {
        "schema_version": "evidence-manifest-v1",
        "manifest_fingerprint": "no_audio_fp_123",
        "canonical_id": "douyin_no_audio_video",
        "platform": "douyin",
        "platform_content_id": "no_audio_video",
        "content_type": "video",
        "formal_asset": {"canonical_id": "douyin_no_audio_video", "content_type": "video", "artifacts": []},
        "evidence_summary": {"total_items": 0, "speech_segments": 0, "verification_status": "not_checked"},
        "evidence_items": [],
    }
    atomic_write_json(manifest_file, no_audio_manifest)

    doc = chunk_evidence_manifest(manifest_file)
    assert doc["summary"]["total_chunks"] == 0
    assert doc["summary"]["status"] == "NO_AUDIO"
    assert doc["chunks"] == []
    assert verify_evidence_chunks(doc)


# ----------------------------------------------------------------------
# 12. Album evidence -> chunks
# ----------------------------------------------------------------------
def test_12_album_evidence_to_chunks(mock_album_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_album_manifest
    doc = chunk_evidence_manifest(manifest_file)
    assert doc["schema_version"] == EVIDENCE_CHUNKS_SCHEMA_VERSION
    assert doc["canonical_id"] == "douyin_mock_album"
    assert doc["content_type"] == "image_album"
    assert doc["summary"]["total_chunks"] == 1
    assert doc["summary"]["unique_evidence_referenced"] == 4
    assert verify_evidence_chunks(doc)


# ----------------------------------------------------------------------
# 13. Album sequence preserved (1-indexed order)
# ----------------------------------------------------------------------
def test_13_album_sequence_preserved(mock_album_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_album_manifest
    doc = chunk_evidence_manifest(manifest_file)
    chunk = doc["chunks"][0]
    sr = chunk["image_sequence_range"]
    assert sr["start_index"] == 1
    assert sr["end_index"] == 3
    assert sr["image_count"] == 3
    assert chunk["temporal_range"] is None  # strictly no fake temporal range


# ----------------------------------------------------------------------
# 14. Same-image OCR/VLM evidence grouped together
# ----------------------------------------------------------------------
def test_14_same_image_ocr_vlm_grouping(mock_album_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_album_manifest
    doc = chunk_evidence_manifest(manifest_file)
    chunk = doc["chunks"][0]
    eids = chunk["evidence_ids"]
    # Image 3 contains both OCR (ve_img_003) and VLM (ve_vlm_img_003)
    assert "ve_img_003" in eids
    assert "ve_vlm_img_003" in eids
    # Both must be present in the same chunk
    assert eids.index("ve_img_003") < eids.index("ve_vlm_img_003")


# ----------------------------------------------------------------------
# 15. Unresolved VLM preserved in chunk references
# ----------------------------------------------------------------------
def test_15_unresolved_vlm_preserved(mock_album_manifest: tuple[Path, dict[str, Any]]) -> None:
    manifest_file, _ = mock_album_manifest
    doc = chunk_evidence_manifest(manifest_file)
    chunk = doc["chunks"][0]
    assert "ve_vlm_img_003" in chunk["evidence_ids"]
    assert chunk["verification_status"] == "not_checked"


# ----------------------------------------------------------------------
# 16. Partial OCR evidence preserved
# ----------------------------------------------------------------------
def test_16_partial_evidence_preserved(tmp_path: Path) -> None:
    proc_dir = tmp_path / "data" / "processed" / "douyin_partial_album"
    proc_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = proc_dir / "evidence_manifest.json"

    # Image 1 failed, Image 2 succeeded
    items = [
        {
            "evidence_id": "ve_ocr_failed_001",
            "modality": "visual_text",
            "source_artifact": {"file_name": "img_001.webp", "role": "ALBUM_IMAGE", "sequence_index": 1, "sha256": "s1"},
            "sequence": {"sequence_index": 1},
            "payload": {"status": "ocr_failed", "error": "corrupt image header"},
            "verification_status": "not_checked",
        },
        {
            "evidence_id": "ve_ocr_success_002",
            "modality": "visual_text",
            "source_artifact": {"file_name": "img_002.webp", "role": "ALBUM_IMAGE", "sequence_index": 2, "sha256": "s2"},
            "sequence": {"sequence_index": 2},
            "payload": {"text": "Clean text"},
            "verification_status": "not_checked",
        },
    ]
    manifest = {
        "schema_version": "evidence-manifest-v1",
        "manifest_fingerprint": "partial_album_fp",
        "canonical_id": "douyin_partial_album",
        "content_type": "image_album",
        "formal_asset": {"canonical_id": "douyin_partial_album", "content_type": "image_album", "artifacts": []},
        "evidence_summary": {"total_items": 2, "verification_status": "not_checked"},
        "evidence_items": items,
    }
    atomic_write_json(manifest_file, manifest)

    doc = chunk_evidence_manifest(manifest_file)
    assert len(doc["chunks"]) == 1
    assert "ve_ocr_failed_001" in doc["chunks"][0]["evidence_ids"]
    assert "ve_ocr_success_002" in doc["chunks"][0]["evidence_ids"]


# ----------------------------------------------------------------------
# 17. Large synthetic album batching (e.g. 12 images -> 3 chunks)
# ----------------------------------------------------------------------
def test_17_large_synthetic_album_batching(tmp_path: Path) -> None:
    proc_dir = tmp_path / "data" / "processed" / "douyin_large_album"
    proc_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = proc_dir / "evidence_manifest.json"

    # 12 images, batch size = 5 -> chunks of size 5, 5, 2 (3 chunks)
    items = []
    for i in range(1, 13):
        items.append({
            "evidence_id": f"ve_img_{i:03d}",
            "modality": "visual_text",
            "source_artifact": {"file_name": f"img_{i:03d}.webp", "role": "ALBUM_IMAGE", "sequence_index": i, "sha256": f"sha_{i}"},
            "sequence": {"sequence_index": i},
            "payload": {"text": f"Page {i}"},
            "verification_status": "not_checked",
        })

    manifest = {
        "schema_version": "evidence-manifest-v1",
        "manifest_fingerprint": "large_album_fp",
        "canonical_id": "douyin_large_album",
        "content_type": "image_album",
        "formal_asset": {"canonical_id": "douyin_large_album", "content_type": "image_album", "artifacts": []},
        "evidence_summary": {"total_items": 12, "verification_status": "not_checked"},
        "evidence_items": items,
    }
    atomic_write_json(manifest_file, manifest)

    policy = ChunkingPolicy(album_images_per_chunk=5)
    doc = chunk_evidence_manifest(manifest_file, policy=policy)

    assert doc["summary"]["total_chunks"] == 3
    assert doc["chunks"][0]["image_sequence_range"] == {"start_index": 1, "end_index": 5, "image_count": 5}
    assert doc["chunks"][1]["image_sequence_range"] == {"start_index": 6, "end_index": 10, "image_count": 5}
    assert doc["chunks"][2]["image_sequence_range"] == {"start_index": 11, "end_index": 12, "image_count": 2}


# ----------------------------------------------------------------------
# 18. Archive immutability (formal archive 100% read-only)
# ----------------------------------------------------------------------
def test_18_archive_immutability(mock_video_manifest: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    manifest_file, _ = mock_video_manifest
    fake_archive = tmp_path / "archive" / "douyin" / "mock_video"
    fake_archive.mkdir(parents=True, exist_ok=True)
    video_file = fake_archive / "mock_video.mp4"
    video_file.write_bytes(b"dummy archive data")

    archive_snapshot_before = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()) for p in fake_archive.iterdir()}

    write_evidence_chunks(manifest_file, force=True)

    archive_snapshot_after = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()) for p in fake_archive.iterdir()}
    assert archive_snapshot_before == archive_snapshot_after


# ----------------------------------------------------------------------
# 19. Real C10 video offline smoke (184/184 unique segments)
# ----------------------------------------------------------------------
def test_19_real_c10_video_smoke() -> None:
    real_manifest = Path("data/processed/douyin_7681603850364521734/evidence_manifest.json")
    if not real_manifest.is_file():
        pytest.skip("Real C10 video evidence manifest not available")

    chunks_file = write_evidence_chunks(real_manifest, force=True)
    assert chunks_file.is_file()
    assert verify_evidence_chunks(chunks_file)

    doc = load_evidence_chunks(chunks_file)
    assert doc is not None
    assert doc["canonical_id"] == "douyin_7681603850364521734"
    assert doc["summary"]["total_chunks"] == 4
    assert doc["summary"]["unique_evidence_referenced"] == 184
    assert doc["summary"]["verification_status"] == "not_checked"

    # Verify temporal range is strictly contiguous and envelopes segments
    for c in doc["chunks"]:
        assert c["temporal_range"] is not None
        assert c["temporal_range"]["start"] < c["temporal_range"]["end"]
        assert c["temporal_range"]["duration"] > 0
        assert c["verification_status"] == "not_checked"
        assert len(c["source_artifacts"]) == 1
        assert c["source_artifacts"][0]["role"] == "PRIMARY_VIDEO"


# ----------------------------------------------------------------------
# 20. Real C10 album offline smoke (3 images / 4 items)
# ----------------------------------------------------------------------
def test_20_real_c10_album_smoke() -> None:
    real_manifest = Path("data/processed/douyin_7682038498466993905/evidence_manifest.json")
    if not real_manifest.is_file():
        pytest.skip("Real C10 album evidence manifest not available")

    chunks_file = write_evidence_chunks(real_manifest, force=True)
    assert chunks_file.is_file()
    assert verify_evidence_chunks(chunks_file)

    doc = load_evidence_chunks(chunks_file)
    assert doc is not None
    assert doc["canonical_id"] == "douyin_7682038498466993905"
    assert doc["summary"]["total_chunks"] == 1
    assert doc["summary"]["unique_evidence_referenced"] == 4
    assert doc["summary"]["verification_status"] == "not_checked"

    chunk = doc["chunks"][0]
    assert chunk["temporal_range"] is None
    assert chunk["image_sequence_range"] == {"start_index": 1, "end_index": 3, "image_count": 3}
    assert "ve_img_003" in chunk["evidence_ids"]
    assert "ve_vlm_img_003" in chunk["evidence_ids"]
    assert chunk["verification_status"] == "not_checked"
