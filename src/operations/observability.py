"""M6-05 read-only observability projection over the Operations store.

Every function here is a **pure read projection**. It derives asset / job /
worker / pipeline health from the durable rows in ``assets``, ``pipeline_runs``,
``jobs``, ``job_attempts``, ``workers`` and ``event_log`` — it never writes a
second copy of truth (M6-05 §5). No mutation, no secrets: the projection picks
explicit fields only and can never emit a ``lease_token``.

Health vocabulary (frozen M6-05):

  HEALTHY             asset pipeline complete and idle / no pipeline activity
  RUNNING             a stage job is LEASED/RUNNING or QUEUED with matching workers
  WAITING_FOR_WORKER  current job QUEUED but no live worker matches its capabilities
  WAITING_RETRY       current job FAILED_RETRYABLE, backoff in progress (auto recovers)
  SUCCEEDED           latest pipeline run completed SUCCEEDED
  FAILED_TERMINAL     current job terminal failure or run FAILED (manual attention)
  CANCELLED           current job / run cancelled by admin (not a failure)
  STALLED             lease expired but recovery not yet run, or scheduler
                      crashed between a SUCCEEDED stage and its downstream enqueue
  INVARIANT_ERROR     orchestration invariant violated (missing durable result,
                      missing output fingerprint, unroutable capability)

AUTO vs MANUAL boundary (M6-05 §40):

  auto-recovered  : WAITING_RETRY, WAITING_FOR_WORKER, RUNNING (lease recovery,
                    requeue, worker rejoin, retryable backoff)
  manual-attention: STALLED, INVARIANT_ERROR, FAILED_TERMINAL
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import DEFAULT_OPERATIONS_PATH, parse_iso
from .scheduler import ASSET_PIPELINE_GRAPH, LIFECYCLE_RANK, STAGE_MILESTONES
from .store import (
    DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
    get_asset_by_canonical_id,
    is_worker_stale,
    list_events,
    list_job_attempts,
    list_jobs,
    list_pipeline_runs,
    list_workers_with_status,
    open_operations_store,
)

# ----------------------------------------------------------------------
# Frozen health vocabulary
# ----------------------------------------------------------------------

HEALTH_HEALTHY = "HEALTHY"
HEALTH_RUNNING = "RUNNING"
HEALTH_WAITING_FOR_WORKER = "WAITING_FOR_WORKER"
HEALTH_WAITING_RETRY = "WAITING_RETRY"
HEALTH_SUCCEEDED = "SUCCEEDED"
HEALTH_FAILED_TERMINAL = "FAILED_TERMINAL"
HEALTH_CANCELLED = "CANCELLED"
HEALTH_STALLED = "STALLED"
HEALTH_INVARIANT_ERROR = "INVARIANT_ERROR"

_HEALTH_VALUES = frozenset(
    {
        HEALTH_HEALTHY,
        HEALTH_RUNNING,
        HEALTH_WAITING_FOR_WORKER,
        HEALTH_WAITING_RETRY,
        HEALTH_SUCCEEDED,
        HEALTH_FAILED_TERMINAL,
        HEALTH_CANCELLED,
        HEALTH_STALLED,
        HEALTH_INVARIANT_ERROR,
    }
)

# Health states that require an administrator to look (M6-05 §40).
MANUAL_ATTENTION_HEALTH = frozenset(
    {HEALTH_STALLED, HEALTH_INVARIANT_ERROR, HEALTH_FAILED_TERMINAL}
)

# Event type used by the scheduler when an orchestration invariant is violated.
_INVARIANT_EVENT = "orchestration_invariant_failure"

_TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED_TERMINAL", "CANCELLED"}


# ----------------------------------------------------------------------
# Frozen result models
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AssetPipelineStatus:
    """JSON-safe derived status of one canonical asset's pipeline."""

    canonical_id: str
    platform: str
    platform_content_id: str
    asset_lifecycle_state: str
    current_pipeline_run_id: Optional[str] = None
    current_pipeline_status: Optional[str] = None
    current_stage: Optional[str] = None
    current_job_id: Optional[str] = None
    current_job_state: Optional[str] = None
    required_capabilities: tuple[str, ...] = ()
    matching_worker_count: int = 0
    assigned_worker: Optional[str] = None
    worker_stale: Optional[bool] = None
    attempt_count: int = 0
    max_attempts: int = 0
    last_error_class: Optional[str] = None
    last_error_message: Optional[str] = None
    next_retry_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    last_event_at: Optional[str] = None
    health: str = HEALTH_HEALTHY
    searchable: bool = False
    historically_searchable: bool = False
    current_refresh_failed: bool = False
    needs_attention: bool = False
    attention_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "asset_lifecycle_state": self.asset_lifecycle_state,
            "current_pipeline_run_id": self.current_pipeline_run_id,
            "current_pipeline_status": self.current_pipeline_status,
            "current_stage": self.current_stage,
            "current_job_id": self.current_job_id,
            "current_job_state": self.current_job_state,
            "required_capabilities": list(self.required_capabilities),
            "matching_worker_count": self.matching_worker_count,
            "assigned_worker": self.assigned_worker,
            "worker_stale": self.worker_stale,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "last_error_class": self.last_error_class,
            "last_error_message": self.last_error_message,
            "next_retry_at": self.next_retry_at,
            "lease_expires_at": self.lease_expires_at,
            "last_event_at": self.last_event_at,
            "health": self.health,
            "searchable": self.searchable,
            "historically_searchable": self.historically_searchable,
            "current_refresh_failed": self.current_refresh_failed,
            "needs_attention": self.needs_attention,
            "attention_reason": self.attention_reason,
        }


@dataclass(frozen=True)
class WorkerStatus:
    """Derived per-worker status (never trusts the human ``status`` column alone)."""

    worker_id: str
    display_name: Optional[str] = None
    hostname: Optional[str] = None
    capabilities: tuple[str, ...] = ()
    last_heartbeat_at: Optional[str] = None
    derived_status: str = "active"  # "active" | "stale"
    online: bool = False
    currently_owned_jobs: tuple[str, ...] = ()
    running_jobs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "display_name": self.display_name,
            "hostname": self.hostname,
            "capabilities": list(self.capabilities),
            "last_heartbeat_at": self.last_heartbeat_at,
            "derived_status": self.derived_status,
            "online": self.online,
            "currently_owned_jobs": list(self.currently_owned_jobs),
            "running_jobs": list(self.running_jobs),
        }


@dataclass(frozen=True)
class OperationsSummary:
    """JSON-safe global operations summary (lightweight default; full validation
    only when explicitly requested via ``store_valid``)."""

    schema_version: str = "m6-observability-v1"
    assets_total: int = 0
    searchable_assets: int = 0
    active_pipeline_runs: int = 0
    jobs_by_state: dict[str, int] = field(default_factory=dict)
    jobs_by_stage: dict[str, int] = field(default_factory=dict)
    workers_total: int = 0
    workers_online: int = 0
    workers_stale: int = 0
    waiting_for_worker: int = 0
    waiting_retry: int = 0
    terminal_failures: int = 0
    stalled_pipelines: int = 0
    invariant_errors: int = 0
    next_retry_at: Optional[str] = None
    store_valid: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assets_total": self.assets_total,
            "searchable_assets": self.searchable_assets,
            "active_pipeline_runs": self.active_pipeline_runs,
            "jobs_by_state": dict(self.jobs_by_state),
            "jobs_by_stage": dict(self.jobs_by_stage),
            "workers_total": self.workers_total,
            "workers_online": self.workers_online,
            "workers_stale": self.workers_stale,
            "waiting_for_worker": self.waiting_for_worker,
            "waiting_retry": self.waiting_retry,
            "terminal_failures": self.terminal_failures,
            "stalled_pipelines": self.stalled_pipelines,
            "invariant_errors": self.invariant_errors,
            "next_retry_at": self.next_retry_at,
            "store_valid": self.store_valid,
        }


# ----------------------------------------------------------------------
# Internal projection helpers
# ----------------------------------------------------------------------


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _json_loads_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            # The store may double-encode a JSON list string (heartbeat with
            # capabilities=None re-dumps the stored string). Unwrap recursively.
            while isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except (ValueError, TypeError):
                    break
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (ValueError, TypeError):
            pass
    return []


def _latest_events_by_asset(db_path: Path) -> dict[str, str]:
    """Map canonical_id -> latest event timestamp (max event_rowid wins)."""
    result: dict[str, str] = {}
    for event in list_events(db_path):
        cid = event.get("canonical_id")
        if not cid:
            continue
        current = result.get(cid)
        if current is None or event.get("timestamp", "") > current:
            result[cid] = event["timestamp"]
    return result


def _latest_failed_attempt(db_path: Path, job_id: str) -> Optional[dict[str, Any]]:
    latest: Optional[dict[str, Any]] = None
    for attempt in list_job_attempts(db_path, job_id):
        if attempt.get("outcome") != "failed":
            continue
        finished = attempt.get("finished_at")
        if finished is None:
            continue
        if latest is None or finished >= latest["finished_at"]:
            latest = attempt
    if latest is None:
        return None
    return {
        "attempt_number": latest.get("attempt_number"),
        "worker_id": latest.get("worker_id"),
        "error_class": latest.get("error_class"),
        "error_message": latest.get("error_message"),
        "retryable": bool(latest.get("retryable")),
        "finished_at": latest.get("finished_at"),
    }


def _invariant_run_ids(db_path: Path) -> set[str]:
    """Set of pipeline_run_ids that produced an orchestration invariant event."""
    return {
        e["pipeline_run_id"]
        for e in list_events(db_path, event_type=_INVARIANT_EVENT)
        if e.get("pipeline_run_id")
    }


# ----------------------------------------------------------------------
# Asset pipeline status
# ----------------------------------------------------------------------


def _matching_worker_count(
    workers: list[dict[str, Any]], required_capabilities: tuple[str, ...]
) -> int:
    count = 0
    for worker in workers:
        if worker.get("derived_status") != "active":
            continue
        caps = set(_json_loads_list(worker.get("capabilities_json")))
        if not required_capabilities or caps.issuperset(required_capabilities):
            count += 1
    return count


def _classify(
    *,
    current_job: Optional[dict[str, Any]],
    current_stage: Optional[str],
    run: Optional[dict[str, Any]],
    asset: dict[str, Any],
    workers: list[dict[str, Any]],
    now: str,
    invariant_run_ids: set[str],
    stale_threshold_seconds: int,
    upstream_succeeded: bool = False,
) -> tuple[str, bool, Optional[str]]:
    """Return (health, needs_attention, attention_reason). Deterministic."""

    run_status = run.get("status") if run else None
    run_id = run.get("run_id") if run else None

    # INVARIANT_ERROR wins over everything else for a failed run.
    if run_id in invariant_run_ids:
        return HEALTH_INVARIANT_ERROR, True, "orchestration invariant violated"

    if current_job is None:
        # No blocking job: if the run is RUNNING the scheduler must still be
        # driving it (scheduler crashed between run creation and first enqueue,
        # or between SUCCEEDED stage and downstream enqueue).
        if run_status == "RUNNING":
            if upstream_succeeded:
                reason = (
                    "SUCCEEDED upstream stage but downstream stage was not "
                    "enqueued (scheduler crash window)"
                )
            else:
                reason = (
                    "pipeline RUNNING but no blocking job (scheduler may have crashed)"
                )
            return HEALTH_STALLED, True, reason
        if run_status == "FAILED":
            return HEALTH_FAILED_TERMINAL, True, "pipeline run failed"
        if run_status == "CANCELLED":
            return HEALTH_CANCELLED, False, None
        if run_status == "SUCCEEDED":
            return HEALTH_SUCCEEDED, False, None
        return HEALTH_HEALTHY, False, None

    state = current_job.get("state")

    if state in ("LEASED", "RUNNING"):
        lease_expires_at = current_job.get("lease_expires_at")
        if lease_expires_at is not None and lease_expires_at <= now:
            return (
                HEALTH_STALLED,
                True,
                f"lease expired at {lease_expires_at} but recovery has not run",
            )
        return HEALTH_RUNNING, False, None

    if state == "QUEUED":
        required = tuple(
            _json_loads_list(current_job.get("required_capabilities_json"))
        )
        if _matching_worker_count(workers, required) == 0:
            return (
                HEALTH_WAITING_FOR_WORKER,
                False,
                None,
            )
        return HEALTH_RUNNING, False, None

    if state == "FAILED_RETRYABLE":
        next_retry_at = current_job.get("next_retry_at")
        # Backoff in progress -> auto-recovered by scheduler requeue. Also treat
        # a due-but-not-yet-requeued retryable as waiting_retry (not a failure).
        return HEALTH_WAITING_RETRY, False, None

    if state == "FAILED_TERMINAL":
        reason = "stage failed terminally"
        return HEALTH_FAILED_TERMINAL, True, reason

    if state == "CANCELLED":
        return HEALTH_CANCELLED, False, None

    if state == "SUCCEEDED":
        # A SUCCEEDED blocking job means the pipeline has advanced past it; the
        # run should have completed or moved on. If run still RUNNING and this
        # is not STORE_INGEST, the scheduler crashed before enqueuing downstream.
        if run_status == "RUNNING" and current_stage != "STORE_INGEST":
            return (
                HEALTH_STALLED,
                True,
                f"{current_stage} SUCCEEDED but downstream stage was not enqueued (scheduler crash window)",
            )
        if run_status == "SUCCEEDED":
            return HEALTH_SUCCEEDED, False, None
        return HEALTH_RUNNING, False, None

    return HEALTH_HEALTHY, False, None


def _select_run(
    runs: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Prefer the RUNNING run; otherwise the newest run."""
    for run in runs:
        if run.get("status") == "RUNNING":
            return run
    return runs[0] if runs else None


def _blocking_stage_and_job(
    run: dict[str, Any],
    jobs_by_stage: dict[str, list[dict[str, Any]]],
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Walk ASSET_PIPELINE_GRAPH and return the first non-succeeded stage/job.

    Mirrors the scheduler's reconcile walk so observability shows the same
    blocking job the scheduler sees. If a stage has no job yet (missing), we
    keep scanning later stages: a terminal job in a later stage (e.g. a
    STORE_INGEST that FAILED_TERMINAL while ARCHIVE has no row yet) is the real
    blocker and must win over a merely-missing earlier stage.
    """
    run_id = run["run_id"]
    missing_stage: Optional[str] = None
    for stage in ASSET_PIPELINE_GRAPH:
        jobs = [
            j
            for j in jobs_by_stage.get(stage, [])
            if j.get("pipeline_run_id") == run_id
        ]
        if not jobs:
            if missing_stage is None:
                missing_stage = stage
            continue
        job = jobs[-1]
        if job.get("state") != "SUCCEEDED":
            return stage, job
    if missing_stage is not None:
        # Nothing non-succeeded found; the first missing stage is the blocker.
        return missing_stage, None
    # All stages succeeded.
    return ASSET_PIPELINE_GRAPH[-1], jobs_by_stage.get(ASSET_PIPELINE_GRAPH[-1], [None])[-1]


def get_asset_pipeline_status(
    db_path: Path | str,
    canonical_id: str,
    *,
    now: Optional[str] = None,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
) -> Optional[AssetPipelineStatus]:
    """Derive the pipeline status of one asset (read-only, JSON-safe)."""
    from .models import utc_now_iso

    db_path = Path(db_path)
    now = now or utc_now_iso()
    asset = get_asset_by_canonical_id(db_path, canonical_id)
    if asset is None:
        return None

    runs = list_pipeline_runs(db_path, canonical_id=canonical_id)
    run = _select_run(runs)

    jobs_by_stage: dict[str, list[dict[str, Any]]] = {
        stage: [] for stage in ASSET_PIPELINE_GRAPH
    }
    if run is not None:
        for job in list_jobs(db_path, canonical_id=canonical_id):
            stage = job.get("stage")
            if stage in jobs_by_stage:
                jobs_by_stage[stage].append(job)

    workers = list_workers_with_status(
        db_path,
        stale_threshold_seconds=stale_threshold_seconds,
        now=now,
    )
    invariant_run_ids = _invariant_run_ids(db_path)

    current_stage: Optional[str] = None
    current_job: Optional[dict[str, Any]] = None
    if run is not None:
        current_stage, current_job = _blocking_stage_and_job(run, jobs_by_stage)

    # The "current job" shown is the blocking job of the current run. If the run
    # has no blocking job (None) the current_stage is the first missing stage.
    if current_job is None and run is not None:
        required: tuple[str, ...] = ()
    elif current_job is not None:
        required = tuple(
            _json_loads_list(current_job.get("required_capabilities_json"))
        )
    else:
        required = ()

    lifecycle = asset.get("lifecycle_state", "DISCOVERED")

    # Is any stage strictly before the blocking stage SUCCEEDED? That is the
    # scheduler-crash window between a SUCCEEDED upstream and its downstream.
    upstream_succeeded = False
    if run is not None and current_stage is not None:
        for stage in ASSET_PIPELINE_GRAPH:
            if stage == current_stage:
                break
            jobs = jobs_by_stage.get(stage, [])
            if jobs and jobs[-1].get("state") == "SUCCEEDED":
                upstream_succeeded = True

    health, needs_attention, attention_reason = _classify(
        current_job=current_job,
        current_stage=current_stage,
        run=run,
        asset=asset,
        workers=workers,
        now=now,
        invariant_run_ids=invariant_run_ids,
        stale_threshold_seconds=stale_threshold_seconds,
        upstream_succeeded=upstream_succeeded,
    )

    assigned_worker = None
    worker_stale = None
    if current_job is not None and current_job.get("state") in ("LEASED", "RUNNING"):
        assigned_worker = current_job.get("lease_owner")
        if assigned_worker:
            worker_stale = is_worker_stale(
                db_path,
                assigned_worker,
                stale_threshold_seconds=stale_threshold_seconds,
                now=now,
            )

    last_error_class: Optional[str] = None
    last_error_message: Optional[str] = None
    if current_job is not None:
        failure = _latest_failed_attempt(db_path, current_job["job_id"])
        if failure is not None:
            last_error_class = failure["error_class"]
            last_error_message = failure["error_message"]

    last_event_at = _latest_events_by_asset(db_path).get(canonical_id)
    historically_searchable = lifecycle == "SEARCHABLE" or any(
        r.get("status") == "SUCCEEDED" for r in runs
    )
    current_refresh_failed = historically_searchable and any(
        r.get("status") == "FAILED" for r in runs
    )

    return AssetPipelineStatus(
        canonical_id=asset["canonical_id"],
        platform=asset["platform"],
        platform_content_id=asset["platform_content_id"],
        asset_lifecycle_state=lifecycle,
        current_pipeline_run_id=run["run_id"] if run else None,
        current_pipeline_status=run.get("status") if run else None,
        current_stage=current_stage,
        current_job_id=current_job["job_id"] if current_job else None,
        current_job_state=current_job.get("state") if current_job else None,
        required_capabilities=required,
        matching_worker_count=_matching_worker_count(workers, required),
        assigned_worker=assigned_worker,
        worker_stale=worker_stale,
        attempt_count=current_job.get("attempt_count", 0) if current_job else 0,
        max_attempts=current_job.get("max_attempts", 0) if current_job else 0,
        last_error_class=last_error_class,
        last_error_message=last_error_message,
        next_retry_at=current_job.get("next_retry_at") if current_job else None,
        lease_expires_at=current_job.get("lease_expires_at") if current_job else None,
        last_event_at=last_event_at,
        health=health,
        searchable=lifecycle == "SEARCHABLE",
        historically_searchable=historically_searchable,
        current_refresh_failed=current_refresh_failed,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
    )


def list_asset_pipeline_statuses(
    db_path: Path | str,
    *,
    now: Optional[str] = None,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
) -> list[AssetPipelineStatus]:
    """Derived status for every registered asset (deterministic order)."""
    from .models import utc_now_iso
    from .store import open_operations_store as _open

    db_path = Path(db_path)
    now = now or utc_now_iso()
    conn = _open(db_path)
    try:
        rows = conn.execute("SELECT canonical_id FROM assets ORDER BY canonical_id ASC").fetchall()
        canonical_ids = [r["canonical_id"] for r in rows]
    finally:
        conn.close()
    return [
        s
        for cid in canonical_ids
        if (s := get_asset_pipeline_status(
            db_path, cid, now=now, stale_threshold_seconds=stale_threshold_seconds
        ))
        is not None
    ]


# ----------------------------------------------------------------------
# Worker status
# ----------------------------------------------------------------------


def get_worker_status(
    db_path: Path | str,
    worker_id: str,
    *,
    now: Optional[str] = None,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
) -> Optional[WorkerStatus]:
    """Derive one worker's status including currently owned / running jobs."""
    from .models import utc_now_iso

    db_path = Path(db_path)
    now = now or utc_now_iso()
    for worker in list_workers_with_status(
        db_path, stale_threshold_seconds=stale_threshold_seconds, now=now
    ):
        if worker["worker_id"] != worker_id:
            continue
        owned: list[str] = []
        running: list[str] = []
        for state in ("LEASED", "RUNNING"):
            for job in list_jobs(db_path, state=state):
                if job.get("lease_owner") == worker_id:
                    owned.append(job["job_id"])
                    if job.get("state") == "RUNNING":
                        running.append(job["job_id"])
        online = worker.get("derived_status") == "active"
        return WorkerStatus(
            worker_id=worker["worker_id"],
            display_name=worker.get("display_name"),
            hostname=worker.get("hostname"),
            capabilities=tuple(
                _json_loads_list(worker.get("capabilities_json"))
            ),
            last_heartbeat_at=worker.get("last_heartbeat_at"),
            derived_status=worker.get("derived_status", "stale"),
            online=online,
            currently_owned_jobs=tuple(owned),
            running_jobs=tuple(running),
        )
    return None


def list_worker_statuses(
    db_path: Path | str,
    *,
    now: Optional[str] = None,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
) -> list[WorkerStatus]:
    from .models import utc_now_iso

    db_path = Path(db_path)
    now = now or utc_now_iso()
    workers = list_workers_with_status(
        db_path, stale_threshold_seconds=stale_threshold_seconds, now=now
    )
    owned_by: dict[str, list[str]] = {"leased": {}, "running": {}}
    owned_by["leased"] = {w["worker_id"]: [] for w in workers}
    owned_by["running"] = {w["worker_id"]: [] for w in workers}
    for state in ("LEASED", "RUNNING"):
        for job in list_jobs(db_path, state=state):
            owner = job.get("lease_owner")
            if owner in owned_by["leased"]:
                owned_by["leased"][owner].append(job["job_id"])
                if state == "RUNNING":
                    owned_by["running"][owner].append(job["job_id"])
    result: list[WorkerStatus] = []
    for worker in workers:
        wid = worker["worker_id"]
        result.append(
            WorkerStatus(
                worker_id=wid,
                display_name=worker.get("display_name"),
                hostname=worker.get("hostname"),
                capabilities=tuple(
                    _json_loads_list(worker.get("capabilities_json"))
                ),
                last_heartbeat_at=worker.get("last_heartbeat_at"),
                derived_status=worker.get("derived_status", "stale"),
                online=worker.get("derived_status") == "active",
                currently_owned_jobs=tuple(owned_by["leased"][wid]),
                running_jobs=tuple(owned_by["running"][wid]),
            )
        )
    return result


# ----------------------------------------------------------------------
# Global summary
# ----------------------------------------------------------------------


def compute_operations_summary(
    db_path: Path | str,
    *,
    now: Optional[str] = None,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
    include_validation: bool = False,
) -> OperationsSummary:
    """Lightweight global summary. Full store validation only on demand."""
    from .models import utc_now_iso

    db_path = Path(db_path)
    now = now or utc_now_iso()

    conn = open_operations_store(db_path)
    try:
        assets_total = conn.execute("SELECT count(*) AS n FROM assets").fetchone()["n"]
        searchable = conn.execute(
            "SELECT count(*) AS n FROM assets WHERE lifecycle_state='SEARCHABLE'"
        ).fetchone()["n"]
        active_runs = conn.execute(
            "SELECT count(*) AS n FROM pipeline_runs WHERE status='RUNNING'"
        ).fetchone()["n"]
        job_rows = conn.execute("SELECT state, stage FROM jobs").fetchall()
    finally:
        conn.close()

    jobs_by_state: dict[str, int] = {}
    jobs_by_stage: dict[str, int] = {}
    for row in job_rows:
        jobs_by_state[row["state"]] = jobs_by_state.get(row["state"], 0) + 1
        jobs_by_stage[row["stage"]] = jobs_by_stage.get(row["stage"], 0) + 1

    statuses = list_asset_pipeline_statuses(
        db_path, now=now, stale_threshold_seconds=stale_threshold_seconds
    )
    health_counts: dict[str, int] = {}
    for s in statuses:
        health_counts[s.health] = health_counts.get(s.health, 0) + 1

    workers = list_workers_with_status(
        db_path, stale_threshold_seconds=stale_threshold_seconds, now=now
    )
    workers_online = sum(1 for w in workers if w.get("derived_status") == "active")
    workers_stale = len(workers) - workers_online

    # Earliest future next_retry_at among FAILED_RETRYABLE jobs.
    next_retry_at: Optional[str] = None
    for job in list_jobs(db_path, state="FAILED_RETRYABLE"):
        value = job.get("next_retry_at")
        if value is None:
            continue
        if next_retry_at is None or value < next_retry_at:
            next_retry_at = value

    store_valid: Optional[bool] = None
    if include_validation:
        from .store import validate_operations_store

        store_valid = validate_operations_store(db_path, now=now).valid

    return OperationsSummary(
        assets_total=assets_total,
        searchable_assets=searchable,
        active_pipeline_runs=active_runs,
        jobs_by_state=jobs_by_state,
        jobs_by_stage=jobs_by_stage,
        workers_total=len(workers),
        workers_online=workers_online,
        workers_stale=workers_stale,
        waiting_for_worker=health_counts.get(HEALTH_WAITING_FOR_WORKER, 0),
        waiting_retry=health_counts.get(HEALTH_WAITING_RETRY, 0),
        terminal_failures=health_counts.get(HEALTH_FAILED_TERMINAL, 0),
        stalled_pipelines=health_counts.get(HEALTH_STALLED, 0),
        invariant_errors=health_counts.get(HEALTH_INVARIANT_ERROR, 0),
        next_retry_at=next_retry_at,
        store_valid=store_valid,
    )


# ----------------------------------------------------------------------
# Event timeline
# ----------------------------------------------------------------------


def get_asset_timeline(
    db_path: Path | str,
    canonical_id: str,
    *,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Ordered event timeline for an asset (UTC timestamp + stable event id).

    Every entry is a plain projection of the durable event row: never an LLM
    narrative, never secret fields.
    """
    events = list_events(db_path, canonical_id=canonical_id, limit=limit)
    timeline: list[dict[str, Any]] = []
    for event in events:
        timeline.append(
            {
                "event_id": event["event_rowid"],
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type"),
                "from_state": event.get("from_state"),
                "to_state": event.get("to_state"),
                "job_id": event.get("job_id"),
                "worker_id": event.get("worker_id"),
                "pipeline_run_id": event.get("pipeline_run_id"),
                "message": event.get("message"),
                "metadata": _json_loads(event.get("metadata_json")),
            }
        )
    return timeline


__all__ = [
    "HEALTH_HEALTHY",
    "HEALTH_RUNNING",
    "HEALTH_WAITING_FOR_WORKER",
    "HEALTH_WAITING_RETRY",
    "HEALTH_SUCCEEDED",
    "HEALTH_FAILED_TERMINAL",
    "HEALTH_CANCELLED",
    "HEALTH_STALLED",
    "HEALTH_INVARIANT_ERROR",
    "MANUAL_ATTENTION_HEALTH",
    "AssetPipelineStatus",
    "WorkerStatus",
    "OperationsSummary",
    "get_asset_pipeline_status",
    "list_asset_pipeline_statuses",
    "get_worker_status",
    "list_worker_statuses",
    "compute_operations_summary",
    "get_asset_timeline",
]