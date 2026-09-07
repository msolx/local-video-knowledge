"""Comprehensive test suite for the Collector Metadata Repository (DY-C08).

Covers:
1. Initialize and idempotent migrations
2. Reopening existing database
3. WAL journal mode and foreign key constraints
4. Begin sync run lifecycle
5. Invalid run lifecycle transitions
6. Prior state for missing item
7. Prior state for existing item
8. Run-scoped item staging isolation
9. Observation append-only persistence
10. Observation idempotent replay
11. Observation conflict rejection on differing payload
12. Atomic finalize_success promotion
13. Watermark commit on success
14. Failed run does not advance watermark
15. Failed run does not mutate existing items
16. Empty/failed run does not mark items inactive
17. Explicit inactive mutation on successful run
18. Reappearance state persistence
19. Reappearance event does not propagate across subsequent runs
20. Optimistic concurrency conflict rejection
21. Transaction rollback on finalize failure
22. Crash recovery for orphaned RUNNING runs
23. RawRef provenance persistence
24. Exact decimal TEXT precision for cursors
25. Full canonical JSON round-trip validation
26. Idempotent retries for begin and finalize
27. Credential sanitization in error messages
28. Multi-scope account isolation
29. Verification of required indexes
30. C06 -> C07 -> C08 offline pipeline integration
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.collector.douyin.transform import (
    DouyinCanonicalTransformer,
    PriorCollectionState,
    RawRefContext,
    TransformContext,
)
from src.collector.raw_archive import DiskRawArchiver
from src.collector.repository import (
    DEFAULT_PLATFORM,
    DEFAULT_SCOPE_ID,
    SqliteMetadataRepository,
)
from src.collector.repository_errors import (
    RepositoryConflictError,
    RepositoryIOError,
    RepositoryNotFoundError,
    RepositoryStateConflictError,
)
from src.collector.repository_models import SyncRunStatus


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "metadata_test.db"


@pytest.fixture
def repo(temp_db_path: Path) -> SqliteMetadataRepository:
    repository = SqliteMetadataRepository(temp_db_path, auto_init=True)
    yield repository
    repository.close()


def make_sample_item(
    content_id: str = "7671141177986518318",
    active: bool = True,
    observed_count: int = 1,
    reappeared_at: str | None = None,
    first_seen_at: str = "2026-09-05T07:00:00Z",
    last_seen_at: str = "2026-09-05T07:00:00Z",
) -> dict[str, Any]:
    """Helper creating a valid canonical collection-item-v1 dictionary."""
    return {
        "schema_version": "collection-item-v1",
        "platform": "douyin",
        "source_type": "collection",
        "platform_content_id": content_id,
        "source_url": f"https://www.douyin.com/video/{content_id}",
        "content_type": "video",
        "title": "Sample Video Title",
        "description": "Sample description text",
        "tags": ["travel", "photography"],
        "author": {
            "platform_user_id": "9988776655",
            "unique_id": "test_creator",
            "display_name": "Test Creator",
            "avatar_url": "https://p3.douyinpic.com/avatar.jpg",
            "profile_url": "https://www.douyin.com/user/9988776655",
            "signature": "Creator bio",
        },
        "published_at": "2026-09-04T12:00:00Z",
        "collection": {
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "reappeared_at": reappeared_at,
            "active": active,
            "observed_count": observed_count,
            "last_seen_position": 1,
        },
        "media": {
            "duration_seconds": 15.5,
            "duration_ms": 15500,
            "width": 1080,
            "height": 1920,
            "aspect_ratio": "9:16",
            "cover_url": "https://p3.douyinpic.com/cover.jpg",
            "cover_asset_id": "tos-cn-v-0000/cover123",
            "video_asset_id": "tos-cn-v-0000/video123",
            "images": None,
            "has_audio": True,
            "audio_title": "Original Sound",
            "audio_author": "Test Creator",
        },
    }


def make_sample_observation(
    sync_run_id: str,
    content_id: str = "7671141177986518318",
    obs_id: str | None = None,
    rank: int = 1,
    page_number: int = 1,
    position: int = 0,
    is_first: bool = True,
    is_reappearance: bool = False,
    req_cursor: str = "0",
    resp_cursor: str = "1788536236926781",
) -> dict[str, Any]:
    """Helper creating a valid canonical collection-observation-v1 dictionary."""
    oid = obs_id or f"obs_{sync_run_id}_{content_id}"
    return {
        "schema_version": "collection-observation-v1",
        "observation_id": oid,
        "sync_run_id": sync_run_id,
        "platform": "douyin",
        "platform_content_id": content_id,
        "observed_at": "2026-09-05T07:00:00Z",
        "page_number": page_number,
        "position_in_page": position,
        "global_rank_seen": rank,
        "page_request_cursor": req_cursor,
        "page_response_cursor": resp_cursor,
        "is_first_observation": is_first,
        "is_reappearance": is_reappearance,
        "raw_ref": {
            "platform_raw_type": "douyin.listcollection.aweme_struct",
            "archive_file": "data/raw/douyin/run_01/page_000001.json",
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "item_index": position,
        },
    }


# =============================================================================
# 1. Initialize and Migration Idempotency
# =============================================================================


def test_01_initialize_and_migration_idempotency(repo: SqliteMetadataRepository, temp_db_path: Path) -> None:
    assert repo.is_initialized
    conn = repo._get_connection()

    # Check migration records
    mig1 = conn.execute("SELECT version, description FROM schema_migrations WHERE version = 1;").fetchone()
    assert mig1 is not None
    assert mig1["version"] == 1

    mig2 = conn.execute("SELECT version, description FROM schema_migrations WHERE version = 2;").fetchone()
    assert mig2 is not None
    assert mig2["version"] == 2

    mig3 = conn.execute("SELECT version, description FROM schema_migrations WHERE version = 3;").fetchone()
    assert mig3 is not None
    assert mig3["version"] == 3

    # Check user_version PRAGMA
    user_ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert user_ver == 3

    # Re-calling initialize() should be safely idempotent
    repo.initialize()
    user_ver_after = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert user_ver_after == 3


# =============================================================================
# 2. Reopen Existing Database
# =============================================================================


def test_02_reopen_existing_db(temp_db_path: Path) -> None:
    # First instance creates run and closes
    repo1 = SqliteMetadataRepository(temp_db_path)
    repo1.begin_sync_run("run_persist_01", mode="incremental")
    repo1.close()

    # Second instance opens existing database
    repo2 = SqliteMetadataRepository(temp_db_path)
    run_rec = repo2.get_sync_run("run_persist_01")
    assert run_rec is not None
    assert run_rec.sync_run_id == "run_persist_01"
    repo2.close()


# =============================================================================
# 3. WAL and Foreign Key Enforcement
# =============================================================================


def test_03_wal_and_foreign_keys_enabled(repo: SqliteMetadataRepository) -> None:
    conn = repo._get_connection()
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal_mode.lower() == "wal"

    fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1

    # Foreign key enforcement: observation referencing non-existent sync_run must fail
    orphan_obs = make_sample_observation("non_existent_run_999", content_id="item_orphan")
    with pytest.raises(RepositoryNotFoundError):
        repo.append_observation(orphan_obs)


# =============================================================================
# 4. Begin Run Lifecycle
# =============================================================================


def test_04_begin_run_lifecycle(repo: SqliteMetadataRepository) -> None:
    run = repo.begin_sync_run("run_life_01", mode="incremental", page_size=20)
    assert run.sync_run_id == "run_life_01"
    assert run.is_running
    assert run.status == SyncRunStatus.RUNNING.value
    assert run.finished_at is None
    assert run.execution_context.get("page_size") == 20


# =============================================================================
# 5. Invalid Run Lifecycle Transitions
# =============================================================================


def test_05_invalid_run_transitions(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_trans_01")
    repo.finalize_success("run_trans_01", candidate_watermark="1000")

    # Cannot restart a completed run
    with pytest.raises(RepositoryConflictError):
        repo.begin_sync_run("run_trans_01")

    # Cannot mark completed run as failed
    with pytest.raises(RepositoryStateConflictError):
        repo.finalize_failure("run_trans_01", error_code="NET_ERR", error_message="Failed")


# =============================================================================
# 6. Prior Missing State
# =============================================================================


def test_06_prior_missing(repo: SqliteMetadataRepository) -> None:
    prior = repo.get_prior_state("never_seen_123")
    assert not prior.exists
    assert not prior.active
    assert prior.observed_count == 0
    assert prior.first_seen_at is None


# =============================================================================
# 7. Prior Existing State
# =============================================================================


def test_07_prior_existing(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_prior_01")
    item = make_sample_item("exist_123", active=True, observed_count=1)
    repo.stage_item("run_prior_01", item)
    repo.finalize_success("run_prior_01", candidate_watermark="2000")

    prior = repo.get_prior_state("exist_123")
    assert prior.exists
    assert prior.active
    assert prior.observed_count == 1
    assert prior.first_seen_at == "2026-09-05T07:00:00Z"


# =============================================================================
# 8. Run-Scoped Item Staging Isolation
# =============================================================================


def test_08_stage_first_item_isolation(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_stage_01")
    item = make_sample_item("staged_only_999")
    repo.stage_item("run_stage_01", item)

    # Item is present in staging
    staged = repo.get_staged_items_for_run("run_stage_01")
    assert len(staged) == 1
    assert staged[0]["platform_content_id"] == "staged_only_999"

    # Crucial: Item must NOT exist in production collection_items yet!
    assert not repo.item_exists("douyin", "staged_only_999")
    assert repo.get_item("staged_only_999") is None


# =============================================================================
# 9. Observation Append
# =============================================================================


def test_09_observation_append(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_obs_01")
    obs = make_sample_observation("run_obs_01", content_id="cid_obs_1", rank=1)
    repo.append_observation(obs)

    observations = repo.get_observations_for_run("run_obs_01")
    assert len(observations) == 1
    assert observations[0]["observation_id"] == "obs_run_obs_01_cid_obs_1"
    assert observations[0]["global_rank_seen"] == 1


# =============================================================================
# 10. Observation Idempotency
# =============================================================================


def test_10_observation_idempotency(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_obs_idem")
    obs = make_sample_observation("run_obs_idem", content_id="cid_idem")

    # Append twice with identical payload
    repo.append_observation(obs)
    repo.append_observation(obs)

    observations = repo.get_observations_for_run("run_obs_idem")
    assert len(observations) == 1


# =============================================================================
# 11. Observation Conflict Rejection
# =============================================================================


def test_11_observation_conflict_rejected(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_obs_conf")
    obs_a = make_sample_observation("run_obs_conf", content_id="cid_conf", rank=1)
    obs_b = make_sample_observation("run_obs_conf", content_id="cid_conf", rank=2)  # different rank!

    repo.append_observation(obs_a)
    with pytest.raises(RepositoryConflictError):
        repo.append_observation(obs_b)


# =============================================================================
# 12. Finalize Success Atomic Promotion
# =============================================================================


def test_12_finalize_success_atomic_promotion(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_fin_succ")
    item1 = make_sample_item("item_fin_1")
    item2 = make_sample_item("item_fin_2")
    repo.stage_item("run_fin_succ", item1)
    repo.stage_item("run_fin_succ", item2)
    repo.append_observation(make_sample_observation("run_fin_succ", "item_fin_1", rank=1))
    repo.append_observation(make_sample_observation("run_fin_succ", "item_fin_2", rank=2))

    metrics = {
        "pages_fetched": 1,
        "items_seen": 2,
        "new_items": 2,
        "reappeared_items": 0,
        "known_items": 0,
        "errors_count": 0,
    }
    completed_run = repo.finalize_success(
        sync_run_id="run_fin_succ",
        candidate_watermark="1788536236926781",
        stop_reason="watermark_reached",
        metrics=metrics,
    )

    assert completed_run.is_completed
    assert repo.item_exists("douyin", "item_fin_1")
    assert repo.item_exists("douyin", "item_fin_2")
    # Staging must be completely cleared
    assert len(repo.get_staged_items_for_run("run_fin_succ")) == 0


# =============================================================================
# 13. Watermark Commit on Success
# =============================================================================


def test_13_watermark_commit_on_success(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_wm_01")
    repo.finalize_success("run_wm_01", candidate_watermark="1788999999")

    state = repo.get_sync_state()
    assert state is not None
    assert state.committed_watermark_cursor == "1788999999"
    assert state.last_successful_sync_run_id == "run_wm_01"


# =============================================================================
# 14. Failed Run Does NOT Advance Watermark
# =============================================================================


def test_14_failed_run_no_watermark_advance(repo: SqliteMetadataRepository) -> None:
    # Established initial committed state
    repo.begin_sync_run("run_initial")
    repo.finalize_success("run_initial", candidate_watermark="1788111111")

    # Start second run that encounters failure
    repo.begin_sync_run("run_failing")
    repo.stage_item("run_failing", make_sample_item("cand_item"))
    repo.finalize_failure(
        sync_run_id="run_failing",
        error_code="RATE_LIMIT",
        error_message="Too many requests",
    )

    # Watermark must remain exactly at initial value
    state = repo.get_sync_state()
    assert state is not None
    assert state.committed_watermark_cursor == "1788111111"
    assert state.last_successful_sync_run_id == "run_initial"


# =============================================================================
# 15. Failed Run Does NOT Mutate Existing Items
# =============================================================================


def test_15_failed_run_no_item_mutation(repo: SqliteMetadataRepository) -> None:
    # Setup initial active item
    repo.begin_sync_run("run_base")
    orig_item = make_sample_item("item_immutable", active=True, observed_count=1)
    repo.stage_item("run_base", orig_item)
    repo.finalize_success("run_base", candidate_watermark="100")

    # Second run stages a mutation to item_immutable, then fails
    repo.begin_sync_run("run_failed_mut")
    mutated_item = make_sample_item("item_immutable", active=False, observed_count=99)
    repo.stage_item("run_failed_mut", mutated_item)
    repo.finalize_failure("run_failed_mut", error_code="CDP_DISCONNECTED", error_message="Crash")

    # In collection_items, original item remains intact
    loaded = repo.get_item("item_immutable")
    assert loaded is not None
    assert loaded["collection"]["active"] is True
    assert loaded["collection"]["observed_count"] == 1


# =============================================================================
# 16. Empty / Failed Run Safety (No Inactive Mutation)
# =============================================================================


def test_16_empty_failed_safety(repo: SqliteMetadataRepository) -> None:
    # Setup 3 active items
    repo.begin_sync_run("run_pop")
    for i in range(3):
        repo.stage_item("run_pop", make_sample_item(f"pop_item_{i}"))
    repo.finalize_success("run_pop", candidate_watermark="300")
    assert repo.count_items(active_only=True) == 3

    # Empty failed run with 0 observations
    repo.begin_sync_run("run_empty_fail")
    repo.finalize_failure("run_empty_fail", error_code="TIMEOUT", error_message="Network timeout")

    # Absolutely all 3 items must still be active
    assert repo.count_items(active_only=True) == 3


# =============================================================================
# 17. Explicit Inactive Mutation on Verified Run
# =============================================================================


def test_17_explicit_inactive_mutation(repo: SqliteMetadataRepository) -> None:
    # Setup items A and B
    repo.begin_sync_run("run_setup")
    repo.stage_item("run_setup", make_sample_item("item_A"))
    repo.stage_item("run_setup", make_sample_item("item_B"))
    repo.finalize_success("run_setup", candidate_watermark="400")

    # C05 detects verified uncollection of item_A
    repo.begin_sync_run("run_inact")
    repo.finalize_success(
        sync_run_id="run_inact",
        candidate_watermark="500",
        items_to_mark_inactive=["item_A"],
    )

    item_a = repo.get_item("item_A")
    item_b = repo.get_item("item_B")
    assert item_a is not None and item_a["collection"]["active"] is False
    assert item_b is not None and item_b["collection"]["active"] is True


# =============================================================================
# 18. Reappearance Persistence
# =============================================================================


def test_18_reappearance_persistence(repo: SqliteMetadataRepository) -> None:
    # Run 1: initial ingestion
    repo.begin_sync_run("run_r1")
    repo.stage_item("run_r1", make_sample_item("reapp_item", active=True, observed_count=1))
    repo.finalize_success("run_r1", candidate_watermark="1000")

    # Mark inactive
    repo.begin_sync_run("run_r2")
    repo.finalize_success("run_r2", candidate_watermark="1000", items_to_mark_inactive=["reapp_item"])
    prior_r3 = repo.get_prior_state("reapp_item")
    assert prior_r3.exists and not prior_r3.active

    # Run 3: Item reappears at collection head
    repo.begin_sync_run("run_r3")
    reappeared_item = make_sample_item(
        "reapp_item",
        active=True,
        observed_count=2,
        reappeared_at="2026-09-05T08:00:00Z",
    )
    repo.stage_item("run_r3", reappeared_item, is_reappearance=True)
    repo.finalize_success("run_r3", candidate_watermark="2000")

    # Verify persistent state
    loaded = repo.get_item("reapp_item")
    assert loaded is not None
    assert loaded["collection"]["active"] is True
    assert loaded["collection"]["reappeared_at"] == "2026-09-05T08:00:00Z"
    assert loaded["collection"]["observed_count"] == 2


# =============================================================================
# 19. No Reappearance Propagation Across Runs
# =============================================================================


def test_19_no_reappearance_propagation_across_runs(repo: SqliteMetadataRepository) -> None:
    # Start from active reappeared item from test 18 setup
    repo.begin_sync_run("run_stable_1")
    repo.stage_item(
        "run_stable_1",
        make_sample_item("stable_item", active=True, observed_count=2, reappeared_at="2026-09-05T08:00:00Z"),
    )
    repo.finalize_success("run_stable_1", candidate_watermark="2000")

    # Run 2: Item is seen again, still active -> is_reappearance MUST be False
    prior = repo.get_prior_state("stable_item")
    assert prior.exists and prior.active

    repo.begin_sync_run("run_stable_2")
    next_item = make_sample_item(
        "stable_item",
        active=True,
        observed_count=3,
        reappeared_at=prior.reappeared_at,  # preserved timestamp
    )
    # Staged with is_reappearance=False
    repo.stage_item("run_stable_2", next_item, is_reappearance=False)
    obs = make_sample_observation("run_stable_2", "stable_item", is_reappearance=False)
    repo.append_observation(obs)
    repo.finalize_success("run_stable_2", candidate_watermark="3000")

    loaded_obs = repo.get_observations_for_run("run_stable_2")[0]
    assert loaded_obs["is_reappearance"] is False


# =============================================================================
# 20. Optimistic Concurrency State Conflict
# =============================================================================


def test_20_optimistic_state_conflict(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_worker_1")
    repo.finalize_success("run_worker_1", candidate_watermark="W_ALPHA")

    repo.begin_sync_run("run_worker_2")
    # Worker 2 expects watermark to be W_OLD, but DB has W_ALPHA
    with pytest.raises(RepositoryStateConflictError) as exc_info:
        repo.finalize_success(
            "run_worker_2",
            candidate_watermark="W_BETA",
            expected_previous_watermark="W_OLD",
        )
    assert "Optimistic state conflict" in str(exc_info.value)


# =============================================================================
# 21. Transaction Rollback on Finalize Error
# =============================================================================


def test_21_transaction_rollback_on_error(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_corrupt")
    # Insert invalid JSON directly into staging to force an internal parse error
    conn = repo._get_connection()
    conn.execute(
        """
        INSERT INTO run_item_staging (
            sync_run_id, scope_id, platform, platform_content_id,
            content_type, canonical_item_json, is_reappearance, staged_at
        ) VALUES ('run_corrupt', 'douyin:default', 'douyin', 'corrupt_id', 'video', 'INVALID_JSON_BLOB', 0, '2026-09-05T00:00:00Z')
        """
    )

    with pytest.raises(Exception):
        repo.finalize_success("run_corrupt", candidate_watermark="9999")

    # Run must still be RUNNING because transaction rolled back
    run_rec = repo.get_sync_run("run_corrupt")
    assert run_rec is not None and run_rec.is_running


# =============================================================================
# 22. Crash Recovery for Orphaned RUNNING Runs
# =============================================================================


def test_22_crash_running_recovery(repo: SqliteMetadataRepository) -> None:
    # Simulate a crashed process that left run_crash in RUNNING status with staging
    repo.begin_sync_run("run_crash")
    repo.stage_item("run_crash", make_sample_item("crash_staged_item"))

    # Attempting recovery without holding service lock must fail
    with pytest.raises(RepositoryStateConflictError, match="without holding the Collector ServiceLock"):
        repo.recover_stale_runs(service_lock_acquired=False)

    # Crash recovery routine runs on restart when ServiceLock is held
    aborted = repo.recover_stale_runs(service_lock_acquired=True)
    assert "run_crash" in aborted

    recovered_run = repo.get_sync_run("run_crash")
    assert recovered_run is not None
    assert recovered_run.is_aborted
    assert recovered_run.error_code == "PROCESS_TERMINATED_ABNORMALLY"
    # Staging must be cleared
    assert len(repo.get_staged_items_for_run("run_crash")) == 0


# =============================================================================
# 23. RawRef Provenance Persistence
# =============================================================================


def test_23_raw_ref_persistence(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_raw_ref")
    obs = make_sample_observation("run_raw_ref", "cid_raw")
    repo.append_observation(obs)

    loaded = repo.get_observations_for_run("run_raw_ref")[0]
    raw_ref = loaded["raw_ref"]
    assert raw_ref["archive_file"] == "data/raw/douyin/run_01/page_000001.json"
    assert len(raw_ref["sha256"]) == 64
    assert raw_ref["item_index"] == 0


# =============================================================================
# 24. Exact Decimal TEXT Precision for Cursors
# =============================================================================


def test_24_cursor_text_precision(repo: SqliteMetadataRepository) -> None:
    huge_cursor = "1788536236926781999999999999"
    repo.begin_sync_run("run_huge_cursor")
    repo.finalize_success("run_huge_cursor", candidate_watermark=huge_cursor)

    state = repo.get_sync_state()
    assert state is not None
    assert state.committed_watermark_cursor == huge_cursor
    assert isinstance(state.committed_watermark_cursor, str)


# =============================================================================
# 25. Full Canonical JSON Round-Trip Validation
# =============================================================================


def test_25_full_canonical_json_reload(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_json_rt")
    item = make_sample_item("rt_item_123")
    repo.stage_item("run_json_rt", item)
    repo.finalize_success("run_json_rt", candidate_watermark="100")

    reloaded = repo.get_item("rt_item_123")
    assert reloaded is not None
    assert reloaded["author"]["display_name"] == "Test Creator"
    assert reloaded["media"]["duration_seconds"] == 15.5
    assert reloaded["collection"]["observed_count"] == 1


# =============================================================================
# 26. Idempotent Retries for Begin and Finalize
# =============================================================================


def test_26_same_run_retry_idempotency(repo: SqliteMetadataRepository) -> None:
    run1 = repo.begin_sync_run("run_retry_idem")
    run2 = repo.begin_sync_run("run_retry_idem")
    assert run1.sync_run_id == run2.sync_run_id

    fin1 = repo.finalize_success("run_retry_idem", candidate_watermark="5000")
    fin2 = repo.finalize_success("run_retry_idem", candidate_watermark="5000")
    assert fin1.sync_run_id == fin2.sync_run_id
    assert fin1.status == fin2.status == "completed"


# =============================================================================
# 27. Credential Sanitization in Error Messages
# =============================================================================


def test_27_no_secret_persistence(repo: SqliteMetadataRepository) -> None:
    repo.begin_sync_run("run_sec_test")
    sensitive_err = "Auth failed with sessionid=secret_cookie_token_value_xyz"
    run_failed = repo.finalize_failure("run_sec_test", error_code="AUTH_FAIL", error_message=sensitive_err)

    assert "secret_cookie_token_value_xyz" not in (run_failed.error_message or "")
    assert "Sanitized" in (run_failed.error_message or "")


# =============================================================================
# 28. Multi-Scope Account Isolation
# =============================================================================


def test_28_scope_isolation(repo: SqliteMetadataRepository) -> None:
    scope_a = "douyin:user_A"
    scope_b = "douyin:user_B"

    # Stage same content_id in both scopes
    repo.begin_sync_run("run_scope_a", scope_id=scope_a)
    repo.stage_item("run_scope_a", make_sample_item("shared_cid", active=True), scope_id=scope_a)
    repo.finalize_success("run_scope_a", candidate_watermark="WM_A", scope_id=scope_a)

    repo.begin_sync_run("run_scope_b", scope_id=scope_b)
    repo.stage_item("run_scope_b", make_sample_item("shared_cid", active=True), scope_id=scope_b)
    repo.finalize_success("run_scope_b", candidate_watermark="WM_B", scope_id=scope_b)

    # Inactivate in scope A
    repo.begin_sync_run("run_inact_a", scope_id=scope_a)
    repo.finalize_success("run_inact_a", candidate_watermark="WM_A2", items_to_mark_inactive=["shared_cid"], scope_id=scope_a)

    item_a = repo.get_item("shared_cid", scope_id=scope_a)
    item_b = repo.get_item("shared_cid", scope_id=scope_b)
    assert item_a is not None and item_a["collection"]["active"] is False
    assert item_b is not None and item_b["collection"]["active"] is True

    state_a = repo.get_sync_state(scope_id=scope_a)
    state_b = repo.get_sync_state(scope_id=scope_b)
    assert state_a is not None and state_a.committed_watermark_cursor == "WM_A2"
    assert state_b is not None and state_b.committed_watermark_cursor == "WM_B"


# =============================================================================
# 29. Verification of Required Indexes
# =============================================================================


def test_29_indexes_exist(repo: SqliteMetadataRepository) -> None:
    conn = repo._get_connection()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index';").fetchall()
    index_names = {r["name"] for r in rows}

    assert "idx_items_active" in index_names
    assert "idx_items_last_seen" in index_names
    assert "idx_items_published" in index_names
    assert "idx_obs_run" in index_names
    assert "idx_obs_item" in index_names
    assert "idx_runs_status" in index_names


# =============================================================================
# 30. C06 -> C07 -> C08 Full Pipeline Integration
# =============================================================================


def test_30_c06_c07_c08_integration(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw_int"
    db_file = tmp_path / "metadata_int.db"

    archiver = DiskRawArchiver(raw_root)
    transformer = DouyinCanonicalTransformer(validate_schema=True)
    repository = SqliteMetadataRepository(db_file)

    sync_run_id = "run_pipeline_int_01"
    raw_payload = {
        "status_code": 0,
        "cursor": 1788536236926781,
        "has_more": 1,
        "aweme_list": [
            {
                "aweme_id": "7671141177986518318",
                "desc": "Integration pipeline test video",
                "create_time": 1725519600,
                "author": {
                    "uid": "11223344",
                    "nickname": "Pipeline Author",
                    "avatar_thumb": {"url_list": ["https://p3.douyinpic.com/avatar.jpg"]},
                },
                "video": {
                    "duration": 12000,
                    "width": 720,
                    "height": 1280,
                    "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpg"], "uri": "tos-cn-v-0000/cover1"},
                    "play_addr": {"uri": "tos-cn-v-0000/video1"},
                },
            }
        ],
    }

    # Step 1: C06 Archive (Raw Before Transform)
    archive_ref = archiver.archive_page(
        sync_run_id=sync_run_id,
        platform="douyin",
        page_number=1,
        request_cursor="0",
        response_cursor="1788536236926781",
        raw_response=raw_payload,
        fetched_at="2026-09-05T07:00:00Z",
    )
    assert archiver.verify(archive_ref)

    # Step 2: C08 Begin SyncRun
    repository.begin_sync_run(sync_run_id, mode="incremental")

    # Step 3: Prior state lookup (C08 -> C07)
    prior = repository.get_prior_state("7671141177986518318")
    assert not prior.exists

    # Step 4: C07 Transform with RawRef provenance
    raw_ref_ctx = RawRefContext(
        archive_file=archive_ref.path,
        sha256=archive_ref.sha256,
        item_index=0,
    )
    ctx = TransformContext(
        sync_run_id=sync_run_id,
        observed_at="2026-09-05T07:00:00Z",
        page_number=1,
        position_in_page=0,
        global_rank_seen=1,
        request_cursor="0",
        response_cursor="1788536236926781",
        raw_ref=raw_ref_ctx,
    )
    transform_result = transformer.transform_item(raw_payload["aweme_list"][0], ctx, prior)
    assert transform_result.validation_passed

    # Step 5: C08 Stage & Observation Persistence
    repository.stage_transform_result(transform_result, sync_run_id)

    # Step 6: C08 Finalize Success
    completed_run = repository.finalize_success(
        sync_run_id=sync_run_id,
        candidate_watermark="1788536236926781",
        stop_reason="watermark_reached",
        metrics={
            "pages_fetched": 1,
            "items_seen": 1,
            "new_items": 1,
            "reappeared_items": 0,
            "known_items": 0,
            "errors_count": 0,
        },
    )
    assert completed_run.is_completed

    # Step 7: Verify SQLite state
    item_persisted = repository.get_item("7671141177986518318")
    assert item_persisted is not None
    assert item_persisted["platform_content_id"] == "7671141177986518318"
    assert item_persisted["collection"]["active"] is True

    obs_persisted = repository.get_observations_for_run(sync_run_id)
    assert len(obs_persisted) == 1
    assert obs_persisted[0]["raw_ref"]["archive_file"] == archive_ref.path

    state_persisted = repository.get_sync_state()
    assert state_persisted is not None
    assert state_persisted.committed_watermark_cursor == "1788536236926781"
    assert state_persisted.incremental_head_watermark_cursor == "1788536236926781"
    assert state_persisted.history_complete is True

    repository.close()


def test_32_migration_v2_column_and_backfill(tmp_path: Path) -> None:
    """Tests migration from v1 database schema to v2."""
    db_file = tmp_path / "v1_legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-09-01T00:00:00Z', 'v1 Initial');")
    conn.execute("PRAGMA user_version = 1;")
    conn.execute(
        """
        CREATE TABLE sync_state (
            scope_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            committed_watermark_cursor TEXT,
            last_successful_sync_run_id TEXT,
            head_anchor_content_id TEXT,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (scope_id, platform)
        );
        """
    )
    conn.execute(
        "INSERT INTO sync_state (scope_id, platform, committed_watermark_cursor, updated_at) VALUES ('douyin:default', 'douyin', '999999', '2026-09-01T00:00:00Z');"
    )
    conn.commit()
    conn.close()

    # Now open with SqliteMetadataRepository
    repo = SqliteMetadataRepository(db_file)
    assert repo.is_initialized

    state = repo.get_sync_state()
    assert state is not None
    assert state.committed_watermark_cursor == "999999"
    # Backfilled during migration v2
    assert state.incremental_head_watermark_cursor == "999999"
    assert state.history_complete is False
    assert state.backfill_checkpoint_cursor is None

    conn2 = repo._get_connection()
    user_ver = conn2.execute("PRAGMA user_version;").fetchone()[0]
    assert user_ver == 3
    repo.close()


def test_34_migration_v3_download_outbox_table(tmp_path: Path) -> None:
    """Tests v3 migration creating download_outbox table and indexes."""
    db_file = tmp_path / "v3_outbox.db"
    repo = SqliteMetadataRepository(db_file)
    assert repo.is_initialized

    conn = repo._get_connection()
    user_ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert user_ver == 3

    # Verify download_outbox schema
    cols = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(download_outbox);").fetchall()}
    assert "outbox_id" in cols
    assert "task_id" in cols
    assert "payload_json" in cols
    assert "status" in cols
    assert "source_sync_run_id" in cols

    # Verify indexes
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(download_outbox);").fetchall()}
    assert "idx_outbox_status_avail" in indexes
    assert "idx_outbox_task" in indexes
    assert "idx_outbox_item" in indexes
    assert "idx_outbox_run" in indexes
    repo.close()


def test_33_sync_state_history_complete_and_backfill_checkpoint(tmp_path: Path) -> None:
    """Tests updating sync_state with history_complete and backfill_checkpoint_cursor."""
    db_file = tmp_path / "test_state.db"
    repo = SqliteMetadataRepository(db_file)
    scope = "douyin:dyacct_custom"

    # Step 1: Incomplete run (STOP_LIMIT) does NOT set watermark, sets backfill_checkpoint
    repo.begin_sync_run("run_limit_01", scope_id=scope)
    repo.finalize_success(
        "run_limit_01",
        candidate_watermark=None,
        stop_reason="max_pages_reached",
        history_complete=False,
        backfill_checkpoint_cursor="1788534410707763",
        scope_id=scope,
    )

    s1 = repo.get_sync_state(scope_id=scope)
    assert s1 is not None
    assert s1.incremental_head_watermark_cursor is None
    assert s1.history_complete is False
    assert s1.backfill_checkpoint_cursor == "1788534410707763"

    # Step 2: Complete run sets watermark and clears checkpoint
    repo.begin_sync_run("run_complete_02", scope_id=scope)
    repo.finalize_success(
        "run_complete_02",
        candidate_watermark="1788536229251543",
        stop_reason="end_of_collection",
        history_complete=True,
        scope_id=scope,
    )

    s2 = repo.get_sync_state(scope_id=scope)
    assert s2 is not None
    assert s2.incremental_head_watermark_cursor == "1788536229251543"
    assert s2.committed_watermark_cursor == "1788536229251543"
    assert s2.history_complete is True
    assert s2.backfill_checkpoint_cursor is None

    repo.close()

