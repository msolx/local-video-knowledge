"""M6-01 Durable Operations Store & Job State Machine tests.

Covers the M6-01 contract: schema/versioning, asset lifecycle, deterministic
job identity + enqueue idempotency, changed-input generations, pipeline runs,
job transitions, attempts, retry exhaustion, cancel semantics, lease fields,
workers, event log, transactions, FK integrity, validation, and the synthetic
end-to-end flow — all on temp DBs (never the production operations DB).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import src.operations.store as ops_store
import src.operations.models as ops_models
from src.operations.store import (
    OPERATIONS_SCHEMA_VERSION,
    OPERATIONS_USER_VERSION,
    OperationsIntegrityError,
    OperationsSchemaError,
    OperationsStateError,
    begin_attempt,
    cancel_job,
    clear_job_lease,
    complete_pipeline_run,
    create_operations_store,
    create_pipeline_run,
    enqueue_job,
    finish_attempt,
    get_asset,
    get_asset_by_canonical_id,
    get_job,
    get_pipeline_run,
    list_events,
    list_failed_jobs,
    list_job_attempts,
    list_jobs,
    list_pending_jobs,
    list_workers,
    open_operations_store,
    register_asset,
    register_worker,
    requeue_retryable_job,
    set_job_lease,
    transition_asset_lifecycle,
    transition_job_state,
    validate_operations_store,
    worker_heartbeat,
)
from src.operations.models import (
    AssetLifecycleState,
    JobStage,
    JobState,
    TriggerType,
    compute_job_id,
    compute_next_retry_at,
    is_legal_asset_transition,
    is_legal_job_transition,
    is_terminal_job_state,
    utc_now_iso,
)

PLATFORM = "douyin"
CONTENT_ID = "cid_7681603850364521734"
CANONICAL_ID = "douyin_7681603850364521734"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "operations.sqlite3"


def _register(db, canonical_id=CANONICAL_ID, content_id=CONTENT_ID, **kw):
    return register_asset(
        db,
        PLATFORM,
        content_id,
        canonical_id,
        metadata=kw.pop("metadata", {"note": "unit test asset"}),
        upstream_fingerprint=kw.pop("upstream_fingerprint", "f" * 64),
    )


def _fp(char="a"):
    return char * 64


def _enqueue(db, canonical_id=CANONICAL_ID, stage=JobStage.DISCOVER.value, fingerprint=None, **kw):
    return enqueue_job(
        db,
        PLATFORM,
        CONTENT_ID,
        stage,
        fingerprint or _fp("a"),
        policy_version=kw.pop("policy_version", "operations-policy-v1"),
        canonical_id=canonical_id,
        **kw,
    )


def _lease_and_run(db, job_id, now="2026-09-10T01:00:00+00:00"):
    set_job_lease(db, job_id, lease_owner="worker-1", lease_expires_at="2026-09-10T01:30:00+00:00", now=now)
    transition_job_state(db, job_id, JobState.LEASED.value, now=now)
    transition_job_state(db, job_id, JobState.RUNNING.value, now=now)


def _succeed(db, job_id, now="2026-09-10T01:00:05+00:00"):
    attempt = begin_attempt(db, job_id, worker_id="worker-1", now=now)
    finish_attempt(db, attempt["attempt_id"], outcome="succeeded", now=now)


# ----------------------------------------------------------------------
# 1-4. Init / schema / versions
# ----------------------------------------------------------------------


def test_fresh_db_initializes(db_path):
    create_operations_store(db_path)
    assert db_path.exists()


def test_user_version_and_meta(db_path):
    create_operations_store(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == OPERATIONS_USER_VERSION
    finally:
        conn.close()
    # foreign_keys=ON is a per-connection pragma; verify via the store's open.
    conn = open_operations_store(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_required_tables_present(db_path):
    create_operations_store(db_path)
    conn = open_operations_store(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    for table in (
        "operations_meta",
        "assets",
        "pipeline_runs",
        "jobs",
        "job_attempts",
        "workers",
        "event_log",
    ):
        assert table in names


def test_incompatible_user_version_fails(db_path):
    create_operations_store(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(OperationsSchemaError):
        open_operations_store(db_path)


def test_open_creates_when_missing(db_path):
    conn = open_operations_store(db_path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == OPERATIONS_USER_VERSION
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 5-8. Asset registration / lifecycle
# ----------------------------------------------------------------------


def test_asset_registration(db_path):
    _register(db_path)
    asset = get_asset(db_path, PLATFORM, CONTENT_ID)
    assert asset is not None
    assert asset["canonical_id"] == CANONICAL_ID
    assert asset["lifecycle_state"] == AssetLifecycleState.DISCOVERED.value
    assert asset["created_at"] == asset["updated_at"]


def test_duplicate_asset_is_single_row(db_path):
    _register(db_path)
    _register(db_path)
    _register(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT count(*) FROM assets").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    obs = [
        e
        for e in list_events(db_path, canonical_id=CANONICAL_ID)
        if e["event_type"] == "asset_observed_duplicate"
    ]
    assert len(obs) == 2


def test_asset_identity_conflict_rejected(db_path):
    _register(db_path)
    with pytest.raises(OperationsIntegrityError):
        register_asset(db_path, PLATFORM, CONTENT_ID, "other_canonical_id")


def test_canonical_id_conflict_rejected(db_path):
    _register(db_path)
    with pytest.raises(OperationsIntegrityError):
        register_asset(db_path, PLATFORM, "other_content", CANONICAL_ID)


def test_asset_lifecycle_legal_transition(db_path):
    _register(db_path)
    for state in (
        AssetLifecycleState.ARCHIVED.value,
        AssetLifecycleState.EVIDENCE_READY.value,
        AssetLifecycleState.KNOWLEDGE_READY.value,
        AssetLifecycleState.SEARCHABLE.value,
    ):
        asset = transition_asset_lifecycle(db_path, CANONICAL_ID, state)
        assert asset["lifecycle_state"] == state
    assert is_legal_asset_transition(
        AssetLifecycleState.KNOWLEDGE_READY.value,
        AssetLifecycleState.SEARCHABLE.value,
    )


def test_asset_lifecycle_illegal_regression(db_path):
    _register(db_path)
    with pytest.raises(OperationsStateError):
        transition_asset_lifecycle(db_path, CANONICAL_ID, AssetLifecycleState.SEARCHABLE.value)
    # Move forward first, then attempt an illegal regression.
    transition_asset_lifecycle(db_path, CANONICAL_ID, AssetLifecycleState.ARCHIVED.value)
    with pytest.raises(OperationsStateError):
        transition_asset_lifecycle(db_path, CANONICAL_ID, AssetLifecycleState.DISCOVERED.value)
    assert not is_legal_asset_transition(
        AssetLifecycleState.SEARCHABLE.value,
        AssetLifecycleState.DISCOVERED.value,
    )


def test_admin_requeue_asset_lifecycle(db_path):
    _register(db_path)
    asset = ops_store.admin_requeue_asset_lifecycle(
        db_path, CANONICAL_ID, AssetLifecycleState.ARCHIVED.value, reason="reconcile"
    )
    assert asset["lifecycle_state"] == AssetLifecycleState.ARCHIVED.value


def test_get_asset_by_canonical_id(db_path):
    _register(db_path)
    asset = get_asset_by_canonical_id(db_path, CANONICAL_ID)
    assert asset is not None
    assert asset["platform_content_id"] == CONTENT_ID


# ----------------------------------------------------------------------
# 9-13. Job identity / enqueue idempotency
# ----------------------------------------------------------------------


def test_deterministic_job_id(db_path):
    a = compute_job_id(PLATFORM, CONTENT_ID, JobStage.DISCOVER.value, _fp("a"), "operations-policy-v1")
    b = compute_job_id(PLATFORM, CONTENT_ID, JobStage.DISCOVER.value, _fp("a"), "operations-policy-v1")
    assert a == b
    assert a.startswith("job_")
    assert len(a) == 4 + 16
    c = compute_job_id(PLATFORM, CONTENT_ID, JobStage.DISCOVER.value, _fp("b"), "operations-policy-v1")
    assert a != c


def test_job_id_excludes_time_and_attempt(db_path):
    job_id = compute_job_id(PLATFORM, CONTENT_ID, "ARCHIVE", _fp("a"), "operations-policy-v1")
    # Same logical job at a different wall time / attempt → identical.
    assert (
        compute_job_id(PLATFORM, CONTENT_ID, "ARCHIVE", _fp("a"), "operations-policy-v1")
        == job_id
    )


def test_enqueue_creates_job(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    assert res.outcome == "enqueued"
    assert res.created is True
    assert res.state == JobState.QUEUED.value
    job = get_job(db_path, res.job_id)
    assert job is not None
    assert job["canonical_id"] == CANONICAL_ID
    assert job["stage"] == JobStage.DISCOVER.value


def test_duplicate_enqueue_same_job(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    r2 = _enqueue(db_path)
    assert r1.job_id == r2.job_id
    assert r2.outcome == "exists_active"
    assert r2.created is False
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_enqueue_requires_registered_asset(db_path):
    with pytest.raises(OperationsIntegrityError):
        _enqueue(db_path)


def test_succeeded_enqueue_skips(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    _lease_and_run(db_path, r1.job_id)
    _succeed(db_path, r1.job_id)
    assert get_job(db_path, r1.job_id)["state"] == JobState.SUCCEEDED.value
    r2 = _enqueue(db_path)
    assert r2.outcome == "existing_success"
    assert r2.created is False
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_enqueue_retryable_does_not_duplicate(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    _lease_and_run(db_path, r1.job_id)
    attempt = begin_attempt(db_path, r1.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=True)
    assert get_job(db_path, r1.job_id)["state"] == JobState.FAILED_RETRYABLE.value
    r2 = _enqueue(db_path)
    assert r2.outcome == "exists_retryable"
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_enqueue_terminal_not_resurrected(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    _lease_and_run(db_path, r1.job_id)
    attempt = begin_attempt(db_path, r1.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=False)
    assert get_job(db_path, r1.job_id)["state"] == JobState.FAILED_TERMINAL.value
    r2 = _enqueue(db_path)
    assert r2.outcome == "exists_terminal"


def test_enqueue_cancelled_not_resurrected(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    cancel_job(db_path, r1.job_id)
    r2 = _enqueue(db_path)
    assert r2.outcome == "exists_cancelled"


# ----------------------------------------------------------------------
# 13-14. Changed input fingerprint → new generation
# ----------------------------------------------------------------------


def test_changed_fingerprint_new_job(db_path):
    _register(db_path)
    r_a = _enqueue(db_path, stage=JobStage.MEDIA_PROCESS.value, fingerprint=_fp("a"))
    _lease_and_run(db_path, r_a.job_id)
    _succeed(db_path, r_a.job_id)
    r_b = _enqueue(db_path, stage=JobStage.MEDIA_PROCESS.value, fingerprint=_fp("b"))
    assert r_b.outcome == "enqueued"
    assert r_b.job_id != r_a.job_id
    # Both generations retained.
    jobs = list_jobs(db_path, stage=JobStage.MEDIA_PROCESS.value)
    assert len(jobs) == 2
    assert get_job(db_path, r_a.job_id)["state"] == JobState.SUCCEEDED.value
    assert get_job(db_path, r_b.job_id)["state"] == JobState.QUEUED.value


# ----------------------------------------------------------------------
# 14-15. Pipeline runs
# ----------------------------------------------------------------------


def test_pipeline_run_lifecycle(db_path):
    _register(db_path)
    run = create_pipeline_run(db_path, CANONICAL_ID, TriggerType.DISCOVERY.value)
    assert run["status"] == "RUNNING"
    assert run["trigger_type"] == TriggerType.DISCOVERY.value
    done = complete_pipeline_run(db_path, run["run_id"], "SUCCEEDED")
    assert done["status"] == "SUCCEEDED"
    assert done["completed_at"] is not None


def test_job_run_relationship(db_path):
    _register(db_path)
    run = create_pipeline_run(db_path, CANONICAL_ID, TriggerType.MANUAL.value)
    res = _enqueue(db_path, pipeline_run_id=run["run_id"])
    job = get_job(db_path, res.job_id)
    assert job["pipeline_run_id"] == run["run_id"]
    # Identity does not include the run id (deterministic across ticks).
    res2 = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.DISCOVER.value,
        _fp("a"),
        policy_version="operations-policy-v1",
        canonical_id=CANONICAL_ID,
    )
    assert res2.job_id == res.job_id


def test_pipeline_run_bad_trigger_rejected(db_path):
    _register(db_path)
    with pytest.raises(OperationsIntegrityError):
        create_pipeline_run(db_path, CANONICAL_ID, "nonsense")


def test_complete_non_running_run_rejected(db_path):
    _register(db_path)
    run = create_pipeline_run(db_path, CANONICAL_ID, TriggerType.MANUAL.value)
    complete_pipeline_run(db_path, run["run_id"], "SUCCEEDED")
    with pytest.raises(OperationsStateError):
        complete_pipeline_run(db_path, run["run_id"], "FAILED")


# ----------------------------------------------------------------------
# 16-21. Job transitions / terminal / cancel
# ----------------------------------------------------------------------


def test_valid_job_transition_chain(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    assert get_job(db_path, res.job_id)["state"] == JobState.RUNNING.value
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="succeeded")
    assert get_job(db_path, res.job_id)["state"] == JobState.SUCCEEDED.value


def test_illegal_job_transition(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    # QUEUED -> RUNNING skips LEASED → illegal.
    with pytest.raises(OperationsStateError):
        transition_job_state(db_path, res.job_id, JobState.RUNNING.value)
    _lease_and_run(db_path, res.job_id)
    # RUNNING -> QUEUED is not a legal normal transition.
    with pytest.raises(OperationsStateError):
        transition_job_state(db_path, res.job_id, JobState.QUEUED.value)


def test_leased_requires_lease(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    with pytest.raises(OperationsStateError):
        transition_job_state(db_path, res.job_id, JobState.LEASED.value)


def test_terminal_states_immutable(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    with pytest.raises(OperationsStateError):
        transition_job_state(db_path, res.job_id, JobState.QUEUED.value)
    with pytest.raises(OperationsStateError):
        transition_job_state(db_path, res.job_id, JobState.FAILED_RETRYABLE.value)
    assert is_terminal_job_state(JobState.SUCCEEDED.value)


def test_queued_cancel(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    job = cancel_job(db_path, res.job_id, reason="admin")
    assert job["state"] == JobState.CANCELLED.value


def test_cancel_twice_noop(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    cancel_job(db_path, res.job_id)
    job = cancel_job(db_path, res.job_id)
    assert job["state"] == JobState.CANCELLED.value


def test_succeeded_cancel_rejected(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    job = cancel_job(db_path, res.job_id)
    assert job["state"] == JobState.SUCCEEDED.value  # deterministic no-op


def test_running_cancel_rejected(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    job = cancel_job(db_path, res.job_id)
    assert job["state"] == JobState.RUNNING.value


def test_cancelled_cannot_requeue(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    cancel_job(db_path, res.job_id)
    with pytest.raises(OperationsStateError):
        requeue_retryable_job(db_path, res.job_id)


# ----------------------------------------------------------------------
# 22-25. Retryable failure / requeue / exhaustion / next_retry_at
# ----------------------------------------------------------------------


def test_retryable_failure_sets_backoff(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(
        db_path,
        attempt["attempt_id"],
        outcome="failed",
        retryable=True,
        error_class="ValueError",
        error_message="boom",
    )
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_RETRYABLE.value
    assert job["attempt_count"] == 1
    assert job["next_retry_at"] is not None


def test_requeue_retryable(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=True)
    job = requeue_retryable_job(db_path, res.job_id)
    assert job["state"] == JobState.QUEUED.value
    assert job["next_retry_at"] is None


def test_retry_exhaustion(db_path):
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    job_id = res.job_id
    for attempt_no in range(1, 3):
        _lease_and_run(db_path, job_id, now=f"2026-09-10T01:00:{attempt_no:02d}+00:00")
        attempt = begin_attempt(db_path, job_id, worker_id="worker-1", now=f"2026-09-10T01:00:{attempt_no:02d}+00:00")
        finish_attempt(
            db_path, attempt["attempt_id"], outcome="failed", retryable=True, now=f"2026-09-10T01:00:{attempt_no:02d}+00:00"
        )
        assert get_job(db_path, job_id)["state"] == JobState.FAILED_RETRYABLE.value
        requeue_retryable_job(db_path, job_id)
        assert get_job(db_path, job_id)["state"] == JobState.QUEUED.value

    # 3rd attempt exhausts → terminal, cannot requeue.
    _lease_and_run(db_path, job_id, now="2026-09-10T01:00:03+00:00")
    attempt = begin_attempt(db_path, job_id, worker_id="worker-1", now="2026-09-10T01:00:03+00:00")
    finish_attempt(
        db_path, attempt["attempt_id"], outcome="failed", retryable=True, now="2026-09-10T01:00:03+00:00"
    )
    job = get_job(db_path, job_id)
    assert job["state"] == JobState.FAILED_TERMINAL.value
    with pytest.raises(OperationsStateError):
        requeue_retryable_job(db_path, job_id)


def test_non_retryable_failure_terminal(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=False)
    assert get_job(db_path, res.job_id)["state"] == JobState.FAILED_TERMINAL.value


def test_next_retry_at_is_utc_iso(db_path):
    now = utc_now_iso()
    later = compute_next_retry_at(now, 1)
    assert later.endswith("+00:00")
    assert datetime.fromisoformat(later) > datetime.fromisoformat(now)


# ----------------------------------------------------------------------
# 26-27. Attempts
# ----------------------------------------------------------------------


def test_attempt_numbering_monotonic(db_path):
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    for i in range(1, 3):
        _lease_and_run(db_path, res.job_id)
        attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
        assert attempt["attempt_number"] == i
        finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=True)
        requeue_retryable_job(db_path, res.job_id)


def test_attempt_persistence(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(
        db_path,
        attempt["attempt_id"],
        outcome="failed",
        retryable=True,
        error_class="RuntimeError",
        error_message="network down",
    )
    attempts = list_job_attempts(db_path, res.job_id)
    assert len(attempts) == 1
    a = attempts[0]
    assert a["attempt_number"] == 1
    assert a["outcome"] == "failed"
    assert a["error_class"] == "RuntimeError"
    assert a["error_message"] == "network down"
    assert a["retryable"] == 1
    assert a["started_at"] is not None and a["finished_at"] is not None


def test_begin_attempt_requires_active_state(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    with pytest.raises(OperationsStateError):
        begin_attempt(db_path, res.job_id)  # QUEUED


def test_finish_twice_rejected(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="succeeded")
    with pytest.raises(OperationsStateError):
        finish_attempt(db_path, attempt["attempt_id"], outcome="succeeded")


# ----------------------------------------------------------------------
# 28-31. Event log
# ----------------------------------------------------------------------


def test_event_on_enqueue(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    events = list_events(db_path, job_id=res.job_id)
    assert any(e["event_type"] == "job_enqueued" for e in events)


def test_event_on_transition(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    events = list_events(db_path, job_id=res.job_id)
    transitions = [e for e in events if e["event_type"] == "job_state_transition"]
    assert len(transitions) == 2
    assert transitions[0]["from_state"] == JobState.QUEUED.value
    assert transitions[0]["to_state"] == JobState.LEASED.value


def test_event_on_attempt(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    attempt = begin_attempt(db_path, res.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="succeeded")
    events = list_events(db_path, job_id=res.job_id)
    types = {e["event_type"] for e in events}
    assert "attempt_begun" in types
    assert "attempt_finished" in types


def test_event_append_only_api(db_path):
    _register(db_path)
    _enqueue(db_path)
    # Public API must not expose event mutation.
    assert not hasattr(ops_store, "update_event")
    assert not hasattr(ops_store, "delete_event")


def test_event_order_stable(db_path):
    _register(db_path)
    for _ in range(5):
        _enqueue(db_path, fingerprint=_fp("a"))
    events = list_events(db_path)
    ids = [e["event_rowid"] for e in events]
    assert ids == sorted(ids)


# ----------------------------------------------------------------------
# 32-33. Workers / capabilities / leases
# ----------------------------------------------------------------------


def test_worker_persistence(db_path):
    register_worker(db_path, "worker-1", ["gpu_asr"], display_name="asr-gpu")
    workers = list_workers(db_path)
    assert len(workers) == 1
    assert workers[0]["capabilities_json"] == '["gpu_asr"]'
    assert workers[0]["display_name"] == "asr-gpu"


def test_worker_heartbeat(db_path):
    register_worker(db_path, "worker-1", ["gpu_asr"])
    w = worker_heartbeat(db_path, "worker-1", status="active")
    assert w["status"] == "active"
    assert w["last_heartbeat_at"] is not None


def test_worker_heartbeat_unregistered_fails(db_path):
    with pytest.raises(OperationsIntegrityError):
        worker_heartbeat(db_path, "ghost", status="active")


def test_capabilities_roundtrip(db_path):
    register_worker(db_path, "worker-1", ["collector", "downloader", "cpu_media"])
    workers = list_workers(db_path)
    caps = json.loads(workers[0]["capabilities_json"])
    assert caps == ["collector", "downloader", "cpu_media"]


def test_lease_fields_persist_and_clear(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    set_job_lease(
        db_path,
        res.job_id,
        lease_owner="worker-1",
        lease_expires_at="2026-09-10T01:30:00+00:00",
        lease_token="tok-1",
    )
    job = get_job(db_path, res.job_id)
    assert job["lease_owner"] == "worker-1"
    assert job["lease_token"] == "tok-1"
    clear_job_lease(db_path, res.job_id)
    job = get_job(db_path, res.job_id)
    assert job["lease_owner"] is None
    assert job["lease_token"] is None


def test_lease_on_terminal_rejected(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    with pytest.raises(OperationsStateError):
        set_job_lease(db_path, res.job_id, lease_owner="x", lease_expires_at="2026-09-10T01:30:00+00:00")


def test_lease_expiry_recovery_requeue(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    set_job_lease(
        db_path,
        res.job_id,
        lease_owner="worker-1",
        lease_expires_at="2026-09-10T01:30:00+00:00",
    )
    transition_job_state(db_path, res.job_id, JobState.LEASED.value)
    job = transition_job_state(db_path, res.job_id, JobState.QUEUED.value, reason="lease-expired")
    assert job["state"] == JobState.QUEUED.value
    assert job["lease_owner"] is None


# ----------------------------------------------------------------------
# 35-38. Transactions / FK / validation
# ----------------------------------------------------------------------


def test_transaction_rollback_on_event_failure(db_path, monkeypatch):
    _register(db_path)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise RuntimeError("event write failed")

    monkeypatch.setattr(ops_store, "_append_event", boom)
    with pytest.raises(RuntimeError):
        _enqueue(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
        e = conn.execute("SELECT count(*) FROM event_log").fetchone()[0]
    finally:
        conn.close()
    assert n == 0  # job insert rolled back with the failed event
    assert e == 1  # only the pre-existing asset_registered event remains


def test_fk_integrity_enforced(db_path):
    _register(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs(job_id, platform, platform_content_id, canonical_id, "
                "stage, state, input_fingerprint, policy_version, max_attempts, "
                "enqueued_at, updated_at, created_at) "
                "VALUES ('job_x', 'p', 'c', 'missing_asset', 'DISCOVER', 'QUEUED', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'v1', 5, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00')"
            )
    finally:
        conn.close()


def test_validation_clean(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    result = validate_operations_store(db_path)
    assert result.valid is True
    assert result.violations == []
    assert result.schema_version == OPERATIONS_SCHEMA_VERSION
    assert result.counts["jobs"] == 1
    assert result.counts["job_attempts"] == 1
    assert result.counts["events"] >= 1


def test_validation_corrupted_state(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE jobs SET state='BOGUS' WHERE job_id=?", (res.job_id,))
        conn.execute(
            "UPDATE assets SET lifecycle_state='NOPE' WHERE canonical_id=?",
            (CANONICAL_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    joined = "\n".join(result.violations)
    assert "BOGUS" in joined
    assert "NOPE" in joined


def test_validation_corrupted_job_id(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE jobs SET state='RUNNING', lease_owner=NULL WHERE job_id=?", (res.job_id,))
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("without active lease" in v for v in result.violations)


def test_validation_attempt_count_inconsistent(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    _lease_and_run(db_path, res.job_id)
    _succeed(db_path, res.job_id)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE jobs SET attempt_count=99 WHERE job_id=?", (res.job_id,))
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("attempt_count" in v for v in result.violations)


# ----------------------------------------------------------------------
# 39-40. Read APIs
# ----------------------------------------------------------------------


def test_list_pending(db_path):
    _register(db_path)
    _enqueue(db_path, fingerprint=_fp("a"))
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, fingerprint=_fp("a"))
    pending = list_pending_jobs(db_path)
    assert len(pending) == 2


def test_list_failed(db_path):
    _register(db_path)
    r1 = _enqueue(db_path)
    _lease_and_run(db_path, r1.job_id)
    attempt = begin_attempt(db_path, r1.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt["attempt_id"], outcome="failed", retryable=True)
    failed = list_failed_jobs(db_path)
    assert len(failed) == 1
    assert failed[0]["state"] == JobState.FAILED_RETRYABLE.value


def test_list_jobs_filters(db_path):
    _register(db_path)
    _enqueue(db_path, fingerprint=_fp("a"))
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, fingerprint=_fp("a"))
    by_state = list_jobs(db_path, state=JobState.QUEUED.value)
    assert len(by_state) == 2
    by_stage = list_jobs(db_path, stage=JobStage.ARCHIVE.value)
    assert len(by_stage) == 1
    by_asset = list_jobs(db_path, canonical_id=CANONICAL_ID)
    assert len(by_asset) == 2


def test_get_pipeline_run(db_path):
    _register(db_path)
    run = create_pipeline_run(db_path, CANONICAL_ID, TriggerType.MANUAL.value)
    got = get_pipeline_run(db_path, run["run_id"])
    assert got is not None
    assert got["run_id"] == run["run_id"]


# ----------------------------------------------------------------------
# 41-43. Unicode / injection / UTC
# ----------------------------------------------------------------------


def test_unicode_metadata(db_path):
    _register(db_path, metadata={"主题": "本地大模型推理", "emoji": "🔥"})
    _enqueue(db_path, metadata={"说明": "测试 中文 与 🔥"})
    asset = get_asset(db_path, PLATFORM, CONTENT_ID)
    assert json.loads(asset["metadata_json"])["主题"] == "本地大模型推理"
    job = get_job(db_path, _enqueue(db_path).job_id)
    assert json.loads(job["metadata_json"])["说明"] == "测试 中文 与 🔥"


def test_sql_injection_safe(db_path):
    evil = "x'; DROP TABLE jobs; --"
    register_asset(db_path, PLATFORM, CONTENT_ID, CANONICAL_ID)
    enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.DISCOVER.value,
        _fp("a"),
        policy_version="v1",
        canonical_id=CANONICAL_ID,
        metadata={"attack": evil},
    )
    result = validate_operations_store(db_path)
    assert result.valid is True  # jobs table still exists and validates


def test_timestamps_utc(db_path):
    _register(db_path)
    res = _enqueue(db_path)
    asset = get_asset(db_path, PLATFORM, CONTENT_ID)
    for key in ("created_at", "updated_at"):
        assert asset[key].endswith("+00:00")
        datetime.fromisoformat(asset[key])
    job = get_job(db_path, res.job_id)
    for key in ("created_at", "enqueued_at", "updated_at"):
        assert job[key].endswith("+00:00")


def test_fingerprint_validation_rejects_nonsecret_timestamp(db_path):
    with pytest.raises(ValueError):
        compute_job_id(PLATFORM, CONTENT_ID, "DISCOVER", "not-a-fingerprint", "v1")


# ----------------------------------------------------------------------
# 44. Synthetic complete flow
# ----------------------------------------------------------------------


def test_synthetic_complete_flow(db_path):
    _register(db_path)

    run = create_pipeline_run(db_path, CANONICAL_ID, TriggerType.DISCOVERY.value)

    # DISCOVER job → succeeded.
    discover = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.DISCOVER.value,
        _fp("d"),
        policy_version="operations-policy-v1",
        canonical_id=CANONICAL_ID,
        pipeline_run_id=run["run_id"],
    )
    _lease_and_run(db_path, discover.job_id)
    _succeed(db_path, discover.job_id)
    assert get_job(db_path, discover.job_id)["state"] == JobState.SUCCEEDED.value
    assert get_asset(db_path, PLATFORM, CONTENT_ID)["lifecycle_state"] == "DISCOVERED"

    # ARCHIVE job → failed_retryable → requeued → succeeded.
    archive = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.ARCHIVE.value,
        _fp("a"),
        policy_version="operations-policy-v1",
        canonical_id=CANONICAL_ID,
        pipeline_run_id=run["run_id"],
    )
    _lease_and_run(db_path, archive.job_id)
    attempt1 = begin_attempt(db_path, archive.job_id, worker_id="worker-1")
    finish_attempt(
        db_path,
        attempt1["attempt_id"],
        outcome="failed",
        retryable=True,
        error_class="TimeoutError",
        error_message="archive timeout",
    )
    assert get_job(db_path, archive.job_id)["state"] == JobState.FAILED_RETRYABLE.value
    requeue_retryable_job(db_path, archive.job_id)
    assert get_job(db_path, archive.job_id)["state"] == JobState.QUEUED.value

    _lease_and_run(db_path, archive.job_id)
    attempt2 = begin_attempt(db_path, archive.job_id, worker_id="worker-1")
    finish_attempt(db_path, attempt2["attempt_id"], outcome="succeeded")
    assert get_job(db_path, archive.job_id)["state"] == JobState.SUCCEEDED.value

    transition_asset_lifecycle(db_path, CANONICAL_ID, AssetLifecycleState.ARCHIVED.value)
    assert get_asset(db_path, PLATFORM, CONTENT_ID)["lifecycle_state"] == "ARCHIVED"
    complete_pipeline_run(db_path, run["run_id"], "SUCCEEDED")

    result = validate_operations_store(db_path)
    assert result.valid is True, result.violations
    assert result.counts["jobs"] == 2
    assert result.counts["job_attempts"] == 3
    assert result.counts["pipeline_runs"] == 1


# ----------------------------------------------------------------------
# 45. Duplicate discovery
# ----------------------------------------------------------------------


def test_duplicate_discovery(db_path):
    for _ in range(3):
        _register(db_path)
    _enqueue(db_path)
    _enqueue(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        n_assets = conn.execute("SELECT count(*) FROM assets").fetchone()[0]
        n_jobs = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert n_assets == 1
    assert n_jobs == 1


# ----------------------------------------------------------------------
# 46. Changed input generation (new upstream version)
# ----------------------------------------------------------------------


def test_changed_input_generation(db_path):
    _register(db_path)
    job_a = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.MEDIA_PROCESS.value,
        _fp("a"),
        policy_version="operations-policy-v1",
        canonical_id=CANONICAL_ID,
    )
    _lease_and_run(db_path, job_a.job_id)
    _succeed(db_path, job_a.job_id)

    job_b = enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        JobStage.MEDIA_PROCESS.value,
        _fp("b"),
        policy_version="operations-policy-v1",
        canonical_id=CANONICAL_ID,
    )
    assert job_b.job_id != job_a.job_id
    assert job_b.outcome == "enqueued"
    # The SUCCEEDED generation A never masks generation B.
    assert get_job(db_path, job_a.job_id)["state"] == JobState.SUCCEEDED.value
    assert get_job(db_path, job_b.job_id)["state"] == JobState.QUEUED.value


# ----------------------------------------------------------------------
# Determinism helpers (unit level)
# ----------------------------------------------------------------------


def test_legal_transition_tables_consistent():
    assert is_legal_job_transition(JobState.QUEUED.value, JobState.LEASED.value)
    assert is_legal_job_transition(JobState.LEASED.value, JobState.RUNNING.value)
    assert is_legal_job_transition(JobState.RUNNING.value, JobState.SUCCEEDED.value)
    assert is_legal_job_transition(JobState.RUNNING.value, JobState.FAILED_RETRYABLE.value)
    assert is_legal_job_transition(JobState.RUNNING.value, JobState.FAILED_TERMINAL.value)
    assert is_legal_job_transition(JobState.FAILED_RETRYABLE.value, JobState.QUEUED.value)
    assert is_legal_job_transition(JobState.QUEUED.value, JobState.CANCELLED.value)
    assert is_legal_job_transition(JobState.FAILED_RETRYABLE.value, JobState.CANCELLED.value)
    assert not is_legal_job_transition(JobState.SUCCEEDED.value, JobState.QUEUED.value)
    assert not is_legal_job_transition(JobState.CANCELLED.value, JobState.QUEUED.value)


def test_no_failed_pc_offline_semantics(db_path):
    # PC-offline is a normal QUEUED wait, never a special state.
    _register(db_path)
    res = _enqueue(db_path)
    assert get_job(db_path, res.job_id)["state"] == JobState.QUEUED.value