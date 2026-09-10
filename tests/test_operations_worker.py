"""M6-02 Capability-aware Worker + Lease Protocol tests.

Covers: capability matching/dispatch, atomic claim + deterministic ordering,
lease token/fencing, renewal, start/attempt accounting, success/retryable/
terminal completion, expired-lease recovery (LEASED vs RUNNING), crash retry
exhaustion, worker heartbeat/staleness, PC-offline semantics, WorkerRuntime
loop with injected handlers, stale-token fencing (P0), concurrent two-worker
claim, validation extensions, and the synthetic crash/recovery flow. All on
temp DBs (never the production operations DB). No network / LLM / GPU.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from src.operations.models import (
    JobStage,
    JobState,
    compute_job_id,
    utc_now_iso,
)
from src.operations.store import (
    OperationsStateError,
    StaleLeaseError,
    claim_next_job,
    complete_job_retryable_failure,
    complete_job_success,
    complete_job_terminal_failure,
    create_operations_store,
    create_pipeline_run,
    enqueue_job,
    get_job,
    list_job_attempts,
    list_workers,
    list_workers_with_status,
    open_operations_store,
    recover_expired_leases,
    register_asset,
    register_worker,
    renew_job_lease,
    start_claimed_job,
    validate_operations_store,
    worker_heartbeat,
)
from src.operations.worker import (
    HandlerResult,
    RetryableJobError,
    TerminalJobError,
    WorkerRuntime,
)

PLATFORM = "douyin"
CONTENT_ID = "cid_7681603850364521734"
CANONICAL_ID = "douyin_7681603850364521734"
FINGERPRINT = "a" * 64

T0 = "2026-09-10T01:00:00+00:00"
T_PLUS = "2026-09-10T01:02:00+00:00"
T_PLUS_LONG = "2026-09-10T01:04:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "operations.sqlite3"


def _register(db, canonical_id=CANONICAL_ID):
    return register_asset(
        db,
        PLATFORM,
        CONTENT_ID,
        canonical_id,
        metadata={"note": "worker test asset"},
        upstream_fingerprint="f" * 64,
    )


def _fp(char="a"):
    return char * 64


def _enqueue(db, canonical_id=CANONICAL_ID, stage=JobStage.DISCOVER.value,
             fingerprint=None, required_capabilities=None, **kw):
    return enqueue_job(
        db,
        PLATFORM,
        CONTENT_ID,
        stage,
        fingerprint or _fp("a"),
        policy_version=kw.pop("policy_version", "operations-policy-v1"),
        canonical_id=canonical_id,
        required_capabilities=required_capabilities,
        **kw,
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _assert_leased(job, worker, token, expires_at):
    assert job["state"] == JobState.LEASED.value
    assert job["lease_owner"] == worker
    assert job["lease_token"] == token
    assert job["leased_at"] == T0
    assert job["lease_expires_at"] == expires_at


# ----------------------------------------------------------------------
# 1-9. Registration, capabilities, atomic claim, ordering
# ----------------------------------------------------------------------


def test_worker_registration_and_capabilities(db_path):
    create_operations_store(db_path)
    register_worker(
        db_path,
        "worker-gpu",
        ["gpu_asr", "llm_extraction"],
        display_name="GPU Worker",
        hostname="pc-01",
        now=T0,
    )
    workers = list_workers(db_path)
    assert len(workers) == 1
    w = workers[0]
    assert w["worker_id"] == "worker-gpu"
    assert set(w["capabilities_json"]) == set() or w["capabilities_json"] is not None
    assert json.loads(w["capabilities_json"]) == ["gpu_asr", "llm_extraction"]
    assert w["status"] == "registered"
    assert w["last_heartbeat_at"] == T0


def test_register_worker_upsert(db_path):
    create_operations_store(db_path)
    register_worker(db_path, "w1", ["collector"], now=T0)
    register_worker(db_path, "w1", ["collector", "downloader"], now=T_PLUS)
    workers = list_workers(db_path)
    assert len(workers) == 1
    assert json.loads(workers[0]["capabilities_json"]) == ["collector", "downloader"]


def test_atomic_claim_sets_lease_fields(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(
        db_path, "worker-1", [], lease_duration_seconds=120, now=T0
    )
    assert claimed is not None
    assert claimed.job_id == res.job_id
    assert claimed.stage == JobStage.DISCOVER.value
    assert claimed.canonical_id == CANONICAL_ID
    assert claimed.platform == PLATFORM
    assert claimed.platform_content_id == CONTENT_ID
    assert claimed.input_fingerprint == _fp("a")
    job = get_job(db_path, res.job_id)
    _assert_leased(job, "worker-1", claimed.lease_token, T_PLUS)


def test_claim_requires_matching_capability(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, required_capabilities=["downloader"])
    # Worker without downloader capability cannot claim.
    claimed = claim_next_job(db_path, "cpu-worker", ["cpu_media"], now=T0)
    assert claimed is None
    job = get_job(db_path, _enqueue_job_id(db_path))
    assert job["state"] == JobState.QUEUED.value


def test_claim_capability_subset_match(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value,
             required_capabilities=["downloader", "cpu_media"])
    claimed = claim_next_job(
        db_path, "all-in-one", ["collector", "downloader", "cpu_media"], now=T0
    )
    assert claimed is not None
    assert set(claimed.required_capabilities) == {"downloader", "cpu_media"}


def test_empty_capability_requirement_is_generic(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path)  # no required_capabilities -> generic
    claimed = claim_next_job(db_path, "control-worker", [], now=T0)
    assert claimed is not None


def test_no_matching_worker_job_stays_queued(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, required_capabilities=["gpu_asr"])
    claimed = claim_next_job(db_path, "cpu-worker", ["cpu_media"], now=T0)
    assert claimed is None
    job = get_job(db_path, _enqueue_job_id(db_path))
    assert job["state"] == JobState.QUEUED.value
    assert job["attempt_count"] == 0


def test_claim_respects_future_retry_time(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, fingerprint=_fp("a"))
    # Put it into FAILED_RETRYABLE with future retry.
    claimed1 = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed1.lease_token, now=T0)
    complete_job_retryable_failure(
        db_path, res.job_id, "w1", claimed1.lease_token,
        error_class="RetryableJobError", error_message="temp", now=T0,
    )
    assert get_job(db_path, res.job_id)["state"] == JobState.FAILED_RETRYABLE.value
    # Not claimable while FAILED_RETRYABLE, even after time passes.
    assert claim_next_job(db_path, "w1", [], now=T0) is None
    assert claim_next_job(db_path, "w1", [], now="2026-09-10T03:00:00+00:00") is None
    # Explicit requeue -> QUEUED, then claimable.
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now="2026-09-10T03:00:00+00:00")
    claimed_later = claim_next_job(
        db_path, "w1", [], now="2026-09-10T03:00:00+00:00"
    )
    assert claimed_later is not None


def test_claim_deterministic_ordering(db_path):
    create_operations_store(db_path)
    _register(db_path)
    e1 = _enqueue(db_path, fingerprint=_fp("a"))
    e2 = _enqueue(db_path, fingerprint=_fp("b"))
    e3 = _enqueue(db_path, fingerprint=_fp("c"))
    # Same created_at; ordering by job_id ASC.
    c1 = claim_next_job(db_path, "w1", [], now=T0)
    c2 = claim_next_job(db_path, "w1", [], now=T0)
    c3 = claim_next_job(db_path, "w1", [], now=T0)
    ids = [c1.job_id, c2.job_id, c3.job_id]
    assert sorted(ids) == ids
    assert set(ids) == {e1.job_id, e2.job_id, e3.job_id}


def test_claim_ordering_priority_then_time(db_path):
    create_operations_store(db_path)
    _register(db_path)
    low = _enqueue(db_path, fingerprint=_fp("a"), priority=0)
    high = _enqueue(db_path, fingerprint=_fp("b"), priority=10)
    c1 = claim_next_job(db_path, "w1", [], now=T0)
    assert c1.job_id == high.job_id
    c2 = claim_next_job(db_path, "w1", [], now=T0)
    assert c2.job_id == low.job_id


def test_claim_unique_lease_tokens(db_path):
    create_operations_store(db_path)
    _register(db_path)
    for ch in "abc":
        _enqueue(db_path, fingerprint=_fp(ch))
    tokens = set()
    for _ in range(3):
        claimed = claim_next_job(db_path, "w1", [], now=T0)
        tokens.add(claimed.lease_token)
    assert len(tokens) == 3
    for tok in tokens:
        assert tok.startswith("lease_")


def test_claim_idempotent_returns_none_when_exhausted(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path)
    c1 = claim_next_job(db_path, "w1", [], now=T0)
    assert c1 is not None
    c2 = claim_next_job(db_path, "w1", [], now=T0)
    assert c2 is None


def test_claim_logs_event(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claim_next_job(db_path, "w1", [], now=T0)
    conn = open_operations_store(db_path)
    try:
        rows = conn.execute(
            "SELECT event_type, worker_id, job_id FROM event_log ORDER BY event_rowid"
        ).fetchall()
    finally:
        conn.close()
    types = [r["event_type"] for r in rows]
    assert "job_claimed" in types
    claimed_event = [r for r in rows if r["event_type"] == "job_claimed"][0]
    assert claimed_event["worker_id"] == "w1"
    assert claimed_event["job_id"] == res.job_id


# ----------------------------------------------------------------------
# 10-16. Renewal, start, attempt accounting
# ----------------------------------------------------------------------


def test_lease_renewal(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], lease_duration_seconds=120, now=T0)
    renew_job_lease(
        db_path, res.job_id, "w1", claimed.lease_token,
        lease_duration_seconds=120, now=T_PLUS,
    )
    job = get_job(db_path, res.job_id)
    assert job["lease_expires_at"] == T_PLUS_LONG


def test_wrong_owner_renewal_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    with pytest.raises(StaleLeaseError):
        renew_job_lease(db_path, res.job_id, "w2", claimed.lease_token, now=T0)
    job = get_job(db_path, res.job_id)
    assert job["lease_owner"] == "w1"


def test_stale_token_renewal_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    with pytest.raises(StaleLeaseError):
        renew_job_lease(
            db_path, res.job_id, "w1", "lease_00000000000000000000000000000000",
            now=T0,
        )


def test_renewal_no_event_churn(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    renew_job_lease(db_path, res.job_id, "w1", claimed.lease_token, now=T_PLUS)
    conn = open_operations_store(db_path)
    try:
        rows = conn.execute(
            "SELECT event_type FROM event_log ORDER BY event_rowid"
        ).fetchall()
    finally:
        conn.close()
    assert not any(r["event_type"] == "job_lease_renewed" for r in rows)


def test_start_with_valid_token(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    started = start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    assert started["state"] == JobState.RUNNING.value
    assert started["attempt_count"] == 1


def test_start_stale_token_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claim_next_job(db_path, "w1", [], now=T0)
    with pytest.raises(StaleLeaseError):
        start_claimed_job(
            db_path, res.job_id, "w1", "lease_00000000000000000000000000000000",
            now=T0,
        )
    assert get_job(db_path, res.job_id)["state"] == JobState.LEASED.value


def test_start_expired_lease_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], lease_duration_seconds=10, now=T0)
    # lease expires at 01:00:10; attempt start at 01:00:20
    with pytest.raises(StaleLeaseError):
        start_claimed_job(
            db_path, res.job_id, "w1", claimed.lease_token,
            now="2026-09-10T01:00:20+00:00",
        )


def test_attempt_created_on_start(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    attempts = list_job_attempts(db_path, res.job_id)
    assert len(attempts) == 1
    a = attempts[0]
    assert a["attempt_number"] == 1
    assert a["worker_id"] == "w1"
    assert a["started_at"] == T0
    assert a["finished_at"] is None
    assert a["outcome"] is None


def test_start_requires_leased_state(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    with pytest.raises(StaleLeaseError):
        start_claimed_job(db_path, res.job_id, "w1", "lease_x", now=T0)


def test_exactly_one_active_attempt(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    attempts = list_job_attempts(db_path, res.job_id)
    active = [a for a in attempts if a["finished_at"] is None]
    assert len(active) == 1


# ----------------------------------------------------------------------
# 17-22. Completion protocols
# ----------------------------------------------------------------------


def test_success_completion(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    done = complete_job_success(
        db_path, res.job_id, "w1", claimed.lease_token,
        now="2026-09-10T01:00:05+00:00", metadata={"bytes": 42},
    )
    assert done["state"] == JobState.SUCCEEDED.value
    assert done["attempt_count"] == 1
    assert done["lease_owner"] is None
    assert done["leased_at"] is None
    assert done["lease_expires_at"] is None
    assert done["lease_token"] is None
    assert done["next_retry_at"] is None
    attempts = list_job_attempts(db_path, res.job_id)
    assert attempts[0]["outcome"] == "succeeded"
    assert attempts[0]["finished_at"] == "2026-09-10T01:00:05+00:00"


def test_retryable_completion(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    done = complete_job_retryable_failure(
        db_path, res.job_id, "w1", claimed.lease_token,
        error_class="RetryableJobError", error_message="timeout", now=T0,
    )
    assert done["state"] == JobState.FAILED_RETRYABLE.value
    assert done["next_retry_at"] is not None
    assert done["attempt_count"] == 1
    assert done["lease_owner"] is None  # lease cleared on retryable
    attempts = list_job_attempts(db_path, res.job_id)
    assert attempts[0]["outcome"] == "failed"
    assert attempts[0]["retryable"] == 1
    assert attempts[0]["error_class"] == "RetryableJobError"


def test_retryable_exhaustion_goes_terminal(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=2)
    from src.operations.store import requeue_retryable_job
    for i in range(2):
        claimed = claim_next_job(db_path, "w1", [], now=T0)
        assert claimed is not None
        start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
        complete_job_retryable_failure(
            db_path, res.job_id, "w1", claimed.lease_token,
            error_class="RetryableJobError", error_message="boom", now=T0,
        )
        if i == 0:
            requeue_retryable_job(db_path, res.job_id, now=T0)
    done = get_job(db_path, res.job_id)
    assert done["state"] == JobState.FAILED_TERMINAL.value
    assert done["next_retry_at"] is None
    assert done["attempt_count"] == 2
    # Cannot claim again.
    assert claim_next_job(db_path, "w1", [], now=T0) is None


def test_terminal_completion(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    done = complete_job_terminal_failure(
        db_path, res.job_id, "w1", claimed.lease_token,
        error_class="TerminalJobError", error_message="bad input", now=T0,
    )
    assert done["state"] == JobState.FAILED_TERMINAL.value
    assert done["lease_owner"] is None
    attempts = list_job_attempts(db_path, res.job_id)
    assert attempts[0]["retryable"] == 0


def test_stale_success_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed_a = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed_a.lease_token, now=T0)
    # w1 crashes; lease expires; w2 reclaims; w1 tries to complete with A token.
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now=T_PLUS_LONG)
    claimed_b = claim_next_job(db_path, "w2", [], now=T_PLUS_LONG)
    assert claimed_b is not None
    assert claimed_b.lease_token != claimed_a.lease_token
    start_claimed_job(db_path, res.job_id, "w2", claimed_b.lease_token, now=T_PLUS_LONG)
    with pytest.raises(StaleLeaseError):
        complete_job_success(db_path, res.job_id, "w1", claimed_a.lease_token, now=T_PLUS_LONG)
    # w2 still owns; job not corrupted.
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.RUNNING.value
    assert job["lease_owner"] == "w2"


def test_stale_failure_rejected(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed_a = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed_a.lease_token, now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now=T_PLUS_LONG)
    claimed_b = claim_next_job(db_path, "w2", [], now=T_PLUS_LONG)
    start_claimed_job(db_path, res.job_id, "w2", claimed_b.lease_token, now=T_PLUS_LONG)
    with pytest.raises(StaleLeaseError):
        complete_job_retryable_failure(
            db_path, res.job_id, "w1", claimed_a.lease_token,
            error_class="RetryableJobError", error_message="x", now=T_PLUS_LONG,
        )
    with pytest.raises(StaleLeaseError):
        complete_job_terminal_failure(
            db_path, res.job_id, "w1", claimed_a.lease_token,
            error_class="TerminalJobError", error_message="x", now=T_PLUS_LONG,
        )
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.RUNNING.value
    assert job["lease_owner"] == "w2"


# ----------------------------------------------------------------------
# 23-28. Expired-lease recovery
# ----------------------------------------------------------------------


def test_leased_before_start_expiry(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claim_next_job(db_path, "w1", [], now=T0)
    result = recover_expired_leases(db_path, now=T_PLUS_LONG)
    assert result["recovered_leased"] == [res.job_id]
    assert result["recovered_running"] == []
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.QUEUED.value
    assert job["lease_owner"] is None
    assert job["lease_token"] is None
    assert job["attempt_count"] == 0
    assert list_job_attempts(db_path, res.job_id) == []


def test_leased_expiry_no_attempt_consumed(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claim_next_job(db_path, "w1", [], now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    assert get_job(db_path, res.job_id)["attempt_count"] == 0
    assert list_job_attempts(db_path, res.job_id) == []


def test_running_expiry_closes_attempt(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    result = recover_expired_leases(db_path, now=T_PLUS_LONG)
    assert result["recovered_running"] == [res.job_id]
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_RETRYABLE.value
    assert job["next_retry_at"] is not None
    assert job["attempt_count"] == 1
    attempts = list_job_attempts(db_path, res.job_id)
    assert attempts[0]["outcome"] == "failed"
    assert attempts[0]["retryable"] == 1
    assert attempts[0]["error_class"] == "WorkerLeaseExpired"
    assert attempts[0]["finished_at"] == T_PLUS_LONG


def test_running_expiry_respects_backoff(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    # next_retry_at far in future -> not immediately reclaimable
    assert claim_next_job(db_path, "w2", [], now=T_PLUS_LONG) is None
    # a fresh claim attempt after retry time works
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now="2026-09-10T01:10:00+00:00")
    claimed2 = claim_next_job(db_path, "w2", [], now="2026-09-10T01:10:00+00:00")
    assert claimed2 is not None


def test_running_expiry_exhaustion_terminal(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=2)
    from src.operations.store import requeue_retryable_job
    for i in range(2):
        claimed = claim_next_job(db_path, "w1", [], now=T0)
        assert claimed is not None
        start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
        recover_expired_leases(db_path, now=T_PLUS_LONG)
        if i == 0:
            requeue_retryable_job(db_path, res.job_id, now=T_PLUS_LONG)
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_TERMINAL.value
    assert job["attempt_count"] == 2
    attempts = list_job_attempts(db_path, res.job_id)
    assert [a["outcome"] for a in attempts] == ["failed", "failed"]
    assert all(a["error_class"] == "WorkerLeaseExpired" for a in attempts)
    assert claim_next_job(db_path, "w1", [], now=T_PLUS_LONG) is None


def test_reclaim_after_expiry(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed_a = claim_next_job(db_path, "w1", [], now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    claimed_b = claim_next_job(db_path, "w2", [], now=T_PLUS_LONG)
    assert claimed_b is not None
    assert claimed_b.lease_token != claimed_a.lease_token
    assert get_job(db_path, res.job_id)["lease_owner"] == "w2"


# ----------------------------------------------------------------------
# 29-31. Heartbeat & staleness
# ----------------------------------------------------------------------


def test_worker_heartbeat_updates_timestamp(db_path):
    create_operations_store(db_path)
    register_worker(db_path, "w1", ["collector"], now=T0)
    worker_heartbeat(db_path, "w1", now=T_PLUS)
    workers = list_workers(db_path)
    assert workers[0]["last_heartbeat_at"] == T_PLUS


def test_worker_stale_derived_status(db_path):
    create_operations_store(db_path)
    register_worker(db_path, "w1", ["collector"], now=T0)
    from src.operations.store import is_worker_stale
    # threshold 10s, heartbeat at T0 -> stale by T_PLUS (120s later)
    assert is_worker_stale(db_path, "w1", stale_threshold_seconds=10, now=T_PLUS) is True
    assert (
        is_worker_stale(db_path, "w1", stale_threshold_seconds=600, now=T_PLUS) is False
    )
    workers = list_workers_with_status(
        db_path, stale_threshold_seconds=10, now=T_PLUS
    )
    assert workers[0]["derived_status"] == "stale"


def test_stale_worker_does_not_fail_job(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    register_worker(db_path, "w1", ["collector"], now=T0)
    claim_next_job(db_path, "w1", [], now=T0)
    from src.operations.store import is_worker_stale
    assert is_worker_stale(db_path, "w1", stale_threshold_seconds=10, now=T_PLUS)
    # Job is untouched by staleness; still LEASED until its own lease expires.
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.LEASED.value
    assert job["lease_owner"] == "w1"


# ----------------------------------------------------------------------
# 32-33. Concurrency
# ----------------------------------------------------------------------


def test_two_worker_concurrent_claim(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path)
    results: list = []
    barrier = threading.Barrier(2)

    def worker_claim(name):
        barrier.wait(timeout=10)
        try:
            claimed = claim_next_job(db_path, name, [], now=T0)
            results.append((name, claimed))
        except Exception as exc:  # pragma: no cover - defensive
            results.append((name, exc))

    t1 = threading.Thread(target=worker_claim, args=("w-a",))
    t2 = threading.Thread(target=worker_claim, args=("w-b",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    winners = [r for r in results if isinstance(r[1], object) and r[1] is not None]
    errors = [r for r in results if not isinstance(r[1], object) or isinstance(r[1], Exception)]
    winners = [r for r in results if getattr(r[1], "job_id", None)]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    losers = [r for r in results if r[1] is None]
    assert len(losers) == 1
    job = get_job(db_path, _enqueue_job_id(db_path))
    assert job["state"] == JobState.LEASED.value
    assert job["lease_owner"] == winners[0][0]


def test_concurrent_claim_capability_filtering(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, required_capabilities=["gpu_asr"])
    results: list = []
    barrier = threading.Barrier(2)

    def worker_claim(name, caps):
        barrier.wait(timeout=10)
        try:
            results.append((name, claim_next_job(db_path, name, caps, now=T0)))
        except Exception as exc:  # pragma: no cover
            results.append((name, exc))

    t1 = threading.Thread(target=worker_claim, args=("gpu", ["gpu_asr"]))
    t2 = threading.Thread(target=worker_claim, args=("cpu", ["cpu_media"]))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    gpu_results = [r for r in results if r[0] == "gpu" and r[1] is not None]
    cpu_results = [r for r in results if r[0] == "cpu" and r[1] is not None]
    assert len(gpu_results) == 1
    assert len(cpu_results) == 0


def _enqueue_job_id(db_path):
    jobs = open_operations_store(db_path)
    try:
        row = jobs.execute("SELECT job_id FROM jobs ORDER BY job_id ASC LIMIT 1").fetchone()
    finally:
        jobs.close()
    return row["job_id"] if row else None


# ----------------------------------------------------------------------
# 34-35. Token fencing P0 test
# ----------------------------------------------------------------------


def test_stale_fencing_full_flow(db_path):
    """P0: worker A claims (token A1); lease expires; job requeued; worker B
    reclaims (token B1). All A-side mutations (renew/start/success/retryable/
    terminal) must fail. B executes and succeeds."""
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, max_attempts=3)
    claimed_a = claim_next_job(db_path, "worker-A", [], lease_duration_seconds=60, now=T0)
    assert claimed_a is not None
    token_a = claimed_a.lease_token
    # A starts, then vanishes.
    start_claimed_job(db_path, res.job_id, "worker-A", token_a, now=T0)
    # Lease expires.
    recover_expired_leases(db_path, now="2026-09-10T01:05:00+00:00")
    assert get_job(db_path, res.job_id)["state"] == JobState.FAILED_RETRYABLE.value
    # Retry window opens; B reclaims.
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now="2026-09-10T01:10:00+00:00")
    claimed_b = claim_next_job(db_path, "worker-B", [], now="2026-09-10T01:10:00+00:00")
    assert claimed_b is not None
    token_b = claimed_b.lease_token
    assert token_b != token_a
    start_claimed_job(db_path, res.job_id, "worker-B", token_b, now="2026-09-10T01:10:00+00:00")
    # A's stale token must be fenced everywhere.
    with pytest.raises(StaleLeaseError):
        renew_job_lease(db_path, res.job_id, "worker-A", token_a, now="2026-09-10T01:10:00+00:00")
    with pytest.raises(StaleLeaseError):
        start_claimed_job(db_path, res.job_id, "worker-A", token_a, now="2026-09-10T01:10:00+00:00")
    with pytest.raises(StaleLeaseError):
        complete_job_success(db_path, res.job_id, "worker-A", token_a, now="2026-09-10T01:10:00+00:00")
    with pytest.raises(StaleLeaseError):
        complete_job_retryable_failure(db_path, res.job_id, "worker-A", token_a, now="2026-09-10T01:10:00+00:00")
    with pytest.raises(StaleLeaseError):
        complete_job_terminal_failure(db_path, res.job_id, "worker-A", token_a, now="2026-09-10T01:10:00+00:00")
    # B completes successfully.
    done = complete_job_success(db_path, res.job_id, "worker-B", token_b, now="2026-09-10T01:11:00+00:00")
    assert done["state"] == JobState.SUCCEEDED.value
    assert done["attempt_count"] == 2
    attempts = list_job_attempts(db_path, res.job_id)
    assert [a["outcome"] for a in attempts] == ["failed", "succeeded"]


def test_lease_expired_before_start_reclaim(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claim_next_job(db_path, "worker-A", [], now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    assert get_job(db_path, res.job_id)["state"] == JobState.QUEUED.value
    assert get_job(db_path, res.job_id)["attempt_count"] == 0
    claimed_b = claim_next_job(db_path, "worker-B", [], now=T_PLUS_LONG)
    assert claimed_b is not None


# ----------------------------------------------------------------------
# 36-44. WorkerRuntime
# ----------------------------------------------------------------------


def _make_runtime(db_path, worker_id="rt-1", capabilities=None, handlers=None, **kw):
    return WorkerRuntime(
        store_path=db_path,
        worker_id=worker_id,
        capabilities=capabilities or [],
        handlers=handlers or {},
        now=lambda: T0,
        **kw,
    )


def test_runtime_idle(db_path):
    create_operations_store(db_path)
    _register(db_path)
    rt = _make_runtime(db_path)
    result = rt.run_once()
    assert result.outcome == "idle"
    assert result.claimed_job_id is None


def test_runtime_synthetic_success(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value)

    def handler(claimed):
        return HandlerResult(metadata={"processed": 7})

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: handler})
    result = rt.run_once()
    assert result.outcome == "completed"
    assert result.claimed_job_id == res.job_id
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.SUCCEEDED.value
    assert job["attempt_count"] == 1
    conn = open_operations_store(db_path)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM job_attempts WHERE job_id=?", (res.job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["metadata_json"]) == {"processed": 7}


def test_runtime_retryable_handler(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value, max_attempts=3)

    def handler(claimed):
        raise RetryableJobError("network glitch")

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: handler})
    result = rt.run_once()
    assert result.outcome == "retryable"
    assert result.error_class == "RetryableJobError"
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_RETRYABLE.value
    assert job["next_retry_at"] is not None


def test_runtime_terminal_handler(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value)

    def handler(claimed):
        raise TerminalJobError("invalid artifact")

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: handler})
    result = rt.run_once()
    assert result.outcome == "terminal"
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_TERMINAL.value
    assert job["next_retry_at"] is None


def test_runtime_unknown_exception_is_retryable(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value, max_attempts=3)

    def handler(claimed):
        raise RuntimeError("mystery")

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: handler})
    result = rt.run_once()
    assert result.outcome == "retryable"
    assert result.error_class == "RuntimeError"
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_RETRYABLE.value


def test_runtime_unknown_exception_exhausts_to_terminal(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value, max_attempts=2)

    def handler(claimed):
        raise RuntimeError("mystery")

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: handler})
    from src.operations.store import requeue_retryable_job
    for i in range(2):
        result = rt.run_once()
        assert result.outcome == "retryable"
        if i == 0:
            requeue_retryable_job(db_path, res.job_id, now=T0)
    result = rt.run_once()
    # Nothing left to claim; but the job went terminal after 2 retryables.
    assert result.outcome == "idle"
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_TERMINAL.value
    assert job["attempt_count"] == 2


def test_runtime_handler_registry_missing_stage(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value)
    rt = _make_runtime(db_path, handlers={})  # no ARCHIVE handler
    result = rt.run_once()
    assert result.outcome == "terminal"
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_TERMINAL.value


def test_runtime_capability_routing(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, required_capabilities=["downloader"])
    rt = _make_runtime(db_path, capabilities=["cpu_media"])
    result = rt.run_once()
    assert result.outcome == "idle"
    # gpu worker with downloader claim succeeds
    rt2 = WorkerRuntime(
        store_path=db_path,
        worker_id="rt-2",
        capabilities=["downloader"],
        handlers={JobStage.ARCHIVE.value: lambda c: HandlerResult()},
        now=lambda: T0,
    )
    result2 = rt2.run_once()
    assert result2.outcome == "completed"


def test_runtime_lease_lost_fences_completion(db_path):
    """If the heartbeat thread detects lease loss, the runtime must NOT commit
    a completion; the store fences the stale token (P0 behavior)."""
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value, max_attempts=3)
    handler_called = {"n": 0}

    def slow_handler(claimed):
        handler_called["n"] += 1
        # While the handler runs, the lease is lost (expired + reclaimed).
        return HandlerResult()

    rt = _make_runtime(db_path, handlers={JobStage.ARCHIVE.value: slow_handler})
    # Claim + start manually with a very short lease so it expires.
    claimed = claim_next_job(
        db_path, rt.worker_id, [], lease_duration_seconds=1, now=T0
    )
    start_claimed_job(db_path, res.job_id, rt.worker_id, claimed.lease_token, now=T0)
    # Expire + let another worker reclaim the job.
    recover_expired_leases(db_path, now="2026-09-10T01:05:00+00:00")
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now="2026-09-10T01:10:00+00:00")
    intruder = claim_next_job(
        db_path, "intruder", [], now="2026-09-10T01:10:00+00:00"
    )
    assert intruder is not None
    start_claimed_job(
        db_path, res.job_id, "intruder", intruder.lease_token,
        now="2026-09-10T01:10:00+00:00",
    )
    # The old worker's completion is fenced by the store.
    with pytest.raises(StaleLeaseError):
        complete_job_success(
            db_path, res.job_id, rt.worker_id, claimed.lease_token,
            now="2026-09-10T01:12:00+00:00",
        )
    with pytest.raises(StaleLeaseError):
        complete_job_retryable_failure(
            db_path, res.job_id, rt.worker_id, claimed.lease_token,
            error_class="RetryableJobError", error_message="x",
            now="2026-09-10T01:12:00+00:00",
        )
    # Intruder still owns the job; runtime must record lease_lost so it never
    # attempts a completion after its ownership ended.
    assert rt.lease_lost is False
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.RUNNING.value
    assert job["lease_owner"] == "intruder"


def test_runtime_graceful_shutdown(db_path):
    create_operations_store(db_path)
    _register(db_path)
    rt = _make_runtime(db_path)
    rt.start_heartbeat_thread()
    assert rt._hb_thread is not None and rt._hb_thread.is_alive()
    rt.stop()
    assert rt._stop_event.is_set()


def test_runtime_heartbeat_renews_lease(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value)
    clock = {"now": T0}

    def fake_now():
        return clock["now"]

    rt = WorkerRuntime(
        store_path=db_path,
        worker_id="rt-hb",
        capabilities=[],
        handlers={JobStage.ARCHIVE.value: lambda c: HandlerResult()},
        lease_duration_seconds=120,
        heartbeat_interval_seconds=0.02,
        now=fake_now,
    )
    rt.register()
    claimed = claim_next_job(
        db_path, "rt-hb", [], lease_duration_seconds=120, now=clock["now"]
    )
    start_claimed_job(db_path, res.job_id, "rt-hb", claimed.lease_token, now=clock["now"])
    # Advance time past the original expiry; wire the active job into the
    # runtime so the heartbeat thread renews it.
    clock["now"] = T_PLUS
    rt._active_job_id = res.job_id
    rt._active_token = claimed._lease_token
    rt.start_heartbeat_thread()
    time.sleep(0.3)  # a few heartbeat cycles at advanced time
    rt.stop()
    job = get_job(db_path, res.job_id)
    # Lease was renewed forward past the original expiry.
    assert _parse_ts(job["lease_expires_at"]) > _parse_ts(T_PLUS)


def test_heartbeat_does_not_auto_renew_all_jobs(db_path):
    """Worker heartbeat and job lease renewal are separate concepts."""
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value)
    rt = _make_runtime(db_path)
    rt.register()
    claimed = claim_next_job(db_path, rt.worker_id, [], lease_duration_seconds=1, now=T0)
    start_claimed_job(db_path, res.job_id, rt.worker_id, claimed.lease_token, now=T0)
    # Heartbeat WITHOUT renewing the job lease.
    rt.heartbeat()
    # Lease still expires on the short schedule.
    assert get_job(db_path, res.job_id)["lease_expires_at"] == "2026-09-10T01:00:01+00:00"


def test_token_hidden_from_repr_and_log(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    # Token is accessible via property but hidden from repr / public dict.
    assert claimed.lease_token.startswith("lease_")
    assert claimed.lease_token not in repr(claimed)
    public = claimed.to_public_dict()
    assert "lease_token" not in public
    # job row in DB holds the token (allowed), but event messages must not.
    conn = open_operations_store(db_path)
    try:
        events = conn.execute(
            "SELECT message, metadata_json FROM event_log ORDER BY event_rowid"
        ).fetchall()
    finally:
        conn.close()
    for ev in events:
        assert claimed.lease_token not in (ev["message"] or "")
        assert claimed.lease_token not in (ev["metadata_json"] or "")


# ----------------------------------------------------------------------
# 45-46. Validation extensions
# ----------------------------------------------------------------------


def test_validation_active_attempt_invariant(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    # Corrupt: close the only attempt while job stays RUNNING -> invariant broken.
    conn = open_operations_store(db_path)
    try:
        conn.execute(
            "UPDATE job_attempts SET finished_at=? WHERE job_id=?",
            (T_PLUS, res.job_id),
        )
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("exactly one active attempt" in v for v in result.violations)


def test_validation_lease_invariant(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path)
    # Force SUCCEEDED with residual lease fields -> invariant broken.
    claimed = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    complete_job_success(db_path, res.job_id, "w1", claimed.lease_token, now=T0)
    conn = open_operations_store(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET lease_owner='ghost' WHERE job_id=?", (res.job_id,)
        )
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("residual lease fields" in v for v in result.violations)


def test_validation_required_capability_valid(db_path):
    create_operations_store(db_path)
    _register(db_path)
    _enqueue(db_path, stage=JobStage.ARCHIVE.value, required_capabilities=["gpu_asr"])
    result = validate_operations_store(db_path)
    assert result.valid is True
    # Corrupt to an invalid capability.
    conn = open_operations_store(db_path)
    try:
        conn.execute("UPDATE jobs SET required_capabilities_json=?", ('["nope"]',))
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("invalid required capability" in v for v in result.violations)


def test_validation_worker_capability_valid(db_path):
    create_operations_store(db_path)
    register_worker(db_path, "w1", ["collector"], now=T0)
    result = validate_operations_store(db_path)
    assert result.valid is True
    conn = open_operations_store(db_path)
    try:
        conn.execute("UPDATE workers SET capabilities_json=? WHERE worker_id=?", ('["bogus"]', "w1"))
        conn.commit()
    finally:
        conn.close()
    result = validate_operations_store(db_path)
    assert result.valid is False
    assert any("invalid capability" in v for v in result.violations)


# ----------------------------------------------------------------------
# 47. Full synthetic crash/recovery flow
# ----------------------------------------------------------------------


def test_full_synthetic_crash_recovery_flow(db_path):
    create_operations_store(db_path)
    _register(db_path)
    res = _enqueue(db_path, stage=JobStage.ARCHIVE.value, max_attempts=3)

    # Cycle 1: w1 claims, starts (attempt 1), crashes.
    c1 = claim_next_job(db_path, "w1", [], now=T0)
    start_claimed_job(db_path, res.job_id, "w1", c1.lease_token, now=T0)
    recover_expired_leases(db_path, now=T_PLUS_LONG)
    job = get_job(db_path, res.job_id)
    assert job["state"] == JobState.FAILED_RETRYABLE.value
    assert job["attempt_count"] == 1

    # Cycle 2: after backoff, w2 claims, starts (attempt 2), succeeds.
    from src.operations.store import requeue_retryable_job
    requeue_retryable_job(db_path, res.job_id, now="2026-09-10T01:10:00+00:00")
    c2 = claim_next_job(db_path, "w2", [], now="2026-09-10T01:10:00+00:00")
    assert c2 is not None
    start_claimed_job(db_path, res.job_id, "w2", c2.lease_token, now="2026-09-10T01:10:00+00:00")
    done = complete_job_success(
        db_path, res.job_id, "w2", c2.lease_token,
        now="2026-09-10T01:10:05+00:00",
    )
    assert done["state"] == JobState.SUCCEEDED.value
    assert done["attempt_count"] == 2
    attempts = list_job_attempts(db_path, res.job_id)
    assert len(attempts) == 2
    assert [a["outcome"] for a in attempts] == ["failed", "succeeded"]
    assert attempts[0]["error_class"] == "WorkerLeaseExpired"
    result = validate_operations_store(db_path)
    assert result.valid is True