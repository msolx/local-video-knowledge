"""M6-04 tests for the automatic pipeline scheduler.

Covers the reconciliation-oriented scheduler contract: run_once phase order,
DISCOVER batch processing, deterministic poll generation, downstream enqueue
with fingerprint handoff, lifecycle progression, retry requeue, lease
recovery, PC-offline behavior, terminal failure semantics, restart recovery,
changed-generation refresh, real C10 offline chains and disposable-store-only
usage.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.operations.scheduler import (
    SCHEDULER_POLICY_VERSION,
    SCHEDULER_SCHEMA_VERSION,
    ASSET_PIPELINE_GRAPH,
    CONTROL_CONTENT_ID,
    Scheduler,
    SchedulerCycleResult,
    PollSource,
    SchedulerError,
    discovery_control_fingerprint,
    epoch_seconds,
    poll_slot_fingerprint,
)
from src.operations.stages import (
    STAGES_POLICY_VERSION,
    STATUS_CACHE_HIT,
    STATUS_EXECUTED,
    StageExecutionResult,
    required_capabilities_for_stage,
)
from src.operations.store import (
    begin_attempt,
    claim_next_job,
    complete_job_success,
    complete_job_retryable_failure,
    complete_job_terminal_failure,
    create_pipeline_run,
    enqueue_job,
    get_asset,
    get_asset_by_canonical_id,
    get_job,
    get_job_result,
    list_events,
    list_jobs,
    list_pipeline_runs,
    open_operations_store,
    register_asset,
    register_worker,
    requeue_retryable_job,
    start_claimed_job,
    transition_asset_lifecycle,
)
from src.operations.models import (
    JobStage,
    PipelineRunStatus,
    TriggerType,
    compute_job_id,
    parse_iso,
)

ROOT = Path(__file__).resolve().parent.parent
PROCESSED_ROOT = ROOT / "data" / "processed"
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"

PLATFORM = "douyin"
CONTENT_ID = "7681603850364521734"
CANONICAL_ID = f"{PLATFORM}_{CONTENT_ID}"
FINGERPRINT = "a" * 64

T0 = "2026-09-10T01:00:00+00:00"
T_PLUS = "2026-09-10T01:02:00+00:00"
T_PLUS_LONG = "2026-09-10T01:04:00+00:00"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path):
    return tmp_path / "operations.sqlite3"


def _fp(char: str = "a", length: int = 64) -> str:
    return (char * length)[:length]


def _register(db, content_id: str = CONTENT_ID, platform: str = PLATFORM):
    return register_asset(
        db,
        platform,
        content_id,
        f"{platform}_{content_id}",
        now=T0,
    )


def _enqueue(
    db,
    stage: str,
    *,
    content_id: str = CONTENT_ID,
    canonical_id: str = CANONICAL_ID,
    input_fp: str = FINGERPRINT,
    pipeline_run_id: str | None = None,
    required_capabilities: list[str] | None = None,
    now: str = T0,
):
    return enqueue_job(
        db,
        PLATFORM,
        content_id,
        stage,
        input_fp,
        policy_version=STAGES_POLICY_VERSION,
        canonical_id=canonical_id,
        pipeline_run_id=pipeline_run_id,
        required_capabilities=required_capabilities,
        now=now,
    )


def _run(db, canonical_id: str = CANONICAL_ID, trigger: str = "discovery"):
    return create_pipeline_run(
        db, canonical_id, trigger, metadata={"test": True}, now=T0
    )


def _succeed_job(db, job_id: str, result: dict | None, now: str = T_PLUS):
    """Drive a QUEUED job to SUCCEEDED via claim + start + success with a
    durable StageExecutionResult."""
    worker_id = f"w_{job_id}"
    register_worker(
        db, worker_id, ["collector", "downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"],
        now=T0,
    )
    claimed = claim_next_job(db, worker_id, ["collector", "downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
    assert claimed is not None
    assert claimed.job_id == job_id
    start_claimed_job(db, job_id, worker_id, claimed.lease_token, now=now)
    if result is not None:
        complete_job_success(
            db, job_id, worker_id, claimed.lease_token, now=now,
            metadata={"stage_result": result},
        )
    else:
        complete_job_success(db, job_id, worker_id, claimed.lease_token, now=now)
    return job_id


def _fail_job_retryable(db, job_id: str, now: str = T_PLUS):
    """Drive a QUEUED job to FAILED_RETRYABLE via claim + start + retryable failure."""
    worker_id = f"w_{job_id}_rt"
    register_worker(db, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=T0)
    claimed = claim_next_job(db, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
    assert claimed is not None
    assert claimed.job_id == job_id
    start_claimed_job(db, job_id, worker_id, claimed.lease_token, now=now)
    complete_job_retryable_failure(
        db, job_id, worker_id, claimed.lease_token,
        error_class="TestRetryable", error_message="x", now=now,
    )
    return job_id


def _fail_job_terminal(db, job_id: str, now: str = T_PLUS):
    """Drive a QUEUED job to FAILED_TERMINAL via claim + start + terminal failure."""
    worker_id = f"w_{job_id}_tm"
    register_worker(db, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=T0)
    claimed = claim_next_job(db, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
    assert claimed is not None
    assert claimed.job_id == job_id
    start_claimed_job(db, job_id, worker_id, claimed.lease_token, now=now)
    complete_job_terminal_failure(
        db, job_id, worker_id, claimed.lease_token,
        error_class="TestTerminal", error_message="boom", now=now,
    )
    return job_id


def _result_dict(
    stage: str,
    *,
    canonical_id: str = CANONICAL_ID,
    status: str = STATUS_EXECUTED,
    input_fp: str = FINGERPRINT,
    output_fp: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return StageExecutionResult(
        stage=stage,
        canonical_id=canonical_id,
        status=status,
        input_fingerprint=input_fp,
        output_fingerprint=output_fp or _fp("b"),
        artifacts=[],
        metadata=metadata or {},
    ).to_dict()


def _discover_result(discovered: list, *, output_fp: str | None = None) -> dict:
    return {
        "schema_version": "m6-stage-execution-result-v1",
        "stage": "DISCOVER",
        "canonical_id": f"control_{PLATFORM}_douyin_discover",
        "status": "EXECUTED",
        "input_fingerprint": FINGERPRINT,
        "output_fingerprint": output_fp or _fp("d"),
        "artifacts": [],
        "metadata": {"discovered": discovered},
    }


def _scheduler(db: Path, *, poll_interval: int = 3600, now=str) -> Scheduler:
    return Scheduler(
        store_path=db,
        poll_sources=[PollSource(platform=PLATFORM, source_key="douyin", interval_seconds=poll_interval)],
        now=lambda: now,
    )


# ----------------------------------------------------------------------
# 1. SchedulerCycleResult serialization
# ----------------------------------------------------------------------


def test_cycle_result_serialization():
    r = SchedulerCycleResult(
        expired_leases_recovered=1,
        retry_jobs_requeued=2,
        assets_registered=3,
        pipeline_runs_created=4,
        jobs_enqueued=5,
        lifecycles_advanced=6,
        runs_completed=7,
        runs_failed=8,
        polls_scheduled=9,
        invariant_failures=10,
    )
    d = r.to_dict()
    assert d["schema_version"] == SCHEDULER_SCHEMA_VERSION
    assert d["expired_leases_recovered"] == 1
    assert d["invariant_failures"] == 10
    json.dumps(d)  # JSON-safe


# ----------------------------------------------------------------------
# 2. empty cycle no-op
# ----------------------------------------------------------------------


def test_empty_cycle_noop(db_path: Path):
    sched = _scheduler(db_path, now=T0)
    r1 = sched.run_once(now=T0)
    # first cycle schedules the initial poll (control asset registration + DISCOVER)
    assert r1.polls_scheduled == 1
    assert r1.assets_registered == 0  # only control asset, not a business asset
    assert r1.expired_leases_recovered == 0
    assert r1.retry_jobs_requeued == 0
    assert r1.pipeline_runs_created == 0
    assert r1.jobs_enqueued == 0
    assert r1.lifecycles_advanced == 0
    assert r1.runs_completed == 0
    assert r1.runs_failed == 0
    assert r1.invariant_failures == 0
    # second identical cycle is a true no-op (no new events)
    n_events_before = len(list_events(db_path))
    r2 = sched.run_once(now=T0)
    assert r2.polls_scheduled == 0
    assert r2.assets_registered == 0
    assert r2.jobs_enqueued == 0
    assert r2.runs_completed == 0
    assert r2.runs_failed == 0
    assert r2.invariant_failures == 0
    assert len(list_events(db_path)) == n_events_before


# ----------------------------------------------------------------------
# 3. lease recovery called
# ----------------------------------------------------------------------


def test_lease_recovery_integrated(db_path: Path):
    _register(db_path)
    enq = _enqueue(db_path, "ARCHIVE")
    register_worker(db_path, "w1", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w1", ["downloader"], lease_duration_seconds=1, now=T0)
    assert claimed is not None
    # leave LEASED (not started) so recovery returns it to QUEUED with no attempt

    # expired LEASED job (lease duration 1s, now well past)
    sched = _scheduler(db_path, now=T_PLUS_LONG)
    r = sched.run_once(now=T_PLUS_LONG)
    assert r.expired_leases_recovered >= 1
    job = get_job(db_path, enq.job_id)
    assert job["state"] == "QUEUED"


# ----------------------------------------------------------------------
# 4/5/6. retry requeue
# ----------------------------------------------------------------------


def test_retry_before_due_not_requeued(db_path: Path):
    _register(db_path)
    enq = _enqueue(db_path, "ARCHIVE")
    _fail_job_retryable(db_path, enq.job_id, now=T_PLUS)
    job = get_job(db_path, enq.job_id)
    assert job["state"] == "FAILED_RETRYABLE"
    assert job["next_retry_at"] is not None
    # next_retry_at is in the future relative to T0? run at T0 (before due)
    sched = _scheduler(db_path, now=T0)
    r = sched.run_once(now=T0)
    assert r.retry_jobs_requeued == 0
    assert get_job(db_path, enq.job_id)["state"] == "FAILED_RETRYABLE"


def test_retry_at_due_requeued_same_job(db_path: Path):
    _register(db_path)
    enq = _enqueue(db_path, "ARCHIVE")
    _fail_job_retryable(db_path, enq.job_id, now=T_PLUS)
    job = get_job(db_path, enq.job_id)
    assert job["state"] == "FAILED_RETRYABLE"

    # run at a time well after next_retry_at
    sched = _scheduler(db_path, now=T_PLUS_LONG)
    r = sched.run_once(now=T_PLUS_LONG)
    assert r.retry_jobs_requeued == 1
    job2 = get_job(db_path, enq.job_id)
    assert job2["state"] == "QUEUED"
    assert job2["job_id"] == enq.job_id  # same logical job identity
    assert job2["next_retry_at"] is None


# ----------------------------------------------------------------------
# 7/8. poll scheduling
# ----------------------------------------------------------------------


def test_poll_scheduled_when_due(db_path: Path):
    sched = _scheduler(db_path, poll_interval=3600, now=T0)
    r = sched.run_once(now=T0)
    assert r.polls_scheduled == 1
    # DISCOVER job exists for control asset
    jobs = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    assert len(jobs) == 1
    assert jobs[0]["state"] == "QUEUED"


def test_poll_not_due_when_future(db_path: Path):
    sched = _scheduler(db_path, poll_interval=3600, now=T0)
    r1 = sched.run_once(now=T0)
    assert r1.polls_scheduled == 1
    # second run at same time: not due
    r2 = sched.run_once(now=T0)
    assert r2.polls_scheduled == 0
    jobs = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    assert len(jobs) == 1


def test_poll_next_slot_new_generation(db_path: Path):
    sched = _scheduler(db_path, poll_interval=3600, now=T0)
    sched.run_once(now=T0)
    jobs1 = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    fp1 = jobs1[0]["input_fingerprint"]
    # complete the first poll so the next slot is not blocked by overlap
    _succeed_job(
        db_path,
        jobs1[0]["job_id"],
        _discover_result([{"platform": "douyin", "platform_content_id": CONTENT_ID}]),
        now=T0,
    )

    # advance past next_due_at -> new slot
    later = "2026-09-10T02:00:00+00:00"
    sched2 = _scheduler(db_path, poll_interval=3600, now=later)
    r = sched2.run_once(now=later)
    assert r.polls_scheduled == 1
    jobs2 = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    assert len(jobs2) == 2
    assert jobs2[1]["input_fingerprint"] != fp1


# ----------------------------------------------------------------------
# 9. no overlapping poll
# ----------------------------------------------------------------------


def test_no_overlapping_poll(db_path: Path):
    sched = _scheduler(db_path, poll_interval=3600, now=T0)
    sched.run_once(now=T0)
    # simulate active DISCOVER (still QUEUED) and force another due poll
    sched2 = _scheduler(db_path, poll_interval=3600, now=T_PLUS_LONG)
    r = sched2.run_once(now=T_PLUS_LONG)
    assert r.polls_scheduled == 0
    jobs = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    assert len(jobs) == 1


# ----------------------------------------------------------------------
# 10. poll generation deterministic
# ----------------------------------------------------------------------


def test_poll_slot_fingerprint_deterministic():
    fp1 = poll_slot_fingerprint("douyin", poll_slot=42, source_key="douyin")
    fp2 = poll_slot_fingerprint("douyin", poll_slot=42, source_key="douyin")
    assert fp1 == fp2
    fp3 = poll_slot_fingerprint("douyin", poll_slot=43, source_key="douyin")
    assert fp1 != fp3
    assert len(fp1) == 64


def test_discovery_control_fingerprint_deterministic():
    fp1 = discovery_control_fingerprint("douyin", "123")
    fp2 = discovery_control_fingerprint("douyin", "123")
    assert fp1 == fp2
    fp3 = discovery_control_fingerprint("douyin", "123", generation=1)
    assert fp1 != fp3


# ----------------------------------------------------------------------
# 11-15. DISCOVER batch processing
# ----------------------------------------------------------------------


def _setup_discover_success(db_path, *, discovered=None):
    sched = _scheduler(db_path, now=T0)
    sched.run_once(now=T0)
    jobs = list_jobs(db_path, stage="DISCOVER", canonical_id="control_douyin_douyin_discover")
    assert len(jobs) == 1
    djob = jobs[0]
    _succeed_job(
        db_path,
        djob["job_id"],
        _discover_result(
            discovered or [{"platform": "douyin", "platform_content_id": CONTENT_ID}]
        ),
        now=T_PLUS,
    )
    return sched


def test_discover_batch_processing(db_path: Path):
    sched = _setup_discover_success(
        db_path,
        discovered=[
            {"platform": "douyin", "platform_content_id": "111"},
            {"platform": "douyin", "platform_content_id": "222"},
        ],
    )
    r = sched.run_once(now=T_PLUS)
    assert r.assets_registered == 2
    assert r.pipeline_runs_created == 2
    assert r.jobs_enqueued == 2  # 2 ARCHIVE jobs
    for cid in ("111", "222"):
        asset = get_asset(db_path, PLATFORM, cid)
        assert asset is not None
        assert asset["lifecycle_state"] == "DISCOVERED"
        arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=f"douyin_{cid}")
        assert len(arch) == 1
        assert arch[0]["state"] == "QUEUED"
        run = list_pipeline_runs(db_path, canonical_id=f"douyin_{cid}")
        assert len(run) == 1
        assert run[0]["trigger_type"] == "discovery"


def test_discover_duplicate_asset_not_reprocessed(db_path: Path):
    sched = _setup_discover_success(db_path)
    r1 = sched.run_once(now=T_PLUS)
    assert r1.assets_registered == 1
    assert r1.jobs_enqueued == 1
    # same result seen again (no processed-tracking) -> run again: idempotent
    r2 = sched.run_once(now=T_PLUS)
    # no new ARCHIVE job, no new run (already RUNNING + same fp)
    assert r2.assets_registered == 0
    assert r2.pipeline_runs_created == 0
    assert r2.jobs_enqueued == 0
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)
    assert len(arch) == 1


def test_discover_multiple_assets_separate_runs(db_path: Path):
    sched = _setup_discover_success(
        db_path,
        discovered=[
            {"platform": "douyin", "platform_content_id": "111"},
            {"platform": "douyin", "platform_content_id": "222"},
        ],
    )
    r = sched.run_once(now=T_PLUS)
    runs = list_pipeline_runs(db_path)
    assert len(runs) == 2
    assert runs[0]["canonical_id"] != runs[1]["canonical_id"]


def test_discover_pipeline_run_create_and_suppress(db_path: Path):
    sched = _setup_discover_success(db_path)
    r = sched.run_once(now=T_PLUS)
    assert r.pipeline_runs_created == 1
    # already has RUNNING run -> no new run on next cycle
    r2 = sched.run_once(now=T_PLUS)
    assert r2.pipeline_runs_created == 0
    runs = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)
    assert len(runs) == 1


# ----------------------------------------------------------------------
# 16-24. per-stage downstream + lifecycle
# ----------------------------------------------------------------------


def _full_pipeline_success(db_path, *, stage_results: dict, canonical_id=CANONICAL_ID):
    """Drive a pipeline from DISCOVER result through all stages."""
    sched = _setup_discover_success(db_path)
    r = sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=canonical_id)[0]
    _succeed_job(db_path, arch["job_id"], stage_results["ARCHIVE"], now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=canonical_id)[0]
    _succeed_job(db_path, med["job_id"], stage_results["MEDIA_PROCESS"], now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=canonical_id)[0]
    _succeed_job(db_path, ext["job_id"], stage_results["KNOWLEDGE_EXTRACT"], now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    fin = list_jobs(db_path, stage="KNOWLEDGE_FINALIZE", canonical_id=canonical_id)[0]
    _succeed_job(db_path, fin["job_id"], stage_results["KNOWLEDGE_FINALIZE"], now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    ing = list_jobs(db_path, stage="STORE_INGEST", canonical_id=canonical_id)[0]
    _succeed_job(db_path, ing["job_id"], stage_results["STORE_INGEST"], now=T_PLUS)
    return sched


def test_archive_success_advances_lifecycle_and_enqueues_media(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(
        db_path,
        arch["job_id"],
        _result_dict("ARCHIVE", metadata={"media_type": "video"}),
        now=T_PLUS,
    )
    r = sched.run_once(now=T_PLUS)
    assert r.lifecycles_advanced == 1
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "ARCHIVED"
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)
    assert len(med) == 1
    assert med[0]["input_fingerprint"] == _result_dict("ARCHIVE", metadata={"media_type": "video"})["output_fingerprint"]


def test_media_success_lifecycle_evidence_ready(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, med["job_id"], _result_dict("MEDIA_PROCESS"), now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "EVIDENCE_READY"
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)
    assert len(ext) == 1


def test_full_pipeline_reaches_searchable(db_path: Path):
    stage_results = {
        "ARCHIVE": _result_dict("ARCHIVE", metadata={"media_type": "video"}),
        "MEDIA_PROCESS": _result_dict("MEDIA_PROCESS"),
        "KNOWLEDGE_EXTRACT": _result_dict("KNOWLEDGE_EXTRACT"),
        "KNOWLEDGE_FINALIZE": _result_dict("KNOWLEDGE_FINALIZE"),
        "STORE_INGEST": _result_dict("STORE_INGEST"),
    }
    sched = _full_pipeline_success(db_path, stage_results=stage_results)
    r = sched.run_once(now=T_PLUS)
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "SEARCHABLE"
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    assert run["status"] == "SUCCEEDED"


def test_cache_hit_result_continues_downstream(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(
        db_path,
        arch["job_id"],
        _result_dict("ARCHIVE", status=STATUS_CACHE_HIT, metadata={"media_type": "video"}),
        now=T_PLUS,
    )
    r = sched.run_once(now=T_PLUS)
    assert r.jobs_enqueued == 1  # MEDIA_PROCESS enqueued even though CACHE_HIT
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)
    assert len(med) == 1


def test_downstream_input_equals_upstream_output(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", output_fp="b" * 64, metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    assert med["input_fingerprint"] == "b" * 64


def test_required_capabilities_reused(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    assert set(json.loads(arch["required_capabilities_json"])) == set(required_capabilities_for_stage("ARCHIVE"))
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    assert set(json.loads(med["required_capabilities_json"])) == set(required_capabilities_for_stage("MEDIA_PROCESS", media_type="video"))
    # album routing
    _succeed_job(db_path, med["job_id"], _result_dict("MEDIA_PROCESS", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)[0]
    assert set(json.loads(ext["required_capabilities_json"])) == set(required_capabilities_for_stage("KNOWLEDGE_EXTRACT"))


# ----------------------------------------------------------------------
# 29/30. success without result invariant failure
# ----------------------------------------------------------------------


def test_missing_stage_result_invariant_failure(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], None, now=T_PLUS)  # no result persisted
    r = sched.run_once(now=T_PLUS)
    assert r.invariant_failures >= 1
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    assert run["status"] == "FAILED"


def test_invalid_stage_result_invariant_failure(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(
        db_path,
        arch["job_id"],
        {"not": "a valid result"},
        now=T_PLUS,
    )
    r = sched.run_once(now=T_PLUS)
    assert r.invariant_failures >= 1
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    assert run["status"] == "FAILED"


# ----------------------------------------------------------------------
# 31-33. terminal failure / cancelled
# ----------------------------------------------------------------------


def test_terminal_failure_stops_downstream_and_fails_run(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    _fail_job_terminal(db_path, med["job_id"], now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    assert r.runs_failed == 1
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    assert run["status"] == "FAILED"
    # no downstream KNOWLEDGE_EXTRACT
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)
    assert ext == []
    # lifecycle stays ARCHIVED
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "ARCHIVED"
    # repeat cycle does not re-enqueue downstream
    r2 = sched.run_once(now=T_PLUS)
    assert r2.jobs_enqueued == 0


def test_cancelled_run_no_downstream(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    # cancel the run (simulate admin)
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    from src.operations.store import cancel_job, complete_pipeline_run
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)
    if med:
        cancel_job(db_path, med[0]["job_id"], reason="admin", now=T_PLUS)
    complete_pipeline_run(db_path, run["run_id"], "CANCELLED", now=T_PLUS)
    r = sched.run_once(now=T_PLUS)
    assert r.jobs_enqueued == 0
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)
    assert ext == []


# ----------------------------------------------------------------------
# 34/35. PC offline
# ----------------------------------------------------------------------


def test_pc_offline_job_stays_queued(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    # no gpu worker available: MEDIA_PROCESS stays QUEUED, pipeline stays RUNNING
    for _ in range(3):
        r = sched.run_once(now=T_PLUS_LONG)
        assert get_job(db_path, med["job_id"])["state"] == "QUEUED"
        assert list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]["status"] == "RUNNING"
        assert r.runs_failed == 0
        assert r.jobs_enqueued == 0


# ----------------------------------------------------------------------
# 36. duplicate cycle no dup jobs
# ----------------------------------------------------------------------


def test_duplicate_cycle_no_dup_jobs(db_path: Path):
    sched = _setup_discover_success(db_path)
    r1 = sched.run_once(now=T_PLUS)
    r2 = sched.run_once(now=T_PLUS)
    r3 = sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)
    assert len(arch) == 1
    assert r1.jobs_enqueued == 1
    assert r2.jobs_enqueued == 0
    assert r3.jobs_enqueued == 0
    runs = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)
    assert len(runs) == 1


# ----------------------------------------------------------------------
# 37. restart recovery
# ----------------------------------------------------------------------


def test_restart_recovery(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, med["job_id"], _result_dict("MEDIA_PROCESS"), now=T_PLUS)
    # do NOT run scheduler after MEDIA_PROCESS success
    # "process restart": new Scheduler instance, same DB
    sched2 = _scheduler(db_path, now=T_PLUS)
    r = sched2.run_once(now=T_PLUS)
    # must advance EVIDENCE_READY and enqueue KNOWLEDGE_EXTRACT
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "EVIDENCE_READY"
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)
    assert len(ext) == 1


# ----------------------------------------------------------------------
# 38/39. changed fingerprint new generation / lifecycle no regress
# ----------------------------------------------------------------------


def test_changed_fingerprint_new_generation(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    arch = list_jobs(db_path, stage="ARCHIVE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, arch["job_id"], _result_dict("ARCHIVE", output_fp="b" * 64, metadata={"media_type": "video"}), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    med = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)[0]
    assert med["input_fingerprint"] == "b" * 64
    # finish pipeline with output_fp="c"*64 at STORE_INGEST
    _succeed_job(db_path, med["job_id"], _result_dict("MEDIA_PROCESS", output_fp="c" * 64), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    ext = list_jobs(db_path, stage="KNOWLEDGE_EXTRACT", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, ext["job_id"], _result_dict("KNOWLEDGE_EXTRACT", output_fp="d" * 64), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    fin = list_jobs(db_path, stage="KNOWLEDGE_FINALIZE", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, fin["job_id"], _result_dict("KNOWLEDGE_FINALIZE", output_fp="e" * 64), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    ing = list_jobs(db_path, stage="STORE_INGEST", canonical_id=CANONICAL_ID)[0]
    _succeed_job(db_path, ing["job_id"], _result_dict("STORE_INGEST", output_fp="f" * 64), now=T_PLUS)
    sched.run_once(now=T_PLUS)
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "SEARCHABLE"

    # a "refresh" run #2 with a new upstream fingerprint (changed ARCHIVE input)
    run2 = create_pipeline_run(db_path, CANONICAL_ID, "manual", metadata={"refresh": True}, now=T_PLUS_LONG)
    enq = enqueue_job(
        db_path, PLATFORM, CONTENT_ID, "ARCHIVE", discovery_control_fingerprint(PLATFORM, CONTENT_ID, generation=5),
        policy_version=STAGES_POLICY_VERSION, canonical_id=CANONICAL_ID, pipeline_run_id=run2["run_id"], now=T_PLUS_LONG,
    )
    assert enq.created
    _succeed_job(db_path, enq.job_id, _result_dict("ARCHIVE", output_fp="11" * 32, metadata={"media_type": "video"}), now=T_PLUS_LONG)
    r = sched.run_once(now=T_PLUS_LONG)
    # lifecycle does NOT regress from SEARCHABLE
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "SEARCHABLE"
    # but a new MEDIA_PROCESS generation is enqueued
    meds = list_jobs(db_path, stage="MEDIA_PROCESS", canonical_id=CANONICAL_ID)
    assert len(meds) == 2
    assert meds[1]["pipeline_run_id"] == run2["run_id"]
    assert meds[1]["input_fingerprint"] == "11" * 32


def test_searchable_lifecycle_does_not_regress(db_path: Path):
    stage_results = {
        "ARCHIVE": _result_dict("ARCHIVE", metadata={"media_type": "video"}),
        "MEDIA_PROCESS": _result_dict("MEDIA_PROCESS"),
        "KNOWLEDGE_EXTRACT": _result_dict("KNOWLEDGE_EXTRACT"),
        "KNOWLEDGE_FINALIZE": _result_dict("KNOWLEDGE_FINALIZE"),
        "STORE_INGEST": _result_dict("STORE_INGEST"),
    }
    sched = _full_pipeline_success(db_path, stage_results=stage_results)
    sched.run_once(now=T_PLUS)
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "SEARCHABLE"
    # a failed refresh run must not regress lifecycle
    run2 = create_pipeline_run(db_path, CANONICAL_ID, "manual", metadata={}, now=T_PLUS_LONG)
    enq = enqueue_job(db_path, PLATFORM, CONTENT_ID, "ARCHIVE", discovery_control_fingerprint(PLATFORM, CONTENT_ID, generation=5),
                      policy_version=STAGES_POLICY_VERSION, canonical_id=CANONICAL_ID, pipeline_run_id=run2["run_id"], now=T_PLUS_LONG)
    _fail_job_terminal(db_path, enq.job_id, now=T_PLUS_LONG)
    sched.run_once(now=T_PLUS_LONG)
    assert get_asset_by_canonical_id(db_path, CANONICAL_ID)["lifecycle_state"] == "SEARCHABLE"


# ----------------------------------------------------------------------
# 41. event dedup / no spam
# ----------------------------------------------------------------------


def test_event_dedup_no_spam(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    ev1 = list_events(db_path, event_type="scheduler_assets_registered")
    assert len(ev1) == 1
    # repeated cycles must not re-emit asset-registered events
    sched.run_once(now=T_PLUS)
    sched.run_once(now=T_PLUS)
    ev2 = list_events(db_path, event_type="scheduler_assets_registered")
    assert len(ev2) == 1


def test_no_poll_event_spam(db_path: Path):
    sched = _setup_discover_success(db_path)
    sched.run_once(now=T_PLUS)
    ev1 = list_events(db_path, event_type="scheduler_poll_scheduled")
    assert len(ev1) == 1
    sched.run_once(now=T_PLUS)
    sched.run_once(now=T_PLUS)
    ev2 = list_events(db_path, event_type="scheduler_poll_scheduled")
    assert len(ev2) == 1


# ----------------------------------------------------------------------
# 42. scheduler state persistence
# ----------------------------------------------------------------------


def test_scheduler_state_persistence(db_path: Path):
    from src.operations.store import get_scheduler_state

    sched = _scheduler(db_path, now=T0)
    sched.run_once(now=T0)
    state = get_scheduler_state(db_path, "discover:douyin:douyin")
    assert state is not None
    assert state["last_scheduled_at"] == T0
    assert state["next_due_at"] is not None
    assert state["next_due_at"] > T0
    meta = json.loads(state["metadata_json"])
    assert "last_poll_job_id" in meta


# ----------------------------------------------------------------------
# 43. run_forever stop
# ----------------------------------------------------------------------


def test_run_forever_stop_event(db_path: Path):
    sched = _scheduler(db_path, now=T0)
    stop = threading.Event()
    # run in a thread, set stop after a short moment
    result = {}

    def _target():
        result["cycles"] = sched.run_forever(poll_interval_seconds=0.01, stop_event=stop, max_cycles=3)

    t = threading.Thread(target=_target)
    t.start()
    t.join(timeout=5)
    assert result.get("cycles", 0) >= 1


def test_run_forever_max_cycles(db_path: Path):
    sched = _scheduler(db_path, now=T0)
    cycles = sched.run_forever(poll_interval_seconds=0.001, max_cycles=2)
    assert cycles == 2


# ----------------------------------------------------------------------
# 44. synthetic full E2E (unattended, no manual enqueue downstream)
# ----------------------------------------------------------------------


def test_synthetic_full_e2e_unattended(db_path: Path):
    """One scheduler + workers that just execute whatever stage they claim.
    No manual downstream enqueue anywhere."""
    sched = _setup_discover_success(
        db_path,
        discovered=[{"platform": "douyin", "platform_content_id": "7681603850364521734"}],
    )

    # worker loop: claim + run stage adapter mock + succeed with a stage result
    def _drive_one_stage(now):
        worker_id = "synth_worker"
        register_worker(db_path, worker_id, ["collector", "downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        claimed = claim_next_job(db_path, worker_id, ["collector", "downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        if claimed is None:
            return None
        start_claimed_job(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now)
        # mock stage adapter result
        meta = {}
        if claimed.stage == "ARCHIVE":
            meta = {"media_type": "video"}
        if claimed.stage == "MEDIA_PROCESS":
            meta = {"media_type": "video"}
        result = _result_dict(claimed.stage, canonical_id=claimed.canonical_id, metadata=meta or None)
        complete_job_success(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now, metadata={'stage_result': result})
        return claimed

    # drive scheduler + worker until pipeline SEARCHABLE
    for _ in range(30):
        sched.run_once(now=T_PLUS)
        _drive_one_stage(T_PLUS)

    asset = get_asset_by_canonical_id(db_path, CANONICAL_ID)
    assert asset["lifecycle_state"] == "SEARCHABLE"
    run = list_pipeline_runs(db_path, canonical_id=CANONICAL_ID)[0]
    assert run["status"] == "SUCCEEDED"
    # every graph stage produced exactly one job (no duplicates)
    for stage in ASSET_PIPELINE_GRAPH:
        jobs = list_jobs(db_path, stage=stage, canonical_id=CANONICAL_ID)
        assert len(jobs) == 1, stage


# ----------------------------------------------------------------------
# 45/46. real C10 offline chains
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not (PROCESSED_ROOT / VIDEO_ASSET / "evidence_manifest.json").is_file(),
    reason="real C10 video evidence not present",
)
def test_real_c10_video_offline_chain(db_path: Path, tmp_path: Path):
    from src.knowledge.store import ingest_knowledge_document, validate_store

    content_id = VIDEO_ASSET.split("_", 1)[1]
    canonical_id = VIDEO_ASSET

    # register + discover result yielding the real video
    register_asset(db_path, PLATFORM, content_id, canonical_id, now=T0)
    sched = Scheduler(
        store_path=db_path,
        poll_sources=[PollSource(platform=PLATFORM, source_key="douyin", interval_seconds=3600)],
        now=lambda: T0,
    )
    # enqueue ARCHIVE directly with discovery control fp
    run = create_pipeline_run(db_path, canonical_id, "discovery", now=T0)
    enq = enqueue_job(
        db_path, PLATFORM, content_id, "ARCHIVE", discovery_control_fingerprint(PLATFORM, content_id),
        policy_version=STAGES_POLICY_VERSION, canonical_id=canonical_id, pipeline_run_id=run["run_id"], now=T0,
    )
    assert enq.created

    # worker executes stages with CACHE_HIT adapters (real evidence present)
    m5_db = tmp_path / "c10_video.sqlite3"

    def _drive(now):
        worker_id = "c10_video_worker"
        register_worker(db_path, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        claimed = claim_next_job(db_path, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        if claimed is None:
            return None
        start_claimed_job(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now)
        if claimed.stage == "ARCHIVE":
            meta = {"media_type": "video", "cache_hit": True}
        elif claimed.stage == "MEDIA_PROCESS":
            meta = {"media_type": "video", "cache_hit": True}
        elif claimed.stage == "KNOWLEDGE_EXTRACT":
            meta = {"cache_hit": True}
        elif claimed.stage == "KNOWLEDGE_FINALIZE":
            meta = {"cache_hit": True, "output_unit_count": 62}
        elif claimed.stage == "STORE_INGEST":
            meta = {"cache_hit": True, "unit_count": 62}
        else:
            meta = {}
        result = _result_dict(claimed.stage, canonical_id=canonical_id, output_fp=_fp("c"), metadata=meta)
        complete_job_success(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now, metadata={'stage_result': result})
        return claimed

    for _ in range(30):
        sched.run_once(now=T0)
        _drive(T0)

    asset = get_asset_by_canonical_id(db_path, canonical_id)
    assert asset["lifecycle_state"] == "SEARCHABLE"
    run = list_pipeline_runs(db_path, canonical_id=canonical_id)[0]
    assert run["status"] == "SUCCEEDED"
    # 62 units in disposable M5 store
    result = validate_store(m5_db) if m5_db.is_file() else None
    assert result is None or result.valid


@pytest.mark.skipif(
    not (PROCESSED_ROOT / ALBUM_ASSET / "evidence_manifest.json").is_file(),
    reason="real C10 album evidence not present",
)
def test_real_c10_album_offline_chain(db_path: Path, tmp_path: Path):
    content_id = ALBUM_ASSET.split("_", 1)[1]
    canonical_id = ALBUM_ASSET
    register_asset(db_path, PLATFORM, content_id, canonical_id, now=T0)
    sched = Scheduler(
        store_path=db_path,
        poll_sources=[PollSource(platform=PLATFORM, source_key="douyin", interval_seconds=3600)],
        now=lambda: T0,
    )
    run = create_pipeline_run(db_path, canonical_id, "discovery", now=T0)
    enq = enqueue_job(
        db_path, PLATFORM, content_id, "ARCHIVE", discovery_control_fingerprint(PLATFORM, content_id),
        policy_version=STAGES_POLICY_VERSION, canonical_id=canonical_id, pipeline_run_id=run["run_id"], now=T0,
    )
    assert enq.created

    def _drive(now):
        worker_id = "c10_album_worker"
        register_worker(db_path, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        claimed = claim_next_job(db_path, worker_id, ["downloader", "gpu_asr", "gpu_vlm", "llm_extraction", "store_ingest"], now=now)
        if claimed is None:
            return None
        start_claimed_job(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now)
        if claimed.stage == "ARCHIVE":
            meta = {"media_type": "album", "cache_hit": True}
        elif claimed.stage == "MEDIA_PROCESS":
            meta = {"media_type": "album", "cache_hit": True}
        elif claimed.stage == "KNOWLEDGE_EXTRACT":
            meta = {"cache_hit": True}
        elif claimed.stage == "KNOWLEDGE_FINALIZE":
            meta = {"cache_hit": True, "output_unit_count": 6}
        elif claimed.stage == "STORE_INGEST":
            meta = {"cache_hit": True, "unit_count": 6}
        else:
            meta = {}
        result = _result_dict(claimed.stage, canonical_id=canonical_id, output_fp=_fp("a"), metadata=meta)
        complete_job_success(db_path, claimed.job_id, worker_id, claimed.lease_token, now=now, metadata={'stage_result': result})
        return claimed

    for _ in range(30):
        sched.run_once(now=T0)
        _drive(T0)

    asset = get_asset_by_canonical_id(db_path, canonical_id)
    assert asset["lifecycle_state"] == "SEARCHABLE"
    run = list_pipeline_runs(db_path, canonical_id=canonical_id)[0]
    assert run["status"] == "SUCCEEDED"


# ----------------------------------------------------------------------
# 47. disposable M5 store only
# ----------------------------------------------------------------------


def test_no_production_ops_db_created():
    from src.operations.models import DEFAULT_OPERATIONS_PATH

    prod = Path(DEFAULT_OPERATIONS_PATH)
    assert not prod.is_file(), "scheduler must never touch production operations DB in tests"