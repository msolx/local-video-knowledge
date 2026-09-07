"""Comprehensive unit and orchestration tests for DouyinSyncEngine (DY-C05).

Verifies authoritative invariants:
1. Incremental sync ALWAYS begins at cursor="0".
2. Strategy A (first-known early stop) is strictly prohibited.
3. Safe watermark boundary stopping condition.
4. Zero-change immediate safe stop.
5. In-run content deduplication.
6. Failure rollback guarantee (no watermark advance, no staging leak).
7. Dry-run safety (staging cleared, no watermark change).
8. Backfill mode with limit.
9. Reappearance handling (inactive item becomes active).
10. Multi-scope account isolation.
11. Stale run recovery at startup under service lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.collector.base import utcnow_iso
from src.collector.douyin.config import DouyinCollectorConfig
from src.collector.douyin.source_client import CollectionPage, DouyinSourceClient
from src.collector.douyin.sync_engine import DouyinSyncEngine, generate_sync_run_id
from src.collector.douyin.transform import DouyinCanonicalTransformer
from src.collector.raw_archive import DiskRawArchiver
from src.collector.repository import SqliteMetadataRepository
from src.collector.repository_models import SyncRunStatus


def make_raw_aweme(aweme_id: str, title: str = "Test Video", create_time: int = 1725519600) -> dict[str, Any]:
    return {
        "aweme_id": aweme_id,
        "desc": title,
        "create_time": create_time,
        "author": {
            "uid": f"uid_{aweme_id[:6]}",
            "nickname": f"Creator {aweme_id[:4]}",
            "avatar_thumb": {"url_list": ["https://p3.douyinpic.com/avatar.jpg"]},
        },
        "video": {
            "duration": 15000,
            "width": 1080,
            "height": 1920,
            "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpg"], "uri": "tos-cn-v-0000/cover"},
            "play_addr": {"uri": f"tos-cn-v-0000/video_{aweme_id}"},
        },
    }


def make_collection_page(
    cursor_req: str,
    cursor_resp: str,
    has_more: bool,
    items: list[dict[str, Any]],
) -> CollectionPage:
    raw_response = {
        "status_code": 0,
        "cursor": int(cursor_resp) if cursor_resp.isdigit() else cursor_resp,
        "has_more": 1 if has_more else 0,
        "aweme_list": items,
    }
    return CollectionPage(
        request_cursor=cursor_req,
        response_cursor=cursor_resp,
        has_more=has_more,
        items=items,
        raw_response=raw_response,
        http_status=200,
        platform_status=0,
        fetched_at=utcnow_iso(),
        latency_ms=45.0,
    )


@pytest.fixture
def test_env(tmp_path: Path):
    raw_root = tmp_path / "raw"
    db_path = tmp_path / "metadata.db"
    config = DouyinCollectorConfig(
        raw_archive_root=raw_root,
        database_path=db_path,
        page_size=2,
    )
    repo = SqliteMetadataRepository(db_path, auto_init=True)
    archiver = DiskRawArchiver(raw_root)
    transformer = DouyinCanonicalTransformer(validate_schema=True)
    mock_source = MagicMock(spec=DouyinSourceClient)

    engine = DouyinSyncEngine(
        config=config,
        repository=repo,
        archiver=archiver,
        transformer=transformer,
        source_client=mock_source,
    )
    return {
        "engine": engine,
        "repo": repo,
        "archiver": archiver,
        "mock_source": mock_source,
        "config": config,
    }


def test_01_incremental_sync_baseline_no_watermark(test_env) -> None:
    """Baseline sync on empty database pages until has_more == 0 and commits candidate watermark from Page 1."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    p1_items = [make_raw_aweme("1001"), make_raw_aweme("1002")]
    p2_items = [make_raw_aweme("1003")]

    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "1788536000000000", True, p1_items),
        make_collection_page("1788536000000000", "0", False, p2_items),
    ]

    record = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    assert record.metrics["items_seen"] == 3
    assert record.metrics["new_items"] == 3
    assert record.metrics["pages_fetched"] == 2

    # Invariant: watermark committed is Page 1 response_cursor
    state = repo.get_sync_state(scope_id=scope_id)
    assert state is not None
    assert state.committed_watermark_cursor == "1788536000000000"

    # All items exist in collection_items
    assert repo.get_item("1001", scope_id=scope_id) is not None
    assert repo.get_item("1002", scope_id=scope_id) is not None
    assert repo.get_item("1003", scope_id=scope_id) is not None


def test_02_strategy_a_fail_guard_re_favorite_qw04(test_env) -> None:
    """FAIL-GUARD TEST: Verifies QW-04 counterexample where an existing item is encountered on Page 1.

    Engine MUST NOT stop at Page 1! It must continue to Page 2 to capture NEW_1.
    """
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Seed DB with historical state: W0 = 1788534410707763, OLD_K exists
    repo.begin_sync_run("run_seed", scope_id=scope_id)
    repo.stage_item(
        "run_seed",
        {
            "schema_version": "collection-item-v1",
            "platform": "douyin",
            "source_type": "collection",
            "platform_content_id": "OLD_K",
            "source_url": "https://www.douyin.com/video/OLD_K",
            "content_type": "video",
            "title": "Old Video",
            "collection": {"first_seen_at": "2026-09-04T00:00:00Z", "last_seen_at": "2026-09-04T00:00:00Z", "active": True, "observed_count": 1},
            "author": {"platform_user_id": "u1", "display_name": "Author 1"},
            "published_at": "2026-09-03T00:00:00Z",
            "media": {"duration_seconds": 10},
        },
        scope_id=scope_id,
    )
    repo.finalize_success("run_seed", candidate_watermark="1788534410707763", scope_id=scope_id)

    # Now mock subsequent sync:
    # Page 1 contains [NEW_2, OLD_K], cursor="1788536229251543" > W0
    # Page 2 contains [NEW_1, OTHER], cursor="1788534410707763" == W0 (hits watermark boundary)
    p1_items = [make_raw_aweme("NEW_2"), make_raw_aweme("OLD_K")]
    p2_items = [make_raw_aweme("NEW_1"), make_raw_aweme("OTHER")]

    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "1788536229251543", True, p1_items),
        make_collection_page("1788536229251543", "1788534410707763", True, p2_items),
    ]

    record = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    # MUST have fetched 2 pages, discovering both NEW_2 and NEW_1!
    assert record.metrics["pages_fetched"] == 2
    assert record.metrics["new_items"] >= 2
    assert record.metrics["known_items"] >= 1

    # Check both NEW_2 and NEW_1 exist
    assert repo.get_item("NEW_2", scope_id=scope_id) is not None
    assert repo.get_item("NEW_1", scope_id=scope_id) is not None

    # Watermark advanced to Page 1 cursor
    state = repo.get_sync_state(scope_id=scope_id)
    assert state.committed_watermark_cursor == "1788536229251543"


def test_03_zero_change_immediate_safe_stop(test_env) -> None:
    """When no new items were favorited, Page 1 response_cursor <= committed_watermark.

    Safe stop triggers immediately on Page 1 with 0 new items.
    """
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    w0 = "1788534410707763"
    repo.begin_sync_run("run_seed", scope_id=scope_id)
    repo.finalize_success("run_seed", candidate_watermark=w0, scope_id=scope_id)

    # Page 1 returns response_cursor == w0
    items = [make_raw_aweme("K1"), make_raw_aweme("K2")]
    mock_source.fetch_collection_page.return_value = make_collection_page("0", w0, True, items)

    record = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    assert record.metrics["pages_fetched"] == 1
    assert record.stop_reason == "watermark_reached"
    assert record.candidate_watermark_cursor == w0


def test_04_in_run_content_deduplication(test_env) -> None:
    """Duplicate items appearing within the same sync run are deduplicated."""
    engine: DouyinSyncEngine = test_env["engine"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Same item "DUP_01" appears on page 1 and page 2
    p1_items = [make_raw_aweme("DUP_01"), make_raw_aweme("1002")]
    p2_items = [make_raw_aweme("DUP_01"), make_raw_aweme("1003")]

    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "500", True, p1_items),
        make_collection_page("500", "0", False, p2_items),
    ]

    record = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    assert record.metrics["duplicate_in_run"] == 1
    assert record.metrics["items_seen"] == 3  # DUP_01 counted once, 1002, 1003


def test_05_failure_rollback_guarantee(test_env) -> None:
    """If an error occurs mid-run, committed watermark is NOT advanced,

    and staged items are NOT merged into collection_items.
    """
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    w0 = "100"
    repo.begin_sync_run("run_seed", scope_id=scope_id)
    repo.finalize_success("run_seed", candidate_watermark=w0, scope_id=scope_id)

    # Page 1 succeeds, Page 2 throws error
    p1_items = [make_raw_aweme("LEAK_01")]
    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "200", True, p1_items),
        RuntimeError("Platform connection dropped"),
    ]

    with pytest.raises(Exception):
        engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    # Watermark MUST NOT advance!
    state = repo.get_sync_state(scope_id=scope_id)
    assert state.committed_watermark_cursor == w0

    # Item LEAK_01 MUST NOT exist in collection_items!
    assert repo.get_item("LEAK_01", scope_id=scope_id) is None


def test_06_dry_run_safety(test_env) -> None:
    """Dry-run fetches and archives, but rolls back staging without advancing watermark."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    p1_items = [make_raw_aweme("DRY_01")]
    mock_source.fetch_collection_page.return_value = make_collection_page("0", "0", False, p1_items)

    record = engine.sync(scope_id=scope_id, dry_run=True, inter_page_delay_sec=0)

    # In dry-run, record is finalized as FAILED/aborted
    assert record.status == SyncRunStatus.FAILED.value
    assert "DRY_RUN" in (record.error_code or "")

    # Item was NOT committed to collection_items
    assert repo.get_item("DRY_01", scope_id=scope_id) is None


def test_07_backfill_mode_pagination(test_env) -> None:
    """Backfill mode ignores committed watermark and stops when limit is reached."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    w0 = "900"
    repo.begin_sync_run("run_seed", scope_id=scope_id)
    repo.finalize_success("run_seed", candidate_watermark=w0, scope_id=scope_id)

    # Page 1 returns response_cursor = "800" <= w0.
    # In incremental mode, this would stop. In backfill mode, it continues!
    p1 = [make_raw_aweme("BF_1"), make_raw_aweme("BF_2")]
    p2 = [make_raw_aweme("BF_3"), make_raw_aweme("BF_4")]

    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "800", True, p1),
        make_collection_page("800", "700", True, p2),
    ]

    record = engine.backfill(scope_id=scope_id, limit=3, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    # Paged through 2 pages because limit=3 required Page 2
    assert record.metrics["pages_fetched"] == 2
    assert record.stop_reason == "backfill_limit_reached"


def test_08_reappearance_handling_in_engine(test_env) -> None:
    """An item previously inactive becomes active and reappeared_items is incremented."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Seed DB with an INACTIVE item
    repo.begin_sync_run("run_seed", scope_id=scope_id)
    repo.stage_item(
        "run_seed",
        {
            "schema_version": "collection-item-v1",
            "platform": "douyin",
            "source_type": "collection",
            "platform_content_id": "REAPP_01",
            "source_url": "https://www.douyin.com/video/REAPP_01",
            "content_type": "video",
            "title": "Inactive Video",
            "collection": {"first_seen_at": "2026-09-01T00:00:00Z", "last_seen_at": "2026-09-01T00:00:00Z", "active": False, "observed_count": 1},
            "author": {"platform_user_id": "u1", "display_name": "Author 1"},
            "published_at": "2026-09-01T00:00:00Z",
            "media": {"duration_seconds": 10},
        },
        scope_id=scope_id,
    )
    repo.finalize_success("run_seed", candidate_watermark="100", scope_id=scope_id)

    # Now item reappears in new sync run
    p1 = [make_raw_aweme("REAPP_01")]
    mock_source.fetch_collection_page.return_value = make_collection_page("0", "0", False, p1)

    record = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    assert record.metrics["reappeared_items"] == 1

    # Check that item is now active in DB
    item = repo.get_item("REAPP_01", scope_id=scope_id)
    assert item["collection"]["active"] is True
    assert item["collection"]["observed_count"] == 2
    assert item["collection"]["reappeared_at"] is not None


def test_09_account_scope_isolation(test_env) -> None:
    """Sync engine enforces strict scope isolation: Account A and B do not collide."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]

    scope_a = "douyin:dyacct_aaaaaaaaaaaaaaaa"
    scope_b = "douyin:dyacct_bbbbbbbbbbbbbbbb"

    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "0", False, [make_raw_aweme("SHARED_ID")]),
        make_collection_page("0", "0", False, [make_raw_aweme("SHARED_ID")]),
    ]

    engine.sync(scope_id=scope_a, inter_page_delay_sec=0)
    engine.sync(scope_id=scope_b, inter_page_delay_sec=0)

    # Item exists in both scopes independently
    assert repo.get_item("SHARED_ID", scope_id=scope_a) is not None
    assert repo.get_item("SHARED_ID", scope_id=scope_b) is not None


def test_10_stale_run_recovered_under_service_lock(test_env) -> None:
    """Engine automatically recovers stale RUNNING runs at startup."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Simulate crashed run left in RUNNING
    repo.begin_sync_run("run_crashed", scope_id=scope_id)
    assert repo.get_sync_run("run_crashed").is_running

    mock_source.fetch_collection_page.return_value = make_collection_page("0", "0", False, [make_raw_aweme("ITEM_OK")])

    engine.sync(scope_id=scope_id, inter_page_delay_sec=0)

    # Crashed run is now ABORTED
    assert repo.get_sync_run("run_crashed").is_aborted


def test_11_initial_sync_max_pages_does_not_advance_watermark(test_env) -> None:
    """CRITICAL BUG FIX TEST: Initial sync hitting max_pages MUST NOT advance head watermark.

    Must preserve history_complete=False and set backfill_checkpoint_cursor.
    """
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    p1 = [make_raw_aweme("AWEME_01"), make_raw_aweme("AWEME_02")]
    mock_source.fetch_collection_page.return_value = make_collection_page("0", "1788534410707763", True, p1)

    record = engine.sync(scope_id=scope_id, max_pages=1, inter_page_delay_sec=0)

    assert record.status == SyncRunStatus.COMPLETED.value
    assert record.stop_reason == "max_pages_reached"
    # Watermark must NOT be committed!
    assert record.candidate_watermark_cursor is None

    state = repo.get_sync_state(scope_id=scope_id)
    assert state is not None
    assert state.incremental_head_watermark_cursor is None
    assert state.committed_watermark_cursor is None
    assert state.history_complete is False
    assert state.backfill_checkpoint_cursor == "1788534410707763"


def test_12_followup_run_after_incomplete_bootstrap_continues_paging(test_env) -> None:
    """Follow-up run when history_complete=False must NOT early stop at Page 1 boundary.

    It must page through to has_more=0, complete history, and commit the watermark.
    """
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Step 1: Incomplete bootstrap (max_pages=1)
    p1 = [make_raw_aweme("A1")]
    mock_source.fetch_collection_page.return_value = make_collection_page("0", "800", True, p1)
    engine.sync(scope_id=scope_id, max_pages=1, inter_page_delay_sec=0)

    state1 = repo.get_sync_state(scope_id=scope_id)
    assert state1.history_complete is False

    # Step 2: Run 2 starts at cursor 0. Page 1 returns response_cursor 800 (has_more=1), Page 2 returns has_more=0
    p2 = [make_raw_aweme("A2")]
    mock_source.fetch_collection_page.side_effect = [
        make_collection_page("0", "800", True, p1),
        make_collection_page("800", "0", False, p2),
    ]

    record2 = engine.sync(scope_id=scope_id, inter_page_delay_sec=0)
    assert record2.status == SyncRunStatus.COMPLETED.value
    assert record2.metrics["pages_fetched"] == 2
    assert record2.stop_reason == "end_of_collection"
    assert record2.candidate_watermark_cursor == "800"

    state2 = repo.get_sync_state(scope_id=scope_id)
    assert state2.history_complete is True
    assert state2.incremental_head_watermark_cursor == "800"
    assert state2.backfill_checkpoint_cursor is None


def test_13_backfill_resume_from_checkpoint(test_env) -> None:
    """Backfill can resume from backfill_checkpoint_cursor to complete history."""
    engine: DouyinSyncEngine = test_env["engine"]
    repo: SqliteMetadataRepository = test_env["repo"]
    mock_source: MagicMock = test_env["mock_source"]
    scope_id = "douyin:dyacct_1111222233334444"

    # Incomplete initial run sets backfill_checkpoint
    mock_source.fetch_collection_page.return_value = make_collection_page("0", "800", True, [make_raw_aweme("A1")])
    engine.sync(scope_id=scope_id, max_pages=1, inter_page_delay_sec=0)

    # Resume backfill from checkpoint "800"
    mock_source.fetch_collection_page.return_value = make_collection_page("800", "0", False, [make_raw_aweme("A2")])
    bf_record = engine.backfill(scope_id=scope_id, resume=True, inter_page_delay_sec=0)

    assert bf_record.status == SyncRunStatus.COMPLETED.value
    assert bf_record.stop_reason == "end_of_collection"

    state = repo.get_sync_state(scope_id=scope_id)
    assert state.history_complete is True
    assert state.backfill_checkpoint_cursor is None

