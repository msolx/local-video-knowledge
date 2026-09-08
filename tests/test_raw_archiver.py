"""Unit test suite for DY-C06 Raw Response Archiver.

Covers:
  1. Basic archiving
  2. Exact raw preservation
  3. UTF-8 Chinese and emoji preservation
  4. Deterministic serialization
  5. SHA-256 integrity computation
  6. verify() method
  7. Corruption detection (single-byte modification)
  8. Atomic temp promotion (no leftover temp files)
  9. Idempotency (same page, same payload)
 10. Conflict rejection (same page, differing payload)
 11. Multi-page archiving within single sync run
 12. Manifest update
 13. Manifest discovery and recovery
 14. Missing file handling
 15. Sensitive credential detection
 16. Path safety and traversal prevention
 17. Safe path naming (no nickname/title in filename)
 18. Git-ignore isolation
 19. Windows path compatibility
 20. Integration with C07 RawRefContext and canonical validation
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src.collector.raw_archive import (
    DiskRawArchiver,
    RawArchiveConflictError,
    RawArchiveCorruptError,
    RawArchiveError,
    RawArchiveHashMismatchError,
    RawArchiveInvalidInputError,
    RawArchiveNotFoundError,
    RawArchiveRef,
    RawArchiveSensitiveFieldError,
)


@pytest.fixture
def temp_raw_root(tmp_path: Path) -> Path:
    return tmp_path / "raw"


@pytest.fixture
def archiver(temp_raw_root: Path) -> DiskRawArchiver:
    return DiskRawArchiver(root_dir=temp_raw_root)


@pytest.fixture
def sample_payload() -> dict[str, Any]:
    return {
        "status_code": 0,
        "has_more": 1,
        "cursor": "1788528929463841",
        "aweme_list": [
            {
                "aweme_id": "7681625905991157755",
                "desc": "FDE 平时到底在干什么？一个 AI 项目是怎么交付的 🧋🍹 #AI #Agent",
                "create_time": 1788517904,
                "author": {
                    "uid": "21801928056",
                    "nickname": "江哥AI落地实战",
                    "sec_uid": "MS4wLjABAAAA-UjgFb0Tbmhd_AzOuSyMxq390Q3NjVQpgefxXEDzTMA",
                },
            }
        ],
    }


# ============================================================================
# Basic Archiving & Integrity Tests
# ============================================================================


def test_1_basic_archive(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 1: Archive a collection page and confirm file creation and reference."""
    ref = archiver.archive_page(
        sync_run_id="run_001",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="1788528929463841",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    assert isinstance(ref, RawArchiveRef)
    assert ref.platform == "douyin"
    assert ref.sync_run_id == "run_001"
    assert ref.page_number == 1
    assert ref.byte_size > 0
    assert len(ref.sha256) == 64
    assert ref.path.endswith("page_000001.json")

    target_file = archiver.get_page_path("douyin", "run_001", 1)
    assert target_file.exists()


def test_2_exact_raw_preservation(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 2: Reading back archived raw file preserves identical semantic content."""
    ref = archiver.archive_page(
        sync_run_id="run_001",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    loaded = archiver.read_page(ref)
    assert loaded == sample_payload
    assert loaded["aweme_list"][0]["aweme_id"] == "7681625905991157755"


def test_3_utf8_chinese_emoji(archiver: DiskRawArchiver) -> None:
    """Case 3: UTF-8 Chinese characters, special punctuation, and emojis preserve accurately."""
    payload = {
        "status_code": 0,
        "title": "这才是成年人奶茶🧋便利店调酒🍹#调酒 #微醺",
        "author": "西瓜小新🍉（微醺版）",
        "bio": "中文字符测试：春水碧于天，画船听雨眠。⚡🔥🎉",
    }
    ref = archiver.archive_page(
        sync_run_id="run_utf8",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    loaded = archiver.read_page(ref)
    assert loaded["title"] == payload["title"]
    assert loaded["author"] == payload["author"]
    assert loaded["bio"] == payload["bio"]


def test_4_deterministic_serialization(archiver: DiskRawArchiver) -> None:
    """Case 4: Independent serializations of identical dict produce identical bytes and SHA-256."""
    payload_a = {"b": 2, "a": 1, "c": [3, 2, 1]}
    payload_b = {"a": 1, "c": [3, 2, 1], "b": 2}

    ref_a = archiver.archive_page(
        sync_run_id="run_det_a",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=payload_a,
        fetched_at="2026-09-05T08:00:00Z",
    )
    ref_b = archiver.archive_page(
        sync_run_id="run_det_b",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=payload_b,
        fetched_at="2026-09-05T08:00:00Z",
    )

    assert ref_a.sha256 == ref_b.sha256
    assert ref_a.byte_size == ref_b.byte_size


def test_5_sha256_matches_disk_bytes(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 5: ref.sha256 strictly equals hashlib.sha256 of physical bytes on disk."""
    ref = archiver.archive_page(
        sync_run_id="run_hash",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    disk_file = archiver.get_page_path("douyin", "run_hash", 1)
    with open(disk_file, "rb") as f:
        disk_bytes = f.read()

    expected_hash = hashlib.sha256(disk_bytes).hexdigest()
    assert ref.sha256 == expected_hash
    assert ref.byte_size == len(disk_bytes)


def test_6_verify_intact_file(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 6: verify() succeeds for an uncorrupted archived page."""
    ref = archiver.archive_page(
        sync_run_id="run_verify",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )
    assert archiver.verify(ref) is True


def test_7_corruption_detection(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 7: Modifying even a single byte on disk causes verify() to raise RawArchiveHashMismatchError."""
    ref = archiver.archive_page(
        sync_run_id="run_corrupt",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    disk_file = archiver.get_page_path("douyin", "run_corrupt", 1)
    with open(disk_file, "r+b") as f:
        f.seek(10)
        byte = f.read(1)
        f.seek(10)
        # Flip bit
        f.write(bytes([byte[0] ^ 0xFF]))

    with pytest.raises(RawArchiveHashMismatchError):
        archiver.verify(ref)


def test_8_atomic_temp_promotion(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 8: Atomic write cleans up any temporary files upon completion."""
    ref = archiver.archive_page(
        sync_run_id="run_atomic",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    run_dir = archiver.get_run_dir("douyin", "run_atomic")
    temp_files = list(run_dir.glob("*.tmp*"))
    assert len(temp_files) == 0


# ============================================================================
# Idempotency & Conflict Tests
# ============================================================================


def test_9_same_page_same_payload_idempotent(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 9: Archiving same page with identical payload returns existing ref without error."""
    ref1 = archiver.archive_page(
        sync_run_id="run_idempotent",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )
    ref2 = archiver.archive_page(
        sync_run_id="run_idempotent",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    assert ref1.sha256 == ref2.sha256
    assert ref1.path == ref2.path


def test_10_same_page_different_payload_conflict(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 10: Archiving same page with different payload raises RawArchiveConflictError."""
    archiver.archive_page(
        sync_run_id="run_conflict",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    different_payload = dict(sample_payload)
    different_payload["cursor"] = "9999999"

    with pytest.raises(RawArchiveConflictError):
        archiver.archive_page(
            sync_run_id="run_conflict",
            platform="douyin",
            page_number=1,
            request_cursor="0",
            response_cursor="100",
            raw_response=different_payload,
            fetched_at="2026-09-05T08:00:00Z",
        )


# ============================================================================
# Multi-page, Manifest & Discovery Tests
# ============================================================================


def test_11_two_pages_same_run(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 11: Sequential pages within same run persist as distinct numbered files."""
    ref1 = archiver.archive_page(
        sync_run_id="run_multi",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )
    p2_payload = dict(sample_payload)
    p2_payload["cursor"] = "200"
    ref2 = archiver.archive_page(
        sync_run_id="run_multi",
        platform="douyin",
        page_number=2,
        request_cursor="100",
        response_cursor="200",
        raw_response=p2_payload,
        fetched_at="2026-09-05T08:01:00Z",
    )

    assert ref1.page_number == 1
    assert ref2.page_number == 2
    assert ref1.path != ref2.path
    assert archiver.get_page_path("douyin", "run_multi", 1).exists()
    assert archiver.get_page_path("douyin", "run_multi", 2).exists()


def test_12_manifest_update(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 12: manifest.json is created and updated with inventory of all pages."""
    archiver.archive_page(
        sync_run_id="run_manifest",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )
    p2 = dict(sample_payload)
    p2["cursor"] = "200"
    archiver.archive_page(
        sync_run_id="run_manifest",
        platform="douyin",
        page_number=2,
        request_cursor="100",
        response_cursor="200",
        raw_response=p2,
        fetched_at="2026-09-05T08:01:00Z",
    )

    manifest_file = archiver.get_run_dir("douyin", "run_manifest") / "manifest.json"
    assert manifest_file.exists()

    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["sync_run_id"] == "run_manifest"
    assert len(data["pages"]) == 2
    assert data["pages"][0]["page_number"] == 1
    assert data["pages"][1]["page_number"] == 2


def test_13_manifest_recovery_and_discovery(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 13: discover_run recovers page references from manifest."""
    archiver.archive_page(
        sync_run_id="run_disc",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="100",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    refs = archiver.discover_run("run_disc", "douyin")
    assert len(refs) == 1
    assert refs[0].page_number == 1
    assert archiver.verify(refs[0]) is True


def test_14_missing_file_handling(archiver: DiskRawArchiver) -> None:
    """Case 14: Referencing non-existent file raises RawArchiveNotFoundError."""
    fake_ref = RawArchiveRef(
        platform="douyin",
        sync_run_id="run_none",
        page_number=99,
        path="data/raw/douyin/run_none/page_000099.json",
        sha256="0" * 64,
        byte_size=10,
        archived_at="2026-09-05T08:00:00Z",
        request_cursor="0",
        response_cursor="0",
        fetched_at="2026-09-05T08:00:00Z",
    )

    with pytest.raises(RawArchiveNotFoundError):
        archiver.verify(fake_ref)

    with pytest.raises(RawArchiveNotFoundError):
        archiver.read_page(fake_ref)


# ============================================================================
# Security, Path Safety & Secret Audit
# ============================================================================


def test_15_sensitive_field_detection(archiver: DiskRawArchiver) -> None:
    """Case 15: Payloads containing credentials (cookie, sessionid) raise RawArchiveSensitiveFieldError."""
    bad_payload_1 = {
        "status_code": 0,
        "cookie": "sessionid=abc123xyz",
    }
    with pytest.raises(RawArchiveSensitiveFieldError):
        archiver.archive_page(
            sync_run_id="run_sec",
            platform="douyin",
            page_number=1,
            request_cursor="0",
            response_cursor="0",
            raw_response=bad_payload_1,
            fetched_at="2026-09-05T08:00:00Z",
        )

    bad_payload_2 = {
        "status_code": 0,
        "nested": {"sessionid": "secret123"},
    }
    with pytest.raises(RawArchiveSensitiveFieldError):
        archiver.archive_page(
            sync_run_id="run_sec",
            platform="douyin",
            page_number=1,
            request_cursor="0",
            response_cursor="0",
            raw_response=bad_payload_2,
            fetched_at="2026-09-05T08:00:00Z",
        )


def test_16_path_safety_and_traversal_prevention(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 16: Path traversal attempts in platform or sync_run_id raise RawArchiveInvalidInputError."""
    with pytest.raises(RawArchiveInvalidInputError):
        archiver.archive_page(
            sync_run_id="../../etc",
            platform="douyin",
            page_number=1,
            request_cursor="0",
            response_cursor="0",
            raw_response=sample_payload,
            fetched_at="2026-09-05T08:00:00Z",
        )

    with pytest.raises(RawArchiveInvalidInputError):
        archiver.archive_page(
            sync_run_id="run_01",
            platform="douyin/nested",
            page_number=1,
            request_cursor="0",
            response_cursor="0",
            raw_response=sample_payload,
            fetched_at="2026-09-05T08:00:00Z",
        )


def test_17_safe_path_naming(archiver: DiskRawArchiver, sample_payload: dict[str, Any]) -> None:
    """Case 17: Filename format strictly uses page_{page_number:06d}.json; never user text."""
    ref = archiver.archive_page(
        sync_run_id="run_naming",
        platform="douyin",
        page_number=5,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    path = Path(ref.path)
    assert path.name == "page_000005.json"
    assert "江哥" not in ref.path
    assert "FDE" not in ref.path


def test_18_gitignore_coverage() -> None:
    """Case 18: Project .gitignore contains rule covering data/raw/."""
    gitignore_path = Path(__file__).resolve().parent.parent / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path = Path("G:/local_pc_project/.gitignore")
    assert gitignore_path.exists()
    content = gitignore_path.read_text(encoding="utf-8")
    assert "data/raw/" in content or "data/*" in content


def test_19_windows_path_compatibility(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 19: Paths normalize with forward slashes for cross-platform schema compatibility."""
    ref = archiver.archive_page(
        sync_run_id="run_win",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )
    assert "\\" not in ref.path
    assert "/" in ref.path


# ============================================================================
# Integration with C07 Canonical Transformer
# ============================================================================


def test_20_integration_with_c07_raw_ref(
    archiver: DiskRawArchiver,
    sample_payload: dict[str, Any],
) -> None:
    """Case 20: C06 RawArchiveRef generates valid raw_ref input for C07 CanonicalTransformer."""
    from src.collector.douyin import (
        DouyinCanonicalTransformer,
        RawRefContext,
        TransformContext,
    )

    # 1. Archive page via C06
    archive_ref = archiver.archive_page(
        sync_run_id="run_c07_int",
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="1788528929463841",
        raw_response=sample_payload,
        fetched_at="2026-09-05T08:00:00Z",
    )

    # 2. Extract item from raw page
    raw_item = sample_payload["aweme_list"][0]

    # 3. Create C07 RawRefContext via C06 archive_ref
    raw_ref_dict = archive_ref.to_raw_ref_dict(item_index=0)
    raw_ref_ctx = RawRefContext(
        archive_file=raw_ref_dict["archive_file"],
        sha256=raw_ref_dict["sha256"],
        item_index=raw_ref_dict["item_index"],
        platform_raw_type=raw_ref_dict["platform_raw_type"],
    )

    # 4. Transform via C07
    transformer = DouyinCanonicalTransformer(validate_schema=True)
    ctx = TransformContext(
        sync_run_id="run_c07_int",
        observed_at="2026-09-05T08:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="1788528929463841",
        raw_ref=raw_ref_ctx,
    )

    result = transformer.transform_item(raw_item, ctx)
    assert result.validation_passed is True

    # 5. Check item raw_ref and observation raw_ref point accurately to C06 archive
    assert result.item["raw_ref"]["archive_file"] == archive_ref.path
    assert result.item["raw_ref"]["sha256"] == archive_ref.sha256
    assert result.item["raw_ref"]["item_index"] == 0

    assert result.observation["raw_ref"]["archive_file"] == archive_ref.path
    assert result.observation["raw_ref"]["sha256"] == archive_ref.sha256
    assert result.observation["raw_ref"]["item_index"] == 0
