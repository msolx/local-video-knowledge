"""Unit and integration test suite for Douyin Canonical Transform Engine (DY-C07).

Validates canonical schemas, state transitions (Cases A, B, C), determinism,
media types (video, image_album), tag extraction, chapters, and error handling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.collector.douyin import (
    CanonicalTransformResult,
    DouyinCanonicalTransformer,
    PriorCollectionState,
    RawRefContext,
    TransformContext,
    TransformError,
    TransformInvalidRawError,
    TransformMissingIdentityError,
    TransformSchemaValidationError,
)


@pytest.fixture
def transformer() -> DouyinCanonicalTransformer:
    return DouyinCanonicalTransformer(validate_schema=True)


@pytest.fixture
def pagination_dir() -> Path:
    return Path("G:/antigravity-cli/dy/listcollection_pagination")


@pytest.fixture
def sample_video_raw(pagination_dir: Path) -> dict[str, Any]:
    with open(pagination_dir / "page_01.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["aweme_list"][0]


@pytest.fixture
def sample_image_album_raw(pagination_dir: Path) -> dict[str, Any]:
    with open(pagination_dir / "page_07.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    # Find aweme_type == 68 item
    for item in data["aweme_list"]:
        if item.get("aweme_type") == 68:
            return item
    raise ValueError("No image album found in page_07.json")


@pytest.fixture
def sample_refav_raw(pagination_dir: Path) -> dict[str, Any]:
    with open(pagination_dir / "page_01.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    for item in data["aweme_list"]:
        if str(item.get("aweme_id")) == "6611417973221494020":
            return item
    raise ValueError("Item 6611417973221494020 not found in page_01.json")


# ============================================================================
# Core Transformation & Schema Validation Tests
# ============================================================================


def test_case_1_video_canonical_transformation_schema_valid(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 1: Standard video item transforms into valid canonical item and observation."""
    ctx = TransformContext(
        sync_run_id="sync_run_20260905_01",
        observed_at="2026-09-05T08:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="1788528929463841",
        raw_ref=RawRefContext(
            archive_file="archives/douyin/page_01.json",
            sha256="c" * 64,
            item_index=0,
        ),
    )

    result = transformer.transform_item(sample_video_raw, ctx)
    assert isinstance(result, CanonicalTransformResult)
    assert result.content_type == "video"
    assert result.platform_content_id == str(sample_video_raw["aweme_id"])
    assert result.is_first_observation is True
    assert result.is_reappearance is False

    item = result.item
    assert item["schema_version"] == "collection-item-v1"
    assert item["platform"] == "douyin"
    assert item["source_type"] == "collection"
    assert item["source_url"] == f"https://www.douyin.com/video/{result.platform_content_id}"
    assert item["media"]["images"] is None
    assert item["media"]["has_audio"] is True
    assert item["media"]["duration_seconds"] is not None
    assert item["media"]["duration_ms"] is not None

    obs = result.observation
    assert obs["schema_version"] == "collection-observation-v1"
    assert obs["observation_id"] == f"obs_{ctx.sync_run_id}_{result.platform_content_id}"
    assert obs["page_number"] == 1
    assert obs["position_in_page"] == 0
    assert obs["global_rank_seen"] == 1
    assert obs["page_request_cursor"] == "0"
    assert obs["page_response_cursor"] == "1788528929463841"


def test_case_2_image_album_canonical_transformation_schema_valid(
    transformer: DouyinCanonicalTransformer,
    sample_image_album_raw: dict[str, Any],
) -> None:
    """Case 2: Image album (aweme_type=68) transforms into valid image_album canonical item."""
    ctx = TransformContext(
        sync_run_id="sync_run_20260905_02",
        observed_at="2026-09-05T08:15:00Z",
        page_number=7,
        position_in_page=1,
        global_rank_seen=62,
        request_cursor="1788528929463847",
        response_cursor="0",
        raw_ref=RawRefContext(
            archive_file="archives/douyin/page_07.json",
            sha256="d" * 64,
            item_index=1,
        ),
    )

    result = transformer.transform_item(sample_image_album_raw, ctx)
    assert result.content_type == "image_album"
    item = result.item
    assert item["content_type"] == "image_album"
    assert isinstance(item["media"]["images"], list)
    assert len(item["media"]["images"]) > 0

    first_image = item["media"]["images"][0]
    assert first_image["index"] == 0
    assert first_image["url"].startswith("http")
    assert first_image["width"] is not None
    assert first_image["height"] is not None


def test_case_3_re_favorite_item_reappearance(
    transformer: DouyinCanonicalTransformer,
    sample_refav_raw: dict[str, Any],
) -> None:
    """Case 3: Historical 2018 item (6611417973221494020) reappearing after deletion."""
    ctx = TransformContext(
        sync_run_id="sync_run_20260905_03",
        observed_at="2026-09-05T08:30:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="1788528929463841",
    )
    prior = PriorCollectionState(
        exists=True,
        active=False,
        first_seen_at="2026-08-01T12:00:00Z",
        last_seen_at="2026-08-15T12:00:00Z",
        observed_count=10,
        last_seen_position=45,
    )

    result = transformer.transform_item(sample_refav_raw, ctx, prior=prior)
    assert result.is_first_observation is False
    assert result.is_reappearance is True

    coll = result.item["collection"]
    assert coll["first_seen_at"] == "2026-08-01T12:00:00Z"  # Unchanged
    assert coll["last_seen_at"] == "2026-09-05T08:30:00Z"
    assert coll["reappeared_at"] == "2026-09-05T08:30:00Z"
    assert coll["active"] is True
    assert coll["observed_count"] == 11
    assert coll["last_seen_position"] == 1

    # Published at strictly reflects create_time from 2018
    assert result.item["published_at"].startswith("2018-")


# ============================================================================
# State Transition Matrix Tests (Case A, B, C)
# ============================================================================


def test_case_4_state_transition_case_a_brand_new_item(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 4: Prior is None -> brand-new item initialization."""
    ctx = TransformContext(
        sync_run_id="run_a",
        observed_at="2026-09-05T09:00:00Z",
        page_number=1,
        position_in_page=3,
        global_rank_seen=4,
        request_cursor="0",
        response_cursor="100",
    )

    res = transformer.transform_item(sample_video_raw, ctx, prior=None)
    assert res.is_first_observation is True
    assert res.is_reappearance is False

    coll = res.item["collection"]
    assert coll["first_seen_at"] == "2026-09-05T09:00:00Z"
    assert coll["last_seen_at"] == "2026-09-05T09:00:00Z"
    assert coll["reappeared_at"] is None
    assert coll["active"] is True
    assert coll["observed_count"] == 1
    assert coll["last_seen_position"] == 4


def test_case_5_state_transition_case_b_continuing_active(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 5: Prior exists and active -> continuing observation."""
    ctx = TransformContext(
        sync_run_id="run_b",
        observed_at="2026-09-05T09:10:00Z",
        page_number=2,
        position_in_page=2,
        global_rank_seen=12,
        request_cursor="100",
        response_cursor="200",
    )
    prior = PriorCollectionState(
        exists=True,
        active=True,
        first_seen_at="2026-09-01T00:00:00Z",
        last_seen_at="2026-09-04T00:00:00Z",
        reappeared_at=None,
        observed_count=4,
        last_seen_position=10,
    )

    res = transformer.transform_item(sample_video_raw, ctx, prior=prior)
    assert res.is_first_observation is False
    assert res.is_reappearance is False

    coll = res.item["collection"]
    assert coll["first_seen_at"] == "2026-09-01T00:00:00Z"
    assert coll["last_seen_at"] == "2026-09-05T09:10:00Z"
    assert coll["reappeared_at"] is None
    assert coll["active"] is True
    assert coll["observed_count"] == 5
    assert coll["last_seen_position"] == 12


def test_case_6_state_transition_case_c_inactive_to_reappearance(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 6: Prior exists and inactive -> reappearance (re-favorite)."""
    ctx = TransformContext(
        sync_run_id="run_c",
        observed_at="2026-09-05T09:20:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="100",
    )
    prior = PriorCollectionState(
        exists=True,
        active=False,
        first_seen_at="2026-07-01T00:00:00Z",
        last_seen_at="2026-07-10T00:00:00Z",
        observed_count=2,
    )

    res = transformer.transform_item(sample_video_raw, ctx, prior=prior)
    assert res.is_first_observation is False
    assert res.is_reappearance is True

    coll = res.item["collection"]
    assert coll["first_seen_at"] == "2026-07-01T00:00:00Z"
    assert coll["last_seen_at"] == "2026-09-05T09:20:00Z"
    assert coll["reappeared_at"] == "2026-09-05T09:20:00Z"
    assert coll["active"] is True
    assert coll["observed_count"] == 3
    assert coll["last_seen_position"] == 1


def test_case_7_reappearance_regression_no_propagation_across_runs(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 7: Reappearance is an observation event, not persistent state. Must not propagate."""
    # Run 1: prior inactive -> triggers reappearance
    prior_run1 = PriorCollectionState(
        exists=True,
        active=False,
        first_seen_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-10T00:00:00Z",
        observed_count=1,
    )
    ctx1 = TransformContext(
        sync_run_id="run_1",
        observed_at="2026-09-05T09:30:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="100",
    )
    res1 = transformer.transform_item(sample_video_raw, ctx1, prior=prior_run1)
    assert res1.is_reappearance is True
    assert res1.item["collection"]["active"] is True
    assert res1.item["collection"]["reappeared_at"] == "2026-09-05T09:30:00Z"
    assert res1.item["collection"]["observed_count"] == 2

    # Convert Run 1 output to persistent state (Prior for Run 2)
    prior_run2 = PriorCollectionState(
        exists=True,
        active=res1.item["collection"]["active"],  # True
        first_seen_at=res1.item["collection"]["first_seen_at"],
        last_seen_at=res1.item["collection"]["last_seen_at"],
        reappeared_at=res1.item["collection"]["reappeared_at"],
        observed_count=res1.item["collection"]["observed_count"],
    )

    # Run 2: same item observed again in subsequent run
    ctx2 = TransformContext(
        sync_run_id="run_2",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="100",
    )
    res2 = transformer.transform_item(sample_video_raw, ctx2, prior=prior_run2)
    assert res2.is_reappearance is False  # Must return False, event is NOT sticky!
    assert res2.item["collection"]["reappeared_at"] == "2026-09-05T09:30:00Z"  # Preserved
    assert res2.item["collection"]["last_seen_at"] == "2026-09-05T10:00:00Z"
    assert res2.item["collection"]["observed_count"] == 3


# ============================================================================
# Architectural Invariant & Boundary Tests
# ============================================================================


def test_case_8_published_at_utc_conversion(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 8: published_at is strictly mapped from create_time (Unix seconds to UTC ISO)."""
    raw_copy = dict(sample_video_raw)
    raw_copy["create_time"] = 1700000000  # 2023-11-14T22:13:20+00:00

    ctx = TransformContext(
        sync_run_id="run_ts",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="0",
    )

    res = transformer.transform_item(raw_copy, ctx)
    assert res.item["published_at"].startswith("2023-11-14T22:13:20")
    # Verify published_at is NEVER equal to observed_at or first_seen_at
    assert res.item["published_at"] != res.item["collection"]["first_seen_at"]


def test_case_9_no_cursor_in_canonical_item(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 9: Pagination cursors must NEVER be injected into CollectionItem."""
    ctx = TransformContext(
        sync_run_id="run_cursor_check",
        observed_at="2026-09-05T10:00:00Z",
        page_number=2,
        position_in_page=1,
        global_rank_seen=15,
        request_cursor="1788528929463841",
        response_cursor="1788528929463842",
    )

    res = transformer.transform_item(sample_video_raw, ctx)
    item = res.item

    assert "cursor" not in item
    assert "page_request_cursor" not in item
    assert "page_response_cursor" not in item
    assert "next_cursor" not in item
    assert "max_cursor" not in item
    assert "cursor" not in item["collection"]


def test_case_10_cursors_in_collection_observation(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 10: Observation contains request and response cursors as decimal strings."""
    ctx = TransformContext(
        sync_run_id="run_obs_cursor",
        observed_at="2026-09-05T10:00:00Z",
        page_number=3,
        position_in_page=4,
        global_rank_seen=25,
        request_cursor="1788528929463842",
        response_cursor="1788528929463843",
    )

    res = transformer.transform_item(sample_video_raw, ctx)
    obs = res.observation

    assert obs["page_request_cursor"] == "1788528929463842"
    assert obs["page_response_cursor"] == "1788528929463843"
    assert isinstance(obs["page_request_cursor"], str)
    assert isinstance(obs["page_response_cursor"], str)


def test_case_11_purely_deterministic_output(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 11: 100 identical calls produce 100% byte-for-byte identical output."""
    ctx = TransformContext(
        sync_run_id="deterministic_run",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="100",
    )

    first_res = transformer.transform_item(sample_video_raw, ctx)
    first_item_json = json.dumps(first_res.item, sort_keys=True)
    first_obs_json = json.dumps(first_res.observation, sort_keys=True)

    for _ in range(100):
        res = transformer.transform_item(sample_video_raw, ctx)
        assert json.dumps(res.item, sort_keys=True) == first_item_json
        assert json.dumps(res.observation, sort_keys=True) == first_obs_json


def test_case_12_no_system_clock_or_random_uuid() -> None:
    """Case 12: Source code audit: transform.py does NOT call datetime.now or uuid."""
    import ast
    import inspect
    from src.collector.douyin import transform

    source = inspect.getsource(transform)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                assert func.attr not in ("now", "utcnow", "uuid4", "random")
            elif isinstance(func, ast.Name):
                assert func.id not in ("now", "utcnow", "uuid4")

    # Check imported module names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in ("uuid", "sqlite3", "aiosqlite", "requests", "httpx", "urllib")
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in ("uuid", "sqlite3", "aiosqlite", "requests", "httpx", "urllib")


# ============================================================================
# Error Taxonomy & Resiliency Tests
# ============================================================================


def test_case_13_missing_identity_raises_error(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 13: Raw item missing aweme_id raises TransformMissingIdentityError."""
    ctx = TransformContext(
        sync_run_id="run_err",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="0",
    )
    raw_bad = dict(sample_video_raw)
    del raw_bad["aweme_id"]

    with pytest.raises(TransformMissingIdentityError):
        transformer.transform_item(raw_bad, ctx)

    raw_empty = dict(sample_video_raw)
    raw_empty["aweme_id"] = "   "
    with pytest.raises(TransformMissingIdentityError):
        transformer.transform_item(raw_empty, ctx)


def test_case_14_missing_create_time_raises_error(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 14: Raw item missing create_time raises TransformInvalidRawError."""
    ctx = TransformContext(
        sync_run_id="run_err",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="0",
    )
    raw_bad = dict(sample_video_raw)
    del raw_bad["create_time"]

    with pytest.raises(TransformInvalidRawError):
        transformer.transform_item(raw_bad, ctx)


def test_case_15_invalid_raw_type_raises_error(
    transformer: DouyinCanonicalTransformer,
) -> None:
    """Case 15: Non-dict input raises TransformInvalidRawError."""
    ctx = TransformContext(
        sync_run_id="run_err",
        observed_at="2026-09-05T10:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="0",
    )

    with pytest.raises(TransformInvalidRawError):
        transformer.transform_item("not_a_dict", ctx)  # type: ignore[arg-type]


def test_case_16_schema_validation_failure_raises_error(
    transformer: DouyinCanonicalTransformer,
) -> None:
    """Case 16: Invalid schema structure raises TransformSchemaValidationError."""
    with pytest.raises(TransformSchemaValidationError):
        transformer.validate_item({"invalid_field": "test"})

    with pytest.raises(TransformSchemaValidationError):
        transformer.validate_observation({"invalid_obs": "test"})


# ============================================================================
# Field Extraction & Enrichment Tests
# ============================================================================


def test_case_17_extract_tags_deduplication_and_fallback(
    transformer: DouyinCanonicalTransformer,
) -> None:
    """Case 17: Tags extracted from text_extra and deduplicated, fallback to desc."""
    # From text_extra
    raw1 = {
        "text_extra": [
            {"hashtag_name": "ai"},
            {"hashtag_name": "#agent"},
            {"hashtag_name": "ai"},  # duplicate
        ],
        "desc": "Regular desc",
    }
    tags1 = transformer.extract_tags(raw1)
    assert tags1 == ["ai", "agent"]

    # Fallback to desc when text_extra is empty
    raw2 = {
        "text_extra": [],
        "desc": "Check out #Python and #MachineLearning #Python now!",
    }
    tags2 = transformer.extract_tags(raw2)
    assert tags2 == ["Python", "MachineLearning"]


def test_case_18_chapters_and_statistics_extraction(
    transformer: DouyinCanonicalTransformer,
) -> None:
    """Case 18: Chapters outline and statistics counters extracted properly."""
    raw = {
        "chapter_list": [
            {"timestamp": 0, "desc": "Introduction", "detail": "Starting"},
            {"timestamp": 35000, "desc": "Architecture"},
        ],
        "statistics": {
            "digg_count": 1200,
            "comment_count": 45,
            "share_count": 12,
            "collect_count": 350,
        },
    }

    chapters = transformer.extract_chapters(raw)
    assert chapters is not None
    assert len(chapters) == 2
    assert chapters[0]["timestamp_ms"] == 0
    assert chapters[0]["title"] == "Introduction"
    assert chapters[0]["description"] == "Starting"
    assert chapters[1]["timestamp_ms"] == 35000
    assert chapters[1]["description"] is None

    stats = transformer.extract_statistics(raw)
    assert stats is not None
    assert stats["digg_count"] == 1200
    assert stats["collect_count"] == 350


def test_case_19_legacy_compatibility_layer(
    transformer: DouyinCanonicalTransformer,
    sample_video_raw: dict[str, Any],
) -> None:
    """Case 19: Legacy local-video-knowledge compatibility layer populated accurately."""
    ctx = TransformContext(
        sync_run_id="run_legacy",
        observed_at="2026-09-05T11:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="0",
    )

    res = transformer.transform_item(sample_video_raw, ctx)
    legacy = res.item["legacy_compatibility"]

    assert legacy is not None
    assert legacy["collected_at"] == "2026-09-05T11:00:00Z"
    assert legacy["original_filename"] == f"{res.platform_content_id}.mp4"
    assert legacy["author_name"] == res.item["author"]["display_name"]
    assert legacy["author_id"] in (res.item["author"]["sec_uid"], res.item["author"]["platform_author_id"])


def test_case_20_all_pagination_fixtures_pass_validation(
    transformer: DouyinCanonicalTransformer,
    pagination_dir: Path,
) -> None:
    """Case 20: 100% of items across all pages in listcollection_pagination pass validation."""
    page_files = sorted(pagination_dir.glob("page_*.json"))
    assert len(page_files) >= 5, f"Expected multiple page fixtures, found {len(page_files)}"

    total_items = 0
    for page_idx, page_file in enumerate(page_files, start=1):
        with open(page_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        aweme_list = data.get("aweme_list") or []

        for pos_idx, raw_item in enumerate(aweme_list):
            total_items += 1
            ctx = TransformContext(
                sync_run_id=f"sync_sweep_{page_idx}",
                observed_at="2026-09-05T12:00:00Z",
                page_number=page_idx,
                position_in_page=pos_idx,
                global_rank_seen=total_items,
                request_cursor=str(page_idx),
                response_cursor=str(page_idx + 1),
                raw_ref=RawRefContext(
                    archive_file=str(page_file.name),
                    sha256="e" * 64,
                    item_index=pos_idx,
                ),
            )
            res = transformer.transform_item(raw_item, ctx)
            assert res.validation_passed is True
            assert res.platform_content_id

    assert total_items >= 25, f"Expected at least 25 items validated, got {total_items}"
