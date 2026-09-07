"""Comprehensive unit tests for IncrementalSyncPolicy (DY-C05).

Verifies authoritative QW-04 invariants:
1. Strategy A (first-known early stop) is strictly prohibited.
2. Safe boundary early stop occurs when response_cursor <= committed_watermark.
3. Candidate watermark is established from Page 1 and preserved across pages.
4. Terminal collection termination when has_more == 0 or items_count == 0.
5. Backfill mode bypasses watermark stop and honors limits.
6. Loop detection guards against infinite repeated cursors.
7. Exact 64-bit integer precision for numeric cursor comparisons.
"""

from __future__ import annotations

import pytest

from src.collector.douyin.sync_policy import (
    IncrementalSyncPolicy,
    PageFacts,
    StopAction,
    StopReasonCode,
    is_watermark_reached,
)


def test_01_is_watermark_reached_numeric_and_string() -> None:
    # 64-bit millisecond timestamps
    w0 = "1788534410707763"
    w_newer = "1788536229251543"
    w_older = "1788528711713559"

    # w_newer is newer than w0, so watermark is NOT reached
    assert not is_watermark_reached(w_newer, w0)
    # w0 is equal to w0, watermark IS reached
    assert is_watermark_reached(w0, w0)
    # w_older is older than w0, watermark IS reached
    assert is_watermark_reached(w_older, w0)

    # String fallback for non-numeric cursors
    assert is_watermark_reached("cursor_abc", "cursor_abc")
    assert not is_watermark_reached("cursor_xyz", "cursor_abc")


def test_02_strategy_a_fail_guard_qw04_counterexample() -> None:
    """FAIL-GUARD TEST: Verifies that encountering an already known item on Page 1

    does NOT trigger premature stop. Strategy A (first-known stop) is strictly prohibited.
    Scenario from QW-04:
    Committed watermark W0 = 1788534410707763
    Page 1 returns: [NEW_2, OLD_K] (1 new, 1 known), response_cursor = 1788536229251543
    Page 2 returns: [NEW_1, 7681625905991157755] (1 new, 1 known), response_cursor = 1788534410707763
    """
    w0 = "1788534410707763"
    policy = IncrementalSyncPolicy(mode="incremental", committed_watermark=w0)

    # Page 1: contains known item OLD_K, but response_cursor > w0
    page1 = PageFacts(
        page_number=1,
        request_cursor="0",
        response_cursor="1788536229251543",
        has_more=1,
        items_count=2,
        new_items_count=1,
        known_items_count=1,  # <--- Crucial: OLD_K is known!
    )
    decision1 = policy.evaluate_page(page1)

    # MUST NOT STOP ON PAGE 1!
    assert decision1.action == StopAction.CONTINUE
    assert decision1.reason_code == StopReasonCode.HAS_MORE
    assert policy.candidate_watermark == "1788536229251543"

    # Page 2: response_cursor <= w0 (reaches historical boundary)
    page2 = PageFacts(
        page_number=2,
        request_cursor="1788536229251543",
        response_cursor="1788534410707763",
        has_more=1,
        items_count=2,
        new_items_count=1,  # NEW_1 captured!
        known_items_count=1,
    )
    decision2 = policy.evaluate_page(page2)

    # NOW safely stops at watermark boundary
    assert decision2.action == StopAction.STOP_SAFE_BOUNDARY
    assert decision2.reason_code == StopReasonCode.WATERMARK_REACHED
    # Candidate watermark is established from Page 1, NOT Page 2!
    assert decision2.candidate_watermark_cursor == "1788536229251543"


def test_03_zero_change_immediate_safe_stop() -> None:
    """When no new items were favorited since last sync, Page 1 response_cursor <= committed_watermark.

    Safe stop triggers immediately on Page 1.
    """
    w0 = "1788534410707763"
    policy = IncrementalSyncPolicy(mode="incremental", committed_watermark=w0)

    page1 = PageFacts(
        page_number=1,
        request_cursor="0",
        response_cursor=w0,
        has_more=1,
        items_count=10,
        new_items_count=0,
        known_items_count=10,
    )
    decision = policy.evaluate_page(page1)

    assert decision.action == StopAction.STOP_SAFE_BOUNDARY
    assert decision.reason_code == StopReasonCode.WATERMARK_REACHED
    assert decision.candidate_watermark_cursor == w0


def test_04_first_run_no_watermark_paginates_to_end() -> None:
    """When committed_watermark is None (initial sync), it pages through history until has_more == 0."""
    policy = IncrementalSyncPolicy(mode="incremental", committed_watermark=None)

    page1 = PageFacts(
        page_number=1,
        request_cursor="0",
        response_cursor="1788536000000000",
        has_more=1,
        items_count=10,
        new_items_count=10,
        known_items_count=0,
    )
    d1 = policy.evaluate_page(page1)
    assert d1.action == StopAction.CONTINUE
    assert policy.candidate_watermark == "1788536000000000"

    page2 = PageFacts(
        page_number=2,
        request_cursor="1788536000000000",
        response_cursor="1788530000000000",
        has_more=1,
        items_count=10,
        new_items_count=10,
        known_items_count=0,
    )
    d2 = policy.evaluate_page(page2)
    assert d2.action == StopAction.CONTINUE

    page3 = PageFacts(
        page_number=3,
        request_cursor="1788530000000000",
        response_cursor="0",
        has_more=0,
        items_count=5,
        new_items_count=5,
        known_items_count=0,
    )
    d3 = policy.evaluate_page(page3)
    assert d3.action == StopAction.STOP_TERMINAL
    assert d3.reason_code == StopReasonCode.END_OF_COLLECTION
    assert d3.candidate_watermark_cursor == "1788536000000000"


def test_05_empty_collection_page1() -> None:
    """When collection is empty, Page 1 returns 0 items."""
    policy = IncrementalSyncPolicy(mode="incremental", committed_watermark="1788534410707763")

    page1 = PageFacts(
        page_number=1,
        request_cursor="0",
        response_cursor="0",
        has_more=0,
        items_count=0,
    )
    d = policy.evaluate_page(page1)
    assert d.action == StopAction.STOP_TERMINAL
    assert d.reason_code == StopReasonCode.EMPTY_COLLECTION
    assert d.candidate_watermark_cursor is None


def test_06_max_pages_ceiling() -> None:
    """Enforces max_pages safety limit even if watermark not yet reached."""
    policy = IncrementalSyncPolicy(
        mode="incremental",
        committed_watermark="100",
        max_pages=2,
    )

    p1 = PageFacts(page_number=1, request_cursor="0", response_cursor="900", has_more=1, items_count=10)
    assert policy.evaluate_page(p1).action == StopAction.CONTINUE

    p2 = PageFacts(page_number=2, request_cursor="900", response_cursor="800", has_more=1, items_count=10)
    d2 = policy.evaluate_page(p2)
    assert d2.action == StopAction.STOP_LIMIT
    assert d2.reason_code == StopReasonCode.MAX_PAGES_REACHED
    assert d2.candidate_watermark_cursor is None
    assert d2.details["uncommitted_candidate_watermark"] == "900"


def test_07_backfill_bypasses_watermark_and_honors_limit() -> None:
    """In backfill mode, passing an older response_cursor <= committed_watermark does not stop."""
    w0 = "1788534410707763"
    policy = IncrementalSyncPolicy(
        mode="backfill",
        committed_watermark=w0,
        backfill_limit=25,
    )

    # Page 1 (10 items)
    p1 = PageFacts(page_number=1, request_cursor="0", response_cursor="1788534410707763", has_more=1, items_count=10)
    d1 = policy.evaluate_page(p1)
    # Even though response_cursor <= w0, backfill mode does NOT stop!
    assert d1.action == StopAction.CONTINUE

    # Page 2 (10 items, total 20)
    p2 = PageFacts(page_number=2, request_cursor="1788534410707763", response_cursor="1788528711713559", has_more=1, items_count=10)
    d2 = policy.evaluate_page(p2)
    assert d2.action == StopAction.CONTINUE

    # Page 3 (10 items, total 30 >= 25 limit)
    p3 = PageFacts(page_number=3, request_cursor="1788528711713559", response_cursor="1788520000000000", has_more=1, items_count=10)
    d3 = policy.evaluate_page(p3)
    assert d3.action == StopAction.STOP_LIMIT
    assert d3.reason_code == StopReasonCode.BACKFILL_LIMIT_REACHED


def test_08_infinite_loop_repeated_cursor_guard() -> None:
    """Guards against infinite pagination loop where response_cursor == request_cursor with has_more=1."""
    policy = IncrementalSyncPolicy(mode="incremental")

    p1 = PageFacts(page_number=1, request_cursor="0", response_cursor="500", has_more=1, items_count=10)
    assert policy.evaluate_page(p1).action == StopAction.CONTINUE

    # Page 2 returns same cursor "500" as request_cursor
    p2 = PageFacts(page_number=2, request_cursor="500", response_cursor="500", has_more=1, items_count=10)
    d2 = policy.evaluate_page(p2)
    assert d2.action == StopAction.STOP_ERROR
    assert d2.reason_code == StopReasonCode.LOOP_DETECTED


def test_09_policy_history_incomplete_bypasses_watermark_boundary() -> None:
    """When history_complete is False, watermark boundary check is bypassed to prevent premature stop."""
    policy = IncrementalSyncPolicy(
        mode="incremental",
        committed_watermark="100",
        history_complete=False,
    )

    # Page 1 returns response_cursor="50" <= "100"
    p1 = PageFacts(page_number=1, request_cursor="0", response_cursor="50", has_more=1, items_count=10)
    d1 = policy.evaluate_page(p1)

    # Must NOT stop at boundary because history is incomplete!
    assert d1.action == StopAction.CONTINUE
    assert d1.reason_code == StopReasonCode.HAS_MORE
    assert policy.candidate_watermark == "50"


def test_10_policy_stop_limit_clears_candidate_watermark() -> None:
    """STOP_LIMIT (max_pages or backfill_limit) must return candidate_watermark_cursor=None."""
    policy = IncrementalSyncPolicy(
        mode="incremental",
        committed_watermark="100",
        max_pages=1,
        history_complete=False,
    )

    p1 = PageFacts(page_number=1, request_cursor="0", response_cursor="500", has_more=1, items_count=10)
    d1 = policy.evaluate_page(p1)

    assert d1.action == StopAction.STOP_LIMIT
    assert d1.reason_code == StopReasonCode.MAX_PAGES_REACHED
    assert d1.candidate_watermark_cursor is None
    assert d1.details["uncommitted_candidate_watermark"] == "500"

