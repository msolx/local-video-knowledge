"""Unit tests for Milestone M3 CanonicalMediaAssetAdapter (M3-01).

Validates:
- Video and Image Album formal local asset ingestion.
- Strict ordering and optional BGM handling for image albums.
- Hash integrity verification and missing file defenses.
- Error taxonomy (ManifestNotFoundError, MediaFileNotFoundError, MediaHashMismatchError, etc.).
- Provenance and SQLite metadata enrichment.
- Projection to pipeline MediaAsset contract.
- Offline integration smoke test on real C10 formal assets.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import pytest

from src.config import load_config
from src.media_adapter import (
    AlbumImageArtifact,
    CanonicalMediaAsset,
    CanonicalMediaAssetAdapter,
    CanonicalMediaType,
    InvalidMediaAssetError,
    ManifestInvalidError,
    ManifestNotFoundError,
    MediaFileNotFoundError,
    MediaHashMismatchError,
    UnsupportedContentTypeError,
)


def _write_file_with_hash(path: Path, content: bytes) -> tuple[Path, str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return path, digest, len(content)


# =============================================================================
# 1. Video Formal Asset Tests
# =============================================================================


def test_01_load_formal_video_success(tmp_path: Path) -> None:
    content_dir = tmp_path / "douyin" / "7681603850364521734"
    video_file, video_sha, video_size = _write_file_with_hash(
        content_dir / "7681603850364521734.mp4", b"dummy_mp4_video_payload_bytes_12345"
    )

    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "7681603850364521734",
        "archived_at": "2026-09-06T15:16:38.000Z",
        "source_provenance": {
            "task_id": "dl_douyin_7681603850364521734_test",
            "scope_id": "douyin:dyacct_test",
        },
        "asset_count": 1,
        "assets": [
            {
                "file_name": "7681603850364521734.mp4",
                "relative_archive_path": "douyin/7681603850364521734/7681603850364521734.mp4",
                "role": "ArtifactRole.PRIMARY_VIDEO",
                "sequence_index": None,
                "byte_size": video_size,
                "sha256": video_sha,
                "content_type": "video",
                "media_summary": {"width": 1080, "height": 1920, "duration_sec": 6.5},
            }
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=True)
    asset = adapter.load_from_dir(content_dir)

    assert asset.is_video is True
    assert asset.is_album is False
    assert asset.content_type == CanonicalMediaType.VIDEO
    assert asset.platform == "douyin"
    assert asset.platform_content_id == "7681603850364521734"
    assert asset.canonical_id == "douyin_7681603850364521734"
    assert asset.video_path == video_file.resolve()
    assert asset.video_sha256 == video_sha
    assert asset.video_size_bytes == video_size
    assert asset.primary_media_path == video_file.resolve()
    assert asset.source_provenance["task_id"] == "dl_douyin_7681603850364521734_test"
    assert asset.has_audio is False
    assert len(asset.album_images) == 0


# =============================================================================
# 2. Image Album Formal Asset Tests
# =============================================================================


def test_02_load_formal_image_album_success(tmp_path: Path) -> None:
    content_dir = tmp_path / "douyin" / "7682038498466993905"
    _, img1_sha, img1_size = _write_file_with_hash(content_dir / "img_001.webp", b"webp_image_1")
    _, img2_sha, img2_size = _write_file_with_hash(content_dir / "img_002.webp", b"webp_image_2")
    _, img3_sha, img3_size = _write_file_with_hash(content_dir / "img_003.webp", b"webp_image_3")

    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "7682038498466993905",
        "archived_at": "2026-09-06T15:26:25.000Z",
        "source_provenance": {"task_id": "dl_album_test"},
        "asset_count": 3,
        "assets": [
            {
                "file_name": "img_001.webp",
                "role": "ArtifactRole.ALBUM_IMAGE",
                "sequence_index": 1,
                "byte_size": img1_size,
                "sha256": img1_sha,
                "content_type": "image",
            },
            {
                "file_name": "img_002.webp",
                "role": "ArtifactRole.ALBUM_IMAGE",
                "sequence_index": 2,
                "byte_size": img2_size,
                "sha256": img2_sha,
                "content_type": "image",
            },
            {
                "file_name": "img_003.webp",
                "role": "ArtifactRole.ALBUM_IMAGE",
                "sequence_index": 3,
                "byte_size": img3_size,
                "sha256": img3_sha,
                "content_type": "image",
            },
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=True)
    asset = adapter.load_from_dir(content_dir)

    assert asset.is_video is False
    assert asset.is_album is True
    assert asset.content_type == CanonicalMediaType.IMAGE_ALBUM
    assert len(asset.album_images) == 3
    assert [img.sequence_index for img in asset.album_images] == [1, 2, 3]
    assert asset.album_images[0].file_name == "img_001.webp"
    assert asset.album_images[1].file_name == "img_002.webp"
    assert asset.album_images[2].file_name == "img_003.webp"
    assert asset.has_audio is False
    assert asset.video_path is None


def test_03_load_formal_image_album_with_optional_bgm(tmp_path: Path) -> None:
    content_dir = tmp_path / "douyin" / "album_with_bgm"
    _, img1_sha, img1_size = _write_file_with_hash(content_dir / "slide_1.webp", b"slide_1")
    _, img2_sha, img2_size = _write_file_with_hash(content_dir / "slide_2.webp", b"slide_2")
    audio_file, audio_sha, audio_size = _write_file_with_hash(content_dir / "bgm.mp3", b"bgm_audio_bytes")

    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "album_with_bgm",
        "assets": [
            {
                "file_name": "slide_1.webp",
                "role": "ALBUM_IMAGE",
                "sequence_index": 1,
                "byte_size": img1_size,
                "sha256": img1_sha,
                "content_type": "image",
            },
            {
                "file_name": "slide_2.webp",
                "role": "ALBUM_IMAGE",
                "sequence_index": 2,
                "byte_size": img2_size,
                "sha256": img2_sha,
                "content_type": "image",
            },
            {
                "file_name": "bgm.mp3",
                "role": "AUDIO_TRACK",
                "sequence_index": None,
                "byte_size": audio_size,
                "sha256": audio_sha,
                "content_type": "audio",
            },
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=True)
    asset = adapter.load_from_dir(content_dir)

    assert asset.is_album is True
    assert asset.has_audio is True
    assert asset.audio_path == audio_file.resolve()
    assert asset.audio_sha256 == audio_sha
    assert asset.audio_size_bytes == audio_size
    assert len(asset.album_images) == 2


def test_04_album_images_ordering_defense(tmp_path: Path) -> None:
    content_dir = tmp_path / "douyin" / "shuffled_album"
    _, sha1, sz1 = _write_file_with_hash(content_dir / "img_3.webp", b"3")
    _, sha2, sz2 = _write_file_with_hash(content_dir / "img_1.webp", b"1")
    _, sha3, sz3 = _write_file_with_hash(content_dir / "img_2.webp", b"2")

    # Manifest deliberately listed out of order: seq 3, seq 1, seq 2
    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "shuffled_album",
        "assets": [
            {"file_name": "img_3.webp", "role": "ALBUM_IMAGE", "sequence_index": 3, "byte_size": sz1, "sha256": sha1, "content_type": "image"},
            {"file_name": "img_1.webp", "role": "ALBUM_IMAGE", "sequence_index": 1, "byte_size": sz2, "sha256": sha2, "content_type": "image"},
            {"file_name": "img_2.webp", "role": "ALBUM_IMAGE", "sequence_index": 2, "byte_size": sz3, "sha256": sha3, "content_type": "image"},
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=True)
    asset = adapter.load_from_dir(content_dir)

    # Invariant: Must be strictly sorted 1, 2, 3
    assert [img.sequence_index for img in asset.album_images] == [1, 2, 3]
    assert [img.file_name for img in asset.album_images] == ["img_1.webp", "img_2.webp", "img_3.webp"]


# =============================================================================
# 3. Error Handling & Invariant Defenses
# =============================================================================


def test_05_missing_manifest_raises_error(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    adapter = CanonicalMediaAssetAdapter()
    with pytest.raises(ManifestNotFoundError):
        adapter.load_from_dir(empty_dir)


def test_06_corrupt_manifest_json_raises_error(tmp_path: Path) -> None:
    corrupt_dir = tmp_path / "corrupt_dir"
    corrupt_dir.mkdir()
    (corrupt_dir / "asset_manifest.json").write_text("NOT_A_VALID_JSON{{{", encoding="utf-8")
    adapter = CanonicalMediaAssetAdapter()
    with pytest.raises(ManifestInvalidError):
        adapter.load_from_dir(corrupt_dir)


def test_07_missing_media_file_raises_error(tmp_path: Path) -> None:
    content_dir = tmp_path / "missing_file_dir"
    content_dir.mkdir()
    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "test_missing",
        "assets": [
            {"file_name": "non_existent.mp4", "role": "PRIMARY_VIDEO", "byte_size": 100, "sha256": "abc", "content_type": "video"}
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter()
    with pytest.raises(MediaFileNotFoundError) as exc:
        adapter.load_from_dir(content_dir)
    assert "non_existent.mp4" in str(exc.value)


def test_08_sha256_mismatch_behavior(tmp_path: Path) -> None:
    content_dir = tmp_path / "hash_mismatch_dir"
    _, real_sha, real_size = _write_file_with_hash(content_dir / "video.mp4", b"actual_content")

    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "test_hash",
        "assets": [
            {"file_name": "video.mp4", "role": "PRIMARY_VIDEO", "byte_size": real_size, "sha256": "wrong_expected_sha256", "content_type": "video"}
        ],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    # When validate_hashes=True, must raise MediaHashMismatchError
    strict_adapter = CanonicalMediaAssetAdapter(validate_hashes=True)
    with pytest.raises(MediaHashMismatchError):
        strict_adapter.load_from_dir(content_dir)

    # When validate_hashes=False, hash check is bypassed
    relaxed_adapter = CanonicalMediaAssetAdapter(validate_hashes=False)
    asset = relaxed_adapter.load_from_dir(content_dir)
    assert asset.is_video is True


def test_09_zero_byte_media_raises_error(tmp_path: Path) -> None:
    content_dir = tmp_path / "zero_byte_dir"
    _write_file_with_hash(content_dir / "zero.mp4", b"")
    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "test_zero",
        "assets": [{"file_name": "zero.mp4", "role": "PRIMARY_VIDEO", "byte_size": 0, "content_type": "video"}],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=False)
    with pytest.raises(InvalidMediaAssetError):
        adapter.load_from_dir(content_dir)


def test_10_unknown_content_type_raises_error(tmp_path: Path) -> None:
    content_dir = tmp_path / "unknown_type_dir"
    _write_file_with_hash(content_dir / "weird.bin", b"strange_bytes")
    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "test_unknown",
        "assets": [{"file_name": "weird.bin", "role": "UNKNOWN_ROLE", "byte_size": 13, "content_type": "binary"}],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    adapter = CanonicalMediaAssetAdapter(validate_hashes=False)
    with pytest.raises(UnsupportedContentTypeError):
        adapter.load_from_dir(content_dir)


# =============================================================================
# 4. Provenance & Metadata Enrichment
# =============================================================================


def test_11_provenance_and_metadata_db_enrichment(tmp_path: Path) -> None:
    # Set up mock SQLite metadata.db
    db_path = tmp_path / "metadata.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE collection_items (platform_content_id TEXT, platform TEXT, canonical_json TEXT)")
    collector_payload = {
        "title": "Douyin Knowledge Video Title",
        "description": "Video description test",
        "author": {"display_name": "Expert Creator", "platform_author_id": "author_999"},
        "published_at": "2026-09-01T12:00:00Z",
        "source_url": "https://www.douyin.com/video/7681603850364521734",
    }
    con.execute(
        "INSERT INTO collection_items VALUES (?, ?, ?)",
        ("7681603850364521734", "douyin", json.dumps(collector_payload)),
    )
    con.commit()
    con.close()

    content_dir = tmp_path / "douyin" / "7681603850364521734"
    _, v_sha, v_size = _write_file_with_hash(content_dir / "video.mp4", b"video_data")
    manifest_data = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": "7681603850364521734",
        "assets": [{"file_name": "video.mp4", "role": "PRIMARY_VIDEO", "byte_size": v_size, "sha256": v_sha, "content_type": "video"}],
    }
    (content_dir / "asset_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    # With metadata.db configured
    adapter_with_db = CanonicalMediaAssetAdapter(metadata_db_path=db_path, validate_hashes=False)
    asset_with_db = adapter_with_db.load_from_dir(content_dir)

    assert asset_with_db.title == "Douyin Knowledge Video Title"
    assert asset_with_db.author_name == "Expert Creator"
    assert asset_with_db.source_metadata.get("source_url") == "https://www.douyin.com/video/7681603850364521734"

    # Without metadata.db configured (graceful fallback)
    adapter_no_db = CanonicalMediaAssetAdapter(metadata_db_path=None, validate_hashes=False)
    asset_no_db = adapter_no_db.load_from_dir(content_dir)

    assert asset_no_db.title == "7681603850364521734"  # falls back to content_id
    assert asset_no_db.author_name is None
    assert asset_no_db.source_metadata == {}


# =============================================================================
# 5. Archive Root Discovery (load_from_content_id / load_all)
# =============================================================================


def test_12_load_by_content_id_and_load_all(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    v_dir = archive_root / "douyin" / "vid_101"
    _, v_sha, v_sz = _write_file_with_hash(v_dir / "v.mp4", b"v101")
    (v_dir / "asset_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "platform": "douyin", "platform_content_id": "vid_101",
                    "assets": [{"file_name": "v.mp4", "role": "PRIMARY_VIDEO", "byte_size": v_sz, "sha256": v_sha, "content_type": "video"}]}),
        encoding="utf-8",
    )

    a_dir = archive_root / "douyin" / "alb_202"
    _, a_sha, a_sz = _write_file_with_hash(a_dir / "img.webp", b"a202")
    (a_dir / "asset_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "platform": "douyin", "platform_content_id": "alb_202",
                    "assets": [{"file_name": "img.webp", "role": "ALBUM_IMAGE", "sequence_index": 1, "byte_size": a_sz, "sha256": a_sha, "content_type": "image"}]}),
        encoding="utf-8",
    )

    adapter = CanonicalMediaAssetAdapter(archive_root=archive_root, validate_hashes=False)

    # By content ID
    asset_v = adapter.load_from_content_id("vid_101", platform="douyin")
    assert asset_v.is_video is True

    asset_a = adapter.load_from_content_id("alb_202", platform="douyin")
    assert asset_a.is_album is True

    # Load all
    all_assets = adapter.load_all("douyin")
    assert len(all_assets) == 2
    cids = {a.platform_content_id for a in all_assets}
    assert cids == {"vid_101", "alb_202"}


# =============================================================================
# 6. Pipeline Integration & Real C10 Smoke Tests
# =============================================================================


def test_13_projection_to_pipeline_media_asset_rejection(tmp_path: Path) -> None:
    # Image albums cannot be projected into single-video MediaAsset
    album_asset = CanonicalMediaAsset(
        platform="douyin",
        platform_content_id="test_alb",
        content_type=CanonicalMediaType.IMAGE_ALBUM,
        canonical_id="douyin_test_alb",
        asset_root=tmp_path,
        manifest_path=tmp_path / "asset_manifest.json",
        album_images=(
            AlbumImageArtifact(1, "img1.webp", tmp_path / "img1.webp", "sha1", 100),
        ),
    )
    config = load_config("config/config.example.json")
    with pytest.raises(UnsupportedContentTypeError) as exc:
        album_asset.to_pipeline_media_asset(config)
    assert "Cannot project non-video asset" in str(exc.value)


@pytest.mark.skipif(
    not Path("archive/douyin/7681603850364521734/asset_manifest.json").exists(),
    reason="Real C10 formal video asset not found in local workspace.",
)
def test_14_real_c10_video_integration_smoke() -> None:
    adapter = CanonicalMediaAssetAdapter(
        archive_root=Path("archive"),
        metadata_db_path=Path("data/metadata.db"),
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7681603850364521734")

    assert asset.is_video is True
    assert asset.video_path is not None
    assert asset.video_path.exists()
    assert asset.video_size_bytes == 173847684
    assert asset.video_sha256 == "3959a0561c58cea93b2d9093f66bf5b306afa888b914657140aa6c2b01bee7ad"
    assert asset.source_provenance.get("task_id") == "dl_douyin_7681603850364521734_aca45416a2adb423"

    # Verify projection into pipeline MediaAsset
    config = load_config("config/config.example.json")
    pipeline_asset = asset.to_pipeline_media_asset(config)
    assert pipeline_asset.video_id == "douyin_7681603850364521734"
    assert pipeline_asset.normalized_source == asset.video_path
    assert pipeline_asset.probe.duration > 0
    assert pipeline_asset.probe.video is not None
    assert pipeline_asset.probe.audio is not None


@pytest.mark.skipif(
    not Path("archive/douyin/7682038498466993905/asset_manifest.json").exists(),
    reason="Real C10 formal album asset not found in local workspace.",
)
def test_15_real_c10_album_integration_smoke() -> None:
    adapter = CanonicalMediaAssetAdapter(
        archive_root=Path("archive"),
        metadata_db_path=Path("data/metadata.db"),
        validate_hashes=True,
    )
    asset = adapter.load_from_content_id("7682038498466993905")

    assert asset.is_album is True
    assert len(asset.album_images) == 3
    assert [img.sequence_index for img in asset.album_images] == [1, 2, 3]
    for img in asset.album_images:
        assert img.path.exists()
        assert img.byte_size > 0
        assert len(img.sha256) == 64
    assert asset.has_audio is False
