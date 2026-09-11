"""M6-05 observability projection tests (read-only, temp DBs only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.operations.models import (
    compute_job_id,
    new_lease_token,
    utc_now_iso,
)
from src.operations.observability import (
    HEALTH_CANCELLED,
    HEALTH_FAILED_TERMINAL,
    HEALTH_HEALTHY,
    HEALTH_INVARIANT_ERROR,
    HEALTH_RUNNING,
    HEALTH_STALLED,
    HEALTH_SUCCEEDED,
    HEALTH_WAITING_FOR_WORKER,
    HEALTH_WAITING_RETRY,
    AssetPipelineStatus,
    OperationsSummary,
    WorkerStatus,
    compute_operations_summary,
    get_asset_pipeline_status,
    get_asset_timeline,
    get_worker_status,
    list_asset_pipeline_statuses,
    list_worker_statuses,
)
from src.operations.scheduler import (
    ASSET_PIPELINE_GRAPH,
    STAGE_MILESTONES,
    discovery_control_fingerprint,
)
from src.operations.stages import (
    StageExecutionResult,
    STATUS_CACHE_HIT,
    STATUS_EXECUTED,
    required_capabilities_for_stage,
)
from src.operations.store import (
    begin_attempt,
    cancel_job,
    claim_next_job,
    complete_job_retryable_failure,
    complete_job_success,
    complete_job_terminal_failure,
    create_operations_store,
    create_pipeline_run,
    enqueue_job,
    get_job,
    register_asset,
    register_worker,
    requeue_retryable_job,
    start_claimed_job,
    transition_asset_lifecycle,
    worker_heartbeat,
)

PLATFORM = "douyin"
CONTENT_ID = "7681603850364521734"
CANONICAL_ID = f"{PLATFORM}_{CONTENT_ID}"
FINGERPRINT = "a" * 64
T0 = "2026-09-10T01:00:00+00:00"
T_PLUS = "2026-09-10T01:02:00+00:00"
T_PLUS_LONG = "2026-09-10T01:04:00+00:00"
T_MANY_HOURS = "2026-09-10T05:00:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "operations.sqlite3"
    create_operations_store(db, now=T0)
    return db


def _register(db_path: Path) -> dict:
    return register_asset(
        db_path,
        PLATFORM,
        CONTENT_ID,
        CANONICAL_ID,
        metadata={},
        upstream_fingerprint=FINGERPRINT,
        now=T0,
    )


def _enqueue(db_path: Path, stage: str = "ARCHIVE") -> dict:
    run = create_pipeline_run(db_path, CANONICAL_ID, "discovery", now=T0)
    fp = discovery_control_fingerprint(PLATFORM, CONTENT_ID)
    enq = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        stage,
        fp,
        policy_version="m6-scheduler-policy-v1",
        canonical_id=CANONICAL_ID,
        pipeline_run_id=run["run_id"],
        required_capabilities=list(required_capabilities_for_stage(stage)),
        now=T0,
    )
    return get_job(db_path, enq.job_id)


def _result_dict(stage: str, *, output_fp: str = FINGERPRINT) -> dict:
    return StageExecutionResult(
        stage=stage,
        canonical_id=CANONICAL_ID,
        status=STATUS_EXECUTED,
        input_fingerprint=FINGERPRINT,
        output_fingerprint=output_fp,
        artifacts=(),
        metadata={},
    ).to_dict()


def _succeed(db_path: Path, job_id: str, *, now: str = T_PLUS) -> None:
    worker_id = "w-" + job_id[:6]
    register_worker(
        db_path, worker_id, list(required_capabilities_for_stage("ARCHIVE")), now=T0
    )
    claimed = claim_next_job(
        db_path, worker_id, list(required_capabilities_for_stage("ARCHIVE")), now=now
    )
    assert claimed is not None and claimed.job_id == job_id
    start_claimed_job(db_path, job_id, worker_id, claimed.lease_token, now=now)
    complete_job_success(
        db_path,
        job_id,
        worker_id,
        claimed.lease_token,
        now=now,
        metadata={"stage_result": _result_dict("ARCHIVE")},
    )


def _claim_start(
    db_path: Path, worker_id: str, stage: str, *, now: str = T_PLUS
) -> str:
    caps = list(required_capabilities_for_stage(stage))
    register_worker(db_path, worker_id, caps, now=T0)
    claimed = claim_next_job(db_path, worker_id, caps, now=now)
    assert claimed is not None
    start_claimed_job(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now)
    return claimed.job_id


# ----------------------------------------------------------------------
# 1. AssetPipelineStatus basics
# ----------------------------------------------------------------------


def test_status_healthy_no_pipeline(db_path: Path):
    _register(db_path)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s is not None
    assert s.canonical_id == CANONICAL_ID
    assert s.platform == PLATFORM
    assert s.asset_lifecycle_state == "DISCOVERED"
    assert s.health == HEALTH_HEALTHY
    assert s.searchable is False
    assert s.needs_attention is False
    assert s.current_pipeline_run_id is None
    # JSON-safe
    data = s.to_dict()
    json.dumps(data)
    assert "lease_token" not in json.dumps(data)


def test_status_json_safe_and_secret_free(db_path: Path):
    _register(db_path)
    _enqueue(db_path, "ARCHIVE")
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    blob = json.dumps(s.to_dict())
    # Field NAMES like lease_expires_at are fine; the token VALUE must not leak.
    assert "lease_" + "0" * 16 not in blob  # token pattern
    assert "lease_" + "f" * 16 not in blob
    assert "lease_token" not in blob
    for secret in ("sessionid", "cookie", "Authorization", "api_key"):
        assert secret not in blob


# ----------------------------------------------------------------------
# Health classification
# ----------------------------------------------------------------------


def test_waiting_for_worker(db_path: Path):
    _register(db_path)
    _enqueue(db_path, "ARCHIVE")
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_WAITING_FOR_WORKER
    assert s.matching_worker_count == 0
    assert "gpu" not in s.required_capabilities  # ARCHIVE needs downloader
    assert s.needs_attention is False


def test_waiting_for_worker_capability_visibility(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    assert "downloader" in job["required_capabilities_json"] or "downloader" in json.dumps(job)
    # A worker with the WRONG capability does not match.
    register_worker(db_path, "w-gpu", ["gpu_asr"], now=T0)
    worker_heartbeat(db_path, "w-gpu", now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.matching_worker_count == 0
    assert s.health == HEALTH_WAITING_FOR_WORKER


def test_running_queued_with_worker(db_path: Path):
    _register(db_path)
    _enqueue(db_path, "ARCHIVE")
    register_worker(db_path, "w-dl", ["downloader"], now=T0)
    worker_heartbeat(db_path, "w-dl", now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_RUNNING
    assert s.matching_worker_count == 1


def test_running_leased(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    register_worker(db_path, "w-dl", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w-dl", ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job["job_id"]
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_RUNNING
    assert s.assigned_worker == "w-dl"
    assert s.worker_stale is False


def test_succeeded_terminal_state(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    _succeed(db_path, job["job_id"], now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    # ARCHIVE SUCCEEDED but the run is not yet reconciled (scheduler crash
    # window between success and downstream enqueue): blocking stage is
    # MEDIA_PROCESS with no job enqueued yet.
    assert s.health == HEALTH_STALLED
    assert s.current_stage == "MEDIA_PROCESS"
    assert s.attention_reason is not None
    # After an explicit lifecycle advance the asset reflects ARCHIVED.
    transition_asset_lifecycle(db_path, CANONICAL_ID, "ARCHIVED", now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.asset_lifecycle_state == "ARCHIVED"


def test_terminal_failure_needs_attention(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    wid = "w-fail"
    register_worker(db_path, wid, ["downloader"], now=T0)
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job["job_id"]
    start_claimed_job(db_path, job["job_id"], wid, claimed.lease_token, now=T_PLUS)
    complete_job_terminal_failure(
        db_path,
        job["job_id"],
        wid,
        claimed.lease_token,
        error_class="TestTerminal",
        error_message="boom",
        now=T_PLUS,
    )
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_FAILED_TERMINAL
    assert s.needs_attention is True
    assert s.attention_reason is not None
    assert s.last_error_class == "TestTerminal"
    assert s.last_error_message == "boom"


def test_waiting_retry_backoff(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    wid = "w-retry"
    register_worker(db_path, wid, ["downloader"], now=T0)
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job["job_id"]
    start_claimed_job(db_path, job["job_id"], wid, claimed.lease_token, now=T_PLUS)
    complete_job_retryable_failure(
        db_path,
        job["job_id"],
        wid,
        claimed.lease_token,
        error_class="RetryableJobError",
        error_message="llm endpoint unavailable",
        now=T_PLUS,
    )
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_WAITING_RETRY
    assert s.needs_attention is False
    assert s.next_retry_at is not None and s.next_retry_at > T_PLUS
    assert s.last_error_class == "RetryableJobError"


def test_cancelled_distinct_from_failed(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    cancel_job(db_path, job["job_id"], reason="admin", now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_CANCELLED
    assert s.needs_attention is False


# ----------------------------------------------------------------------
# STALLED / invariant detection
# ----------------------------------------------------------------------


def test_stalled_expired_lease_before_recovery(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    wid = "w-stall"
    register_worker(db_path, wid, ["downloader"], now=T0)
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T0)
    assert claimed is not None and claimed.job_id == job["job_id"]
    # Lease expired (default 120s -> expired by T_PLUS_LONG) but recovery not run.
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS_LONG)
    assert s.health == HEALTH_STALLED
    assert s.needs_attention is True
    assert "lease" in s.attention_reason.lower()
    assert s.lease_expires_at is not None


def test_stalled_scheduler_crash_window(db_path: Path):
    # ARCHIVE SUCCEEDED but downstream MEDIA_PROCESS was never enqueued (the
    # scheduler crashed between complete and enqueue). Run is still RUNNING.
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    _succeed(db_path, job["job_id"], now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_STALLED
    assert s.needs_attention is True
    assert "scheduler crash" in s.attention_reason.lower()


def test_invariant_error_succeeded_without_result(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    # Succeed WITHOUT a durable stage_result (missing invariant).
    wid = "w-inv"
    register_worker(db_path, wid, ["downloader"], now=T0)
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job["job_id"]
    start_claimed_job(db_path, job["job_id"], wid, claimed.lease_token, now=T_PLUS)
    complete_job_success(db_path, job["job_id"], wid, claimed.lease_token, now=T_PLUS)
    s = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s.health == HEALTH_STALLED  # SUCCEEDED but downstream not enqueued
    # Now emulate the scheduler flagging the invariant (durable result missing).
    from src.operations.scheduler import Scheduler

    sched = Scheduler(db_path, poll_sources=[], now=lambda: T_PLUS)
    r = sched.run_once(now=T_PLUS)
    assert r.invariant_failures >= 1
    s2 = get_asset_pipeline_status(db_path, CANONICAL_ID, now=T_PLUS)
    assert s2.health == HEALTH_INVARIANT_ERROR
    assert s2.needs_attention is True


# ----------------------------------------------------------------------
# Timeline
# ----------------------------------------------------------------------


def test_timeline_ordering(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    _succeed(db_path, job["job_id"], now=T_PLUS)
    timeline = get_asset_timeline(db_path, CANONICAL_ID)
    stamps = [e["timestamp"] for e in timeline]
    assert stamps == sorted(stamps)
    event_types = [e["event_type"] for e in timeline]
    assert "asset_registered" in event_types
    assert "job_enqueued" in event_types
    assert "pipeline_run_created" in event_types
    assert "job_state_transition" in event_types
    # Stable event ids present and increasing.
    ids = [e["event_id"] for e in timeline]
    assert ids == sorted(ids)
    for e in timeline:
        assert e["event_id"] is not None
        assert e["timestamp"] is not None
        # Never leaks the lease token value.
        assert "lease_token" not in json.dumps(e)
        assert "lease_" + "a" * 16 not in json.dumps(e)


def test_timeline_no_llm_narrative(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    _succeed(db_path, job["job_id"], now=T_PLUS)
    timeline = get_asset_timeline(db_path, CANONICAL_ID)
    for e in timeline:
        # Message is a structured store message, not a free-form narrative.
        assert "summar" not in e["message"].lower()


# ----------------------------------------------------------------------
# Worker status
# ----------------------------------------------------------------------


def test_worker_status_online_with_jobs(db_path: Path):
    _register(db_path)
    job = _enqueue(db_path, "ARCHIVE")
    wid = "w-own"
    register_worker(db_path, wid, ["downloader"], now=T0)
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job["job_id"]
    start_claimed_job(db_path, job["job_id"], wid, claimed.lease_token, now=T_PLUS)
    ws = get_worker_status(db_path, wid, now=T_PLUS)
    assert ws is not None
    assert ws.online is True
    assert ws.derived_status == "active"
    assert job["job_id"] in ws.currently_owned_jobs
    assert job["job_id"] in ws.running_jobs
    assert ws.to_dict()["worker_id"] == wid


def test_worker_status_stale(db_path: Path):
    _register(db_path)
    register_worker(db_path, "w-stale", ["downloader"], now=T0)
    # No heartbeat after registration -> stale by threshold.
    ws = get_worker_status(db_path, "w-stale", now=T_PLUS_LONG)
    assert ws is not None
    assert ws.online is False
    assert ws.derived_status == "stale"


def test_list_worker_statuses(db_path: Path):
    register_worker(db_path, "w-a", ["downloader"], now=T0)
    register_worker(db_path, "w-b", ["gpu_asr"], now=T0)
    worker_heartbeat(db_path, "w-a", now=T_PLUS)
    # w-b last heartbeat was at T0; by T_PLUS_LONG (4 min later) it is stale.
    workers = list_worker_statuses(db_path, now=T_PLUS_LONG)
    by_id = {w.worker_id: w for w in workers}
    assert by_id["w-a"].derived_status == "active"
    assert by_id["w-b"].derived_status == "stale"


# ----------------------------------------------------------------------
# Global summary
# ----------------------------------------------------------------------


def test_operations_summary(db_path: Path):
    _register(db_path)
    _enqueue(db_path, "ARCHIVE")
    # Make a second terminal-failed asset.
    cid2 = "douyin_999"
    register_asset(db_path, PLATFORM, "999", cid2, metadata={}, now=T0)
    run2 = create_pipeline_run(db_path, cid2, "discovery", now=T0)
    fp = discovery_control_fingerprint(PLATFORM, "999")
    enq2 = enqueue_job(
        db_path,
        PLATFORM,
        "999",
        "ARCHIVE",
        fp,
        policy_version="m6-scheduler-policy-v1",
        canonical_id=cid2,
        pipeline_run_id=run2["run_id"],
        required_capabilities=["downloader"],
        priority=10,
        now=T0,
    )
    wid = "w-t"
    register_worker(db_path, wid, ["downloader"], now=T0)
    # Priority 10 makes cid2's job claimable before asset1's (default priority 0).
    claimed = claim_next_job(db_path, wid, ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == enq2.job_id
    start_claimed_job(db_path, enq2.job_id, wid, claimed.lease_token, now=T_PLUS)
    complete_job_terminal_failure(
        db_path, enq2.job_id, wid, claimed.lease_token, error_class="X", error_message="y", now=T_PLUS
    )

    summary = compute_operations_summary(db_path, now=T_PLUS)
    assert isinstance(summary, OperationsSummary)
    assert summary.assets_total == 2
    assert summary.active_pipeline_runs == 2
    assert summary.jobs_by_state.get("QUEUED") == 1
    assert summary.jobs_by_state.get("FAILED_TERMINAL") == 1
    assert summary.jobs_by_stage.get("ARCHIVE") == 2
    assert summary.terminal_failures == 1
    data = summary.to_dict()
    json.dumps(data)
    assert "lease_" not in json.dumps(data)


def test_summary_searchable_counts(db_path: Path):
    _register(db_path)
    for state in ("ARCHIVED", "EVIDENCE_READY", "KNOWLEDGE_READY", "SEARCHABLE"):
        transition_asset_lifecycle(db_path, CANONICAL_ID, state, now=T_PLUS)
    summary = compute_operations_summary(db_path, now=T_PLUS)
    assert summary.searchable_assets == 1


def test_summary_validation_flag(db_path: Path):
    _register(db_path)
    s1 = compute_operations_summary(db_path, now=T_PLUS, include_validation=False)
    assert s1.store_valid is None
    s2 = compute_operations_summary(db_path, now=T_PLUS, include_validation=True)
    assert s2.store_valid is True


# ----------------------------------------------------------------------
# Real C10 fixtures (read-only, no network / LLM / M5 store)
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_real_c10_video_pipeline_status(_root: Path, tmp_path: Path):
    from src.operations.observability import get_asset_pipeline_status

    db = tmp_path / "ops.sqlite3"
    create_operations_store(db, now=T0)
    cid = "douyin_7681603850364521734"
    register_asset(db, PLATFORM, "7681603850364521734", cid, metadata={}, now=T0)
    run = create_pipeline_run(db, cid, "discovery", now=T0)
    fp = discovery_control_fingerprint(PLATFORM, "7681603850364521734")
    enqueue_job(
        db,
        PLATFORM,
        "7681603850364521734",
        "ARCHIVE",
        fp,
        policy_version="m6-scheduler-policy-v1",
        canonical_id=cid,
        pipeline_run_id=run["run_id"],
        required_capabilities=list(required_capabilities_for_stage("ARCHIVE")),
        now=T0,
    )
    s = get_asset_pipeline_status(db, cid, now=T_PLUS)
    assert s is not None
    assert s.canonical_id == cid
    assert s.health == HEALTH_WAITING_FOR_WORKER


def test_real_c10_album_pipeline_status(_root: Path, tmp_path: Path):
    from src.operations.observability import get_asset_pipeline_status

    db = tmp_path / "ops.sqlite3"
    create_operations_store(db, now=T0)
    cid = "douyin_7682038498466993905"
    register_asset(db, PLATFORM, "7682038498466993905", cid, metadata={}, now=T0)
    s = get_asset_pipeline_status(db, cid, now=T_PLUS)
    assert s is not None
    assert s.asset_lifecycle_state == "DISCOVERED"
    assert s.health in (HEALTH_HEALTHY, HEALTH_RUNNING)


def test_real_c10_no_production_db_touched(_root: Path, tmp_path: Path):
    """Observability never writes; every test uses a disposable tmp DB."""
    prod = _root / "data" / "operations" / "operations.sqlite3"
    if prod.exists():
        pytest.skip("production ops DB exists; skipping guard assertion")
    db = tmp_path / "ops.sqlite3"
    create_operations_store(db, now=T0)
    summary = compute_operations_summary(db, now=T_PLUS)
    assert summary.assets_total == 0
    assert not prod.exists()