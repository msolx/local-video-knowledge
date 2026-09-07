"""Comprehensive test suite for DY-C09: Download Queue Producer & Outbox Engine.

Covers:
1. DownloadTask contract serialization, validation, and deterministic task_id computation.
2. DownloadEligibilityPolicy (inactive, already valid, reappearance missing, repair missing, new items).
3. Security boundary: stripping credentials, cookies, tokens; volatile acquisition hints.
4. Transactional Outbox pattern: commit-before-enqueue atomic transaction and rollback semantics.
5. OutboxDispatcher lifecycle: poll_pending, mark_dispatched, retry backoff, terminal failure.
6. Scope isolation, AssetStateProvider abstractions, and SyncEngine integration.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.collector.download_models import (
    AssetStateProvider,
    DownloadPriority,
    DownloadReason,
    DownloadTask,
    DownloadTaskStatus,
    InMemoryAssetStateProvider,
    NullAssetStateProvider,
    OutboxRecord,
    compute_download_task_id,
    utcnow_iso,
)
from src.collector.download_queue import (
    DownloadEligibilityDecision,
    DownloadEligibilityPolicy,
    DownloadOutboxDispatcher,
    DownloadQueueProducer,
)
from src.collector.douyin.transform import PriorCollectionState
from src.collector.repository import SqliteMetadataRepository


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def repo(tmp_path: Path) -> SqliteMetadataRepository:
    db_file = tmp_path / "metadata_test.db"
    return SqliteMetadataRepository(db_file)


@pytest.fixture
def sample_canonical_item() -> dict[str, Any]:
    return {
        "schema_version": "collection-item-v1",
        "platform": "douyin",
        "platform_content_id": "7123456789012345678",
        "content_type": "video",
        "source_url": "https://www.douyin.com/video/7123456789012345678",
        "title": "Sample Douyin Video Title",
        "published_at": "2026-09-01T12:00:00Z",
        "author": {
            "platform_user_id": "9988776655",
            "sec_uid": "MS4wLjABAAAA_sec_test",
            "nickname": "Test Creator",
        },
        "collection": {
            "active": True,
            "first_seen_at": "2026-09-05T10:00:00Z",
            "last_seen_at": "2026-09-05T10:00:00Z",
            "reappeared_at": None,
            "observed_count": 1,
            "last_seen_position": 1,
        },
        "media": {
            "download_input": {
                "play_addr_h264": "https://aweme.snssdk.com/video/tos/sample.mp4",
                "cookie": "sessionid=secret_cookie_leak;",
                "token": "secret_token_val",
                "width": 1080,
                "height": 1920,
            }
        },
    }


# =============================================================================
# 1. Contract & Deterministic Task ID Tests
# =============================================================================


def test_01_download_task_contract_serialization(sample_canonical_item: dict[str, Any]) -> None:
    """Test 01: DownloadTask serialization round-trip and deterministic JSON."""
    task = DownloadTask(
        task_id="dl_douyin_7123456789012345678_aabbccdd11223344",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="7123456789012345678",
        content_type="video",
        source_url="https://www.douyin.com/video/7123456789012345678",
        download_input={"play_addr": "https://example.com/video.mp4", "is_volatile_hint": True},
        priority=DownloadPriority.NEW_COLLECTION.value,
        reason=DownloadReason.NEW_COLLECTION_ITEM.value,
    )

    d = task.to_dict()
    assert d["schema_version"] == "download-task-v1"
    assert d["canonical_item_version"] == "collection-item-v1"
    assert d["priority"] == 10
    assert d["status"] == "PENDING"

    # Deterministic JSON
    raw_json = task.to_deterministic_json()
    assert isinstance(raw_json, str)

    # Reconstruct from dict
    reconstructed = DownloadTask.from_dict(d)
    assert reconstructed.task_id == task.task_id
    assert reconstructed.platform == task.platform
    assert reconstructed.priority == task.priority
    assert reconstructed.download_input == task.download_input


def test_02_deterministic_task_id_computation() -> None:
    """Test 02: Deterministic task ID generation is stable and independent of timestamps/run IDs."""
    id1 = compute_download_task_id("douyin", "douyin:dyacct_1", "71234567890", "video")
    id2 = compute_download_task_id("douyin", "douyin:dyacct_1", "71234567890", "video")
    assert id1 == id2
    assert id1.startswith("dl_douyin_71234567890_")

    # Scope separation produces distinct IDs
    id_scope2 = compute_download_task_id("douyin", "douyin:dyacct_2", "71234567890", "video")
    assert id1 != id_scope2

    # Content type separation produces distinct IDs
    id_type_img = compute_download_task_id("douyin", "douyin:dyacct_1", "71234567890", "image")
    assert id1 != id_type_img


# =============================================================================
# 2. Eligibility Policy Tests
# =============================================================================


def test_03_eligibility_new_active_item(sample_canonical_item: dict[str, Any]) -> None:
    """Test 03: First-seen active item is eligible with NEW_COLLECTION_ITEM."""
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=False, active=False),
        asset_provider=NullAssetStateProvider(),
    )
    assert decision.is_eligible is True
    assert decision.reason == DownloadReason.NEW_COLLECTION_ITEM
    assert decision.priority == DownloadPriority.NEW_COLLECTION.value


def test_04_eligibility_inactive_item(sample_canonical_item: dict[str, Any]) -> None:
    """Test 04: Inactive item never triggers download."""
    sample_canonical_item["collection"]["active"] = False
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=False, active=False),
        asset_provider=NullAssetStateProvider(),
    )
    assert decision.is_eligible is False
    assert decision.reason is None
    assert decision.priority == 0


def test_05_eligibility_asset_already_valid(sample_canonical_item: dict[str, Any]) -> None:
    """Test 05: Item with already valid physical asset skips download (no duplicate enqueue)."""
    provider = InMemoryAssetStateProvider()
    provider.add_asset("douyin", "douyin:dyacct_test", "7123456789012345678")

    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=False, active=False),
        asset_provider=provider,
    )
    assert decision.is_eligible is False
    assert decision.reason is None


def test_06_eligibility_reappearance_missing_asset(sample_canonical_item: dict[str, Any]) -> None:
    """Test 06: Reappeared item with missing asset triggers REAPPEARANCE_ASSET_MISSING (priority 5)."""
    sample_canonical_item["collection"]["reappeared_at"] = "2026-09-05T11:00:00Z"
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=True, active=True, reappeared_at="2026-09-05T11:00:00Z"),
        is_current_reappearance=True,
        asset_provider=NullAssetStateProvider(),
    )
    assert decision.is_eligible is True
    assert decision.reason == DownloadReason.REAPPEARANCE_ASSET_MISSING
    assert decision.priority == DownloadPriority.REPAIR.value


def test_06b_historical_reappearance_without_active_event_is_repair(sample_canonical_item: dict[str, Any]) -> None:
    """Test 06b: Item with historical reappeared_at but NOT a reappearance event in current run is ASSET_MISSING_REPAIR."""
    sample_canonical_item["collection"]["reappeared_at"] = "2026-09-05T11:00:00Z"
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=True, active=True, reappeared_at="2026-09-05T11:00:00Z"),
        is_current_reappearance=False,  # Normal active observation in subsequent run!
        asset_provider=NullAssetStateProvider(),
    )
    assert decision.is_eligible is True
    # MUST be ASSET_MISSING_REPAIR, NOT REAPPEARANCE_ASSET_MISSING!
    assert decision.reason == DownloadReason.ASSET_MISSING_REPAIR
    assert decision.priority == DownloadPriority.REPAIR.value


def test_07_eligibility_known_item_missing_asset_repair(sample_canonical_item: dict[str, Any]) -> None:
    """Test 07: Known item (prior exists) missing asset triggers ASSET_MISSING_REPAIR (priority 5)."""
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:dyacct_test",
        prior=PriorCollectionState(exists=True, active=True),
        asset_provider=NullAssetStateProvider(),
    )
    assert decision.is_eligible is True
    assert decision.reason == DownloadReason.ASSET_MISSING_REPAIR
    assert decision.priority == DownloadPriority.REPAIR.value


# =============================================================================
# 3. Security Boundary & Queue Producer Tests
# =============================================================================


def test_08_security_strip_credentials_and_tokens(sample_canonical_item: dict[str, Any]) -> None:
    """Test 08: Producer strictly removes cookies, tokens, and authorization headers from download_input."""
    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task = producer.build_download_intent(
        item=sample_canonical_item,
        sync_run_id="sync_run_sec_test",
        scope_id="douyin:dyacct_test",
    )
    assert task is not None
    assert "cookie" not in task.download_input
    assert "token" not in task.download_input
    assert "sessionid" not in str(task.download_input)
    assert task.download_input.get("is_volatile_hint") is True
    assert task.download_input.get("play_addr_h264") == "https://aweme.snssdk.com/video/tos/sample.mp4"


def test_09_download_input_volatile_hint(sample_canonical_item: dict[str, Any]) -> None:
    """Test 09: download_input retains clean volatile media hints."""
    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task = producer.build_download_intent(
        item=sample_canonical_item,
        sync_run_id="sync_run_01",
        scope_id="douyin:dyacct_test",
    )
    assert task is not None
    assert task.download_input["width"] == 1080
    assert task.download_input["height"] == 1920
    assert task.download_input["is_volatile_hint"] is True


def test_10_queue_producer_batch_build_intents(sample_canonical_item: dict[str, Any]) -> None:
    """Test 10: Batch evaluation filters out ineligible items and builds tasks for eligible ones."""
    item1 = dict(sample_canonical_item)
    item1["platform_content_id"] = "item_1"

    item2 = dict(sample_canonical_item)
    item2["platform_content_id"] = "item_2"
    item2["collection"] = dict(sample_canonical_item["collection"])
    item2["collection"]["active"] = False  # Ineligible

    item3 = dict(sample_canonical_item)
    item3["platform_content_id"] = "item_3"

    provider = InMemoryAssetStateProvider()
    provider.add_asset("douyin", "douyin:dyacct_test", "item_3")  # Ineligible (already has asset)

    producer = DownloadQueueProducer(asset_provider=provider)
    tasks = producer.build_intents_for_items(
        items=[item1, item2, item3],
        sync_run_id="sync_batch_test",
        scope_id="douyin:dyacct_test",
    )

    assert len(tasks) == 1
    assert tasks[0].platform_content_id == "item_1"


# =============================================================================
# 4. Transactional Outbox Pattern & DB Persistence Tests
# =============================================================================


def test_11_outbox_transactional_insert_with_finalize_success(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 11: finalize_success atomically commits staged items, watermark, and download outbox."""
    run_id = "run_tx_success_01"
    scope = "douyin:dyacct_test"
    repo.begin_sync_run(run_id, scope_id=scope)

    # Stage item
    repo.stage_item(run_id, sample_canonical_item, scope_id=scope)

    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task = producer.build_download_intent(sample_canonical_item, run_id, scope)
    assert task is not None

    # Finalize success with download tasks
    rec = repo.finalize_success(
        sync_run_id=run_id,
        candidate_watermark="10001",
        download_tasks=[task],
        scope_id=scope,
    )
    assert rec.is_completed

    # Verify watermark updated
    state = repo.get_sync_state(scope_id=scope)
    assert state is not None
    assert state.incremental_head_watermark_cursor == "10001"

    # Verify item committed
    item = repo.get_collection_item(sample_canonical_item["platform_content_id"], scope_id=scope)
    assert item is not None
    assert item.active is True

    # Verify outbox row committed
    outbox_task = repo.get_outbox_record(task.task_id)
    assert outbox_task is not None
    assert outbox_task.status == "PENDING"
    assert outbox_task.source_sync_run_id == run_id


def test_12_outbox_rollback_on_finalize_failure(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 12: finalize_failure never commits outbox tasks and clears staging."""
    run_id = "run_tx_fail_01"
    scope = "douyin:dyacct_test"
    repo.begin_sync_run(run_id, scope_id=scope)
    repo.stage_item(run_id, sample_canonical_item, scope_id=scope)

    rec = repo.finalize_failure(
        sync_run_id=run_id,
        error_code="TEST_ERROR",
        error_message="Simulated run failure",
        scope_id=scope,
    )
    assert rec.is_failed

    # Zero outbox records
    pending = repo.get_pending_outbox_tasks(scope_id=scope)
    assert len(pending) == 0


def test_13_outbox_atomic_rollback_on_exception(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 13: Exception during finalize_success rolls back outbox, watermark, and items atomically."""
    run_id = "run_tx_abort_01"
    scope = "douyin:dyacct_test"
    repo.begin_sync_run(run_id, scope_id=scope)
    repo.stage_item(run_id, sample_canonical_item, scope_id=scope)

    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task = producer.build_download_intent(sample_canonical_item, run_id, scope)
    assert task is not None

    # Simulate failure by expecting optimistic concurrency failure
    with pytest.raises(Exception):
        repo.finalize_success(
            sync_run_id=run_id,
            candidate_watermark="10001",
            expected_previous_watermark="WRONG_WATERMARK",  # Triggers rollback!
            download_tasks=[task],
            scope_id=scope,
        )

    # Outbox should have 0 records
    assert repo.get_outbox_record(task.task_id) is None
    # Item should not be committed
    assert repo.get_collection_item(sample_canonical_item["platform_content_id"], scope_id=scope) is None


def test_14_outbox_dedup_idempotency_on_replay(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 14: Replaying identical task ID updates existing PENDING task without creating duplicates."""
    run_id_1 = "run_replay_01"
    run_id_2 = "run_replay_02"
    scope = "douyin:dyacct_test"

    repo.begin_sync_run(run_id_1, scope_id=scope)
    repo.stage_item(run_id_1, sample_canonical_item, scope_id=scope)

    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task1 = producer.build_download_intent(sample_canonical_item, run_id_1, scope)
    assert task1 is not None
    repo.finalize_success(run_id_1, candidate_watermark="10001", download_tasks=[task1], scope_id=scope)

    # Run 2 with same item
    repo.begin_sync_run(run_id_2, scope_id=scope)
    repo.stage_item(run_id_2, sample_canonical_item, scope_id=scope)
    task2 = producer.build_download_intent(sample_canonical_item, run_id_2, scope)
    assert task2 is not None
    assert task2.task_id == task1.task_id  # Exact same deterministic task ID!

    repo.finalize_success(run_id_2, candidate_watermark="10002", download_tasks=[task2], scope_id=scope)

    # Check that download_outbox has exactly 1 row, updated with source_sync_run_id = run_id_2
    conn = repo._get_connection()
    count = conn.execute("SELECT COUNT(*) FROM download_outbox WHERE task_id = ?", (task1.task_id,)).fetchone()[0]
    assert count == 1

    rec = repo.get_outbox_record(task1.task_id)
    assert rec is not None
    # Fix B: source_sync_run_id (first_source_sync_run_id) is preserved from original creation
    assert rec.source_sync_run_id == run_id_1
    assert rec.first_source_sync_run_id == run_id_1
    # last_seen_sync_run_id is updated to run_id_2
    assert rec.last_seen_sync_run_id == run_id_2


def test_14b_replaying_dispatched_task_does_not_reset_to_pending(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 14b: If an outbox task is already DISPATCHED, re-running collector does NOT reset it to PENDING."""
    run_id_1 = "run_disp_01"
    run_id_2 = "run_disp_02"
    scope = "douyin:dyacct_test"

    repo.begin_sync_run(run_id_1, scope_id=scope)
    repo.stage_item(run_id_1, sample_canonical_item, scope_id=scope)

    producer = DownloadQueueProducer(asset_provider=NullAssetStateProvider())
    task1 = producer.build_download_intent(sample_canonical_item, run_id_1, scope)
    assert task1 is not None
    repo.finalize_success(run_id_1, candidate_watermark="10001", download_tasks=[task1], scope_id=scope)

    # Dispatcher marks task as DISPATCHED
    from src.collector.download_queue import DownloadOutboxDispatcher
    dispatcher = DownloadOutboxDispatcher(repo)
    polled = dispatcher.poll_pending(scope_id=scope)
    assert len(polled) == 1
    assert polled[0].task_id == task1.task_id
    dispatcher.mark_dispatched([task1.task_id])

    rec_before = repo.get_outbox_record(task1.task_id)
    assert rec_before is not None
    assert rec_before.status == "DISPATCHED"

    # Now Run 2 arrives with the same item
    repo.begin_sync_run(run_id_2, scope_id=scope)
    repo.stage_item(run_id_2, sample_canonical_item, scope_id=scope)
    task2 = producer.build_download_intent(sample_canonical_item, run_id_2, scope)
    assert task2 is not None
    assert task2.task_id == task1.task_id

    repo.finalize_success(run_id_2, candidate_watermark="10002", download_tasks=[task2], scope_id=scope)

    # Status must STILL be DISPATCHED, not reset to PENDING!
    rec_after = repo.get_outbox_record(task1.task_id)
    assert rec_after is not None
    assert rec_after.status == "DISPATCHED"
    assert rec_after.source_sync_run_id == run_id_1
    assert rec_after.first_source_sync_run_id == run_id_1



# =============================================================================
# 5. Outbox Dispatcher Lifecycle & Failure Handling Tests
# =============================================================================


def test_15_outbox_dispatcher_poll_pending(repo: SqliteMetadataRepository) -> None:
    """Test 15: poll_pending returns available pending tasks."""
    task = DownloadTask(
        task_id="dl_test_poll_01",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="test_poll_01",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task])

    dispatcher = DownloadOutboxDispatcher(repo)
    tasks = dispatcher.poll_pending(scope_id="douyin:dyacct_test")
    assert len(tasks) == 1
    assert tasks[0].task_id == "dl_test_poll_01"


def test_16_outbox_dispatcher_mark_dispatched(repo: SqliteMetadataRepository) -> None:
    """Test 16: mark_dispatched transitions tasks to DISPATCHED state."""
    task = DownloadTask(
        task_id="dl_test_disp_01",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="test_disp_01",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task])
    dispatcher = DownloadOutboxDispatcher(repo)

    updated = dispatcher.mark_dispatched(["dl_test_disp_01"])
    assert updated == 1

    rec = repo.get_outbox_record("dl_test_disp_01")
    assert rec is not None
    assert rec.status == "DISPATCHED"
    assert rec.dispatched_at is not None

    # No longer returned by poll_pending
    assert len(dispatcher.poll_pending(scope_id="douyin:dyacct_test")) == 0


def test_17_outbox_dispatcher_mark_delivery_failed_retry(repo: SqliteMetadataRepository) -> None:
    """Test 17: mark_delivery_failed increments attempt_count and schedules exponential retry."""
    task = DownloadTask(
        task_id="dl_test_retry_01",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="test_retry_01",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task])
    dispatcher = DownloadOutboxDispatcher(repo)

    # First failure
    dispatcher.mark_delivery_failed("dl_test_retry_01", "Connection refused to queue")

    rec = repo.get_outbox_record("dl_test_retry_01")
    assert rec is not None
    assert rec.attempt_count == 1
    assert rec.status == "PENDING"  # Remains PENDING for retry
    assert "Connection refused" in (rec.last_error or "")
    assert rec.available_at > rec.created_at  # Delayed in future


def test_18_outbox_dispatcher_mark_delivery_failed_terminal(repo: SqliteMetadataRepository) -> None:
    """Test 18: Consecutive failures exceeding max_attempts transition task to FAILED."""
    task = DownloadTask(
        task_id="dl_test_term_01",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="test_term_01",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task])
    dispatcher = DownloadOutboxDispatcher(repo)

    dispatcher.mark_delivery_failed("dl_test_term_01", "Err 1")
    dispatcher.mark_delivery_failed("dl_test_term_01", "Err 2")
    dispatcher.mark_delivery_failed("dl_test_term_01", "Err 3")

    rec = repo.get_outbox_record("dl_test_term_01")
    assert rec is not None
    assert rec.attempt_count == 3
    assert rec.status == "FAILED"  # Terminal failed!


def test_19_error_sanitization_in_outbox(repo: SqliteMetadataRepository) -> None:
    """Test 19: Error messages containing tokens or cookies are sanitized."""
    task = DownloadTask(
        task_id="dl_test_san_01",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="test_san_01",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task])
    dispatcher = DownloadOutboxDispatcher(repo)

    dispatcher.mark_delivery_failed("dl_test_san_01", "HTTP 401 Unauthorized with sessionid=secret12345")
    rec = repo.get_outbox_record("dl_test_san_01")
    assert rec is not None
    assert "secret12345" not in (rec.last_error or "")
    assert "Sanitized" in (rec.last_error or "")


# =============================================================================
# 6. Scope Isolation & Protocol Tests
# =============================================================================


def test_20_scope_isolation_in_outbox(repo: SqliteMetadataRepository) -> None:
    """Test 20: Tasks from Account A and Account B are partitioned and isolated by scope_id."""
    task_a = DownloadTask(
        task_id="dl_acct_a_01",
        platform="douyin",
        scope_id="douyin:dyacct_AAA",
        platform_content_id="content_shared",
        content_type="video",
        source_url="https://example.com/1",
    )
    task_b = DownloadTask(
        task_id="dl_acct_b_01",
        platform="douyin",
        scope_id="douyin:dyacct_BBB",
        platform_content_id="content_shared",
        content_type="video",
        source_url="https://example.com/1",
    )
    repo.persist_outbox_tasks([task_a, task_b])

    dispatcher = DownloadOutboxDispatcher(repo)
    tasks_a = dispatcher.poll_pending(scope_id="douyin:dyacct_AAA")
    tasks_b = dispatcher.poll_pending(scope_id="douyin:dyacct_BBB")

    assert len(tasks_a) == 1
    assert tasks_a[0].scope_id == "douyin:dyacct_AAA"

    assert len(tasks_b) == 1
    assert tasks_b[0].scope_id == "douyin:dyacct_BBB"


def test_21_asset_state_provider_protocol_compliance() -> None:
    """Test 21: AssetStateProvider implementations conform to Protocol."""
    null_provider = NullAssetStateProvider()
    assert isinstance(null_provider, AssetStateProvider)
    assert null_provider.has_valid_asset("douyin", "scope_1", "cid_1", "video") is False

    mem_provider = InMemoryAssetStateProvider()
    assert isinstance(mem_provider, AssetStateProvider)
    mem_provider.add_asset("douyin", "scope_1", "cid_1")
    assert mem_provider.has_valid_asset("douyin", "scope_1", "cid_1", "video") is True
    assert mem_provider.has_valid_asset("douyin", "scope_1", "cid_2", "video") is False


# =============================================================================
# 7. Helper & Compatibility Tests
# =============================================================================


def test_22_get_committed_items_for_run(
    repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]
) -> None:
    """Test 22: get_committed_items_for_run retrieves committed items linked to a sync run."""
    run_id = "run_items_comm_01"
    scope = "douyin:dyacct_test"
    repo.begin_sync_run(run_id, scope_id=scope)
    repo.stage_item(run_id, sample_canonical_item, scope_id=scope)
    repo.append_observation(
        {
            "observation_id": "obs_comm_01",
            "sync_run_id": run_id,
            "platform_content_id": sample_canonical_item["platform_content_id"],
            "page_number": 1,
            "position_in_page": 1,
        },
        scope_id=scope,
    )
    repo.finalize_success(run_id, candidate_watermark="10001", scope_id=scope)

    items = repo.get_committed_items_for_run(run_id, scope_id=scope)
    assert len(items) == 1
    assert items[0].platform_content_id == sample_canonical_item["platform_content_id"]


def test_23_get_staged_items(repo: SqliteMetadataRepository, sample_canonical_item: dict[str, Any]) -> None:
    """Test 23: get_staged_items queries active staging table before finalize."""
    run_id = "run_staged_query_01"
    scope = "douyin:dyacct_test"
    repo.begin_sync_run(run_id, scope_id=scope)
    repo.stage_item(run_id, sample_canonical_item, scope_id=scope)

    staged = repo.get_staged_items(run_id)
    assert len(staged) == 1
    assert staged[0].platform_content_id == sample_canonical_item["platform_content_id"]


def test_24_download_task_backward_compatibility_property() -> None:
    """Test 24: enqueued_at property returns created_at for backward compatibility."""
    t = DownloadTask(
        task_id="dl_compat_01",
        platform="douyin",
        scope_id="douyin:default",
        platform_content_id="compat_01",
        content_type="video",
        source_url="https://example.com",
        created_at="2026-09-05T12:00:00Z",
    )
    assert t.enqueued_at == "2026-09-05T12:00:00Z"


def test_25_queue_producer_c01_enqueue_protocol(repo: SqliteMetadataRepository) -> None:
    """Test 25: producer.enqueue(task) persists to outbox conforming to C01 protocol."""
    producer = DownloadQueueProducer(repository=repo)
    task = DownloadTask(
        task_id="dl_proto_01",
        platform="douyin",
        scope_id="douyin:default",
        platform_content_id="proto_01",
        content_type="video",
        source_url="https://example.com",
    )
    ok = producer.enqueue(task)
    assert ok is True
    rec = repo.get_outbox_record("dl_proto_01")
    assert rec is not None
    assert rec.status == "PENDING"


def test_26_outbox_record_to_task_roundtrip() -> None:
    """Test 26: OutboxRecord.to_task accurately reconstructs DownloadTask."""
    orig_task = DownloadTask(
        task_id="dl_roundtrip_01",
        platform="douyin",
        scope_id="douyin:dyacct_rt",
        platform_content_id="rt_01",
        content_type="video",
        source_url="https://example.com",
        download_input={"play_addr": "https://video.mp4"},
        priority=DownloadPriority.REPAIR.value,
        reason=DownloadReason.ASSET_MISSING_REPAIR.value,
    )
    record = OutboxRecord(
        outbox_id="ob_dl_roundtrip_01",
        task_id=orig_task.task_id,
        scope_id=orig_task.scope_id,
        platform=orig_task.platform,
        platform_content_id=orig_task.platform_content_id,
        content_type=orig_task.content_type,
        payload_json=orig_task.to_deterministic_json(),
        status="PENDING",
        created_at=orig_task.created_at,
        available_at=orig_task.created_at,
        source_sync_run_id="run_rt_01",
    )
    reconstructed = record.to_task()
    assert reconstructed.task_id == orig_task.task_id
    assert reconstructed.priority == 5
    assert reconstructed.reason == "ASSET_MISSING_REPAIR"
    assert reconstructed.download_input == orig_task.download_input


def test_27_outbox_available_at_future_filtering(repo: SqliteMetadataRepository) -> None:
    """Test 27: Tasks with future available_at timestamp are excluded from poll_pending."""
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = repo._get_connection()
    conn.execute(
        """
        INSERT INTO download_outbox (
            outbox_id, task_id, scope_id, platform, platform_content_id,
            content_type, payload_json, status, created_at, available_at, source_sync_run_id
        ) VALUES ('ob_future_01', 'dl_future_01', 'douyin:default', 'douyin', 'fut_01', 'video', '{}', 'PENDING', ?, ?, 'run_fut')
        """,
        (utcnow_iso(), future_time),
    )
    conn.commit()

    dispatcher = DownloadOutboxDispatcher(repo)
    pending = dispatcher.poll_pending()
    assert all(t.task_id != "dl_future_01" for t in pending)


def test_28_outbox_persist_outside_finalize_api(repo: SqliteMetadataRepository) -> None:
    """Test 28: persist_outbox_tasks persists standalone tasks with correct status."""
    task = DownloadTask(
        task_id="dl_standalone_01",
        platform="douyin",
        scope_id="douyin:default",
        platform_content_id="std_01",
        content_type="video",
        source_url="https://example.com",
    )
    count = repo.persist_outbox_tasks([task], sync_run_id="manual_repair_01")
    assert count == 1
    rec = repo.get_outbox_record("dl_standalone_01")
    assert rec is not None
    assert rec.source_sync_run_id == "manual_repair_01"
    assert rec.status == "PENDING"


def test_29_eligibility_decision_details(sample_canonical_item: dict[str, Any]) -> None:
    """Test 29: Eligibility decision provides diagnostic details dict."""
    policy = DownloadEligibilityPolicy()
    decision = policy.evaluate(
        item=sample_canonical_item,
        scope_id="douyin:default",
        prior=PriorCollectionState(exists=False, active=False),
    )
    assert "status" in decision.details
    assert decision.details["content_id"] == sample_canonical_item["platform_content_id"]


def test_30_sync_engine_wired_queue_producer_generates_outbox() -> None:
    """Test 30: SyncEngine generates outbox records through wired queue_producer during sync."""
    from unittest.mock import MagicMock
    from src.collector.douyin.config import DouyinCollectorConfig
    from src.collector.douyin.source_client import CollectionPage
    from src.collector.douyin.sync_engine import DouyinSyncEngine
    from src.collector.douyin.transform import DouyinCanonicalTransformer
    from src.collector.raw_archive import DiskRawArchiver

    # Setup isolated test environment
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "metadata.db"
        raw_path = tmp_path / "raw"

        repo = SqliteMetadataRepository(db_path)
        archiver = DiskRawArchiver(raw_path)
        transformer = DouyinCanonicalTransformer()
        config = DouyinCollectorConfig()

        # Mock source client with 2 items
        mock_source = MagicMock()
        mock_source.fetch_collection_page.return_value = CollectionPage(
            request_cursor="0",
            response_cursor="20001",
            has_more=False,
            items=[
                {
                    "aweme_id": "88880001",
                    "desc": "Test Item 1",
                    "create_time": 1700000000,
                    "author": {"uid": "u1", "sec_uid": "sec1", "nickname": "nick1"},
                    "video": {"play_addr": {"url_list": ["https://video1.mp4"]}},
                },
                {
                    "aweme_id": "88880002",
                    "desc": "Test Item 2",
                    "create_time": 1700000010,
                    "author": {"uid": "u2", "sec_uid": "sec2", "nickname": "nick2"},
                    "video": {"play_addr": {"url_list": ["https://video2.mp4"]}},
                },
            ],
            raw_response={"status_code": 0, "aweme_list": [], "cursor": 20001, "has_more": 0},
            http_status=200,
            platform_status=0,
            fetched_at="2026-09-05T12:00:00Z",
            latency_ms=100.0,
        )

        queue_producer = DownloadQueueProducer(repository=repo, asset_provider=NullAssetStateProvider())

        engine = DouyinSyncEngine(
            config=config,
            repository=repo,
            archiver=archiver,
            transformer=transformer,
            source_client=mock_source,
            queue_producer=queue_producer,
        )

        run_rec = engine.sync(sync_run_id="run_wire_test_01", scope_id="douyin:dyacct_wire")
        assert run_rec.is_completed

        # Verify outbox records were atomically persisted into download_outbox
        dispatcher = DownloadOutboxDispatcher(repo)
        pending = dispatcher.poll_pending(scope_id="douyin:dyacct_wire")
        assert len(pending) == 2
        content_ids = {t.platform_content_id for t in pending}
        assert content_ids == {"88880001", "88880002"}
        assert all(t.status == "PENDING" for t in pending)
        assert all(t.collection_sync_run_id == "run_wire_test_01" for t in pending)
        repo.close()
