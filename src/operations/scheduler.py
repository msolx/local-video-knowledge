"""M6-04: durable control-plane scheduler.

The scheduler is the NAS control-plane owner-side component. It is
**level-triggered / reconciliation-oriented**: every ``run_once`` re-derives
all decisions from the Operations DB and durable StageExecutionResult state,
so a restart mid-cycle converges to the same plan without any in-memory event
chain.

Responsibilities (one deterministic ``run_once`` pass):

1. recover expired leases          (reuse M6-02 recover_expired_leases)
2. requeue due FAILED_RETRYABLE    (reuse M6-01 requeue_retryable_job)
3. process completed DISCOVER results (register assets, create pipeline runs,
                                       enqueue ARCHIVE)
4. reconcile asset pipelines       (advance lifecycle, enqueue missing
                                    downstream stages, finalize runs)
5. schedule DISCOVER poll if due   (deterministic poll generation)

The scheduler never parses ASR / evidence / KnowledgeUnit internals. It only
trusts the StageExecutionResult, the stage-success invariant and the output
fingerprint produced by the M6-03 adapters.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    DEFAULT_OPERATIONS_PATH,
    JobStage,
    PipelineRunStatus,
    TriggerType,
    compute_job_id,
    parse_iso,
    utc_now_iso,
)
from .stages import (
    required_capabilities_for_stage,
)
from .store import (
    complete_pipeline_run,
    create_pipeline_run,
    enqueue_job,
    get_asset,
    get_asset_by_canonical_id,
    get_job,
    get_job_result,
    list_failed_jobs,
    list_jobs,
    list_pipeline_runs,
    open_operations_store,
    recover_expired_leases,
    register_asset,
    requeue_retryable_job,
    set_scheduler_state,
    transition_asset_lifecycle,
)

# ----------------------------------------------------------------------
# Frozen M6-04 scheduler contracts
# ----------------------------------------------------------------------

SCHEDULER_POLICY_VERSION = "m6-scheduler-policy-v1"
SCHEDULER_SCHEMA_VERSION = "m6-scheduler-v1"

# Reserved control-plane identity for source-level DISCOVER polling.
# Never collides with a real Douyin content id (numeric) and never enters the
# canonical asset registry as a business asset.
CONTROL_CONTENT_ID = "__discover__"

# Asset pipeline graph (single-asset downstream chain). DISCOVER is the batch
# producer that yields assets and is therefore NOT part of this per-asset
# chain (see M6-04 decision D34).
ASSET_PIPELINE_GRAPH = (
    JobStage.ARCHIVE.value,
    JobStage.MEDIA_PROCESS.value,
    JobStage.KNOWLEDGE_EXTRACT.value,
    JobStage.KNOWLEDGE_FINALIZE.value,
    JobStage.STORE_INGEST.value,
)

# Successful stage -> lifecycle milestone (KNOWLEDGE_EXTRACT intentionally has
# no milestone: EVIDENCE_READY..KNOWLEDGE_READY has no intermediate state).
STAGE_MILESTONES = {
    JobStage.ARCHIVE.value: "ARCHIVED",
    JobStage.MEDIA_PROCESS.value: "EVIDENCE_READY",
    JobStage.KNOWLEDGE_FINALIZE.value: "KNOWLEDGE_READY",
    JobStage.STORE_INGEST.value: "SEARCHABLE",
}

LIFECYCLE_RANK = {
    "DISCOVERED": 0,
    "ARCHIVED": 1,
    "EVIDENCE_READY": 2,
    "KNOWLEDGE_READY": 3,
    "SEARCHABLE": 4,
}

PROCESSED_DISCOVER_KEY = "processed_discover_jobs"


class SchedulerError(Exception):
    """Raised for scheduler configuration / orchestration violations."""


# ----------------------------------------------------------------------
# Typed cycle result
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerCycleResult:
    """JSON-safe summary of one reconciliation pass."""

    schema_version: str = SCHEDULER_SCHEMA_VERSION
    expired_leases_recovered: int = 0
    retry_jobs_requeued: int = 0
    assets_registered: int = 0
    pipeline_runs_created: int = 0
    jobs_enqueued: int = 0
    lifecycles_advanced: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    polls_scheduled: int = 0
    invariant_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "expired_leases_recovered": self.expired_leases_recovered,
            "retry_jobs_requeued": self.retry_jobs_requeued,
            "assets_registered": self.assets_registered,
            "pipeline_runs_created": self.pipeline_runs_created,
            "jobs_enqueued": self.jobs_enqueued,
            "lifecycles_advanced": self.lifecycles_advanced,
            "runs_completed": self.runs_completed,
            "runs_failed": self.runs_failed,
            "polls_scheduled": self.polls_scheduled,
            "invariant_failures": self.invariant_failures,
        }


# ----------------------------------------------------------------------
# Fingerprints / deterministic poll generation
# ----------------------------------------------------------------------


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def discovery_control_fingerprint(
    platform: str,
    content_id: str,
    *,
    generation: int = 0,
    policy_version: str = SCHEDULER_POLICY_VERSION,
) -> str:
    """Per-asset deterministic control fingerprint used as ARCHIVE input.

    Stable across repeated polls of the same asset (=> enqueue idempotency,
    no re-download). ``generation`` is bumped only when a prior ARCHIVE job is
    terminal so a later discovery can start a fresh attempt. This is a
    control-plane identity, NOT an artifact/knowledge identity.
    """
    return _sha256_text(
        f"discover|{platform}|{content_id}|gen:{generation}|{policy_version}"
    )


def poll_slot_fingerprint(
    platform: str,
    *,
    poll_slot: int,
    source_key: str,
    policy_version: str = SCHEDULER_POLICY_VERSION,
) -> str:
    """Deterministic DISCOVER input fingerprint for a poll slot.

    ``poll_slot`` is a scheduler trigger identity (floor(now / interval)),
    never a knowledge/artifact identity. A new slot yields a new job
    generation; re-scheduling the same slot yields the identical job id.
    """
    return _sha256_text(
        f"discover|{platform}|{source_key}|slot:{poll_slot}|{policy_version}"
    )


def epoch_seconds(now: str) -> int:
    dt = datetime.fromisoformat(now)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _add_seconds(now: str, seconds: int) -> str:
    dt = datetime.fromisoformat(now)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=seconds)).isoformat()


@dataclass
class PollSource:
    """A scheduled collection source the scheduler polls."""

    platform: str = "douyin"
    source_key: str = "douyin"
    interval_seconds: int = 3600

    @property
    def scheduler_key(self) -> str:
        return f"discover:{self.platform}:{self.source_key}"

    @property
    def control_canonical_id(self) -> str:
        return f"control_{self.platform}_{self.source_key}_discover"


class Scheduler:
    """Reconciliation-oriented control plane.

    ``run_once(now)`` performs one deterministic pass. ``run_forever`` loops
    over ``run_once`` using only the standard library (threading.Event).
    All time-dependent decisions use the injected ``now`` (UTC ISO) for
    deterministic, sleep-free testing.
    """

    def __init__(
        self,
        store_path: Path | str = DEFAULT_OPERATIONS_PATH,
        *,
        poll_sources: Optional[list[PollSource]] = None,
        policy_version: str = SCHEDULER_POLICY_VERSION,
        now: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.store_path = Path(store_path)
        self.policy_version = policy_version
        self.now = now
        self.poll_sources = list(poll_sources or [PollSource()])
        open_operations_store(self.store_path)

    # -- run_once / run_forever -------------------------------------------

    def run_once(self, now: Optional[str] = None) -> SchedulerCycleResult:
        """One deterministic reconciliation pass (frozen phase order)."""
        db_path = self.store_path
        now = now or self.now()
        counts: dict[str, int] = {
            "expired_leases_recovered": 0,
            "retry_jobs_requeued": 0,
            "assets_registered": 0,
            "pipeline_runs_created": 0,
            "jobs_enqueued": 0,
            "lifecycles_advanced": 0,
            "runs_completed": 0,
            "runs_failed": 0,
            "polls_scheduled": 0,
            "invariant_failures": 0,
        }

        # Phase 1: recover expired leases.
        counts["expired_leases_recovered"] = self._recover_expired_leases(
            db_path, now
        )
        # Phase 2: requeue due FAILED_RETRYABLE jobs.
        counts["retry_jobs_requeued"] = self._requeue_due_retryable(db_path, now)
        # Phase 3: process completed DISCOVER results.
        self._process_discover_results(db_path, now, counts)
        # Phase 4: reconcile asset pipelines (downstream enqueue + lifecycle
        # + run finalization).
        self._reconcile_runs(db_path, now, counts)
        # Phase 5: schedule DISCOVER poll if due.
        self._schedule_polls(db_path, now, counts)
        return SchedulerCycleResult(
            schema_version=self.policy_result_schema(),
            **counts,
        )

    def policy_result_schema(self) -> str:
        return SCHEDULER_SCHEMA_VERSION

    def run_forever(
        self,
        poll_interval_seconds: float,
        stop_event: Optional[threading.Event] = None,
        *,
        max_cycles: Optional[int] = None,
    ) -> int:
        """Loop run_once until stop_event is set (or max_cycles reached)."""
        stop = stop_event or threading.Event()
        cycles = 0
        while not stop.is_set():
            self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            stop.wait(poll_interval_seconds)
        return cycles

    # -- event helpers ----------------------------------------------------

    def _append_event(
        self,
        db_path: Path,
        event_type: str,
        *,
        canonical_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> None:
        from .store import _append_event as _raw_append

        conn = open_operations_store(db_path)
        try:
            _raw_append(
                conn,
                event_type,
                timestamp=now or utc_now_iso(),
                canonical_id=canonical_id,
                pipeline_run_id=pipeline_run_id,
                job_id=job_id,
                message=message,
                metadata=metadata,
            )
            conn.commit()
        finally:
            conn.close()

    def _state_metadata(self, state: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Parse the scheduler_state.metadata_json column (a JSON string)."""
        if state is None:
            return {}
        raw = state.get("metadata_json")
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str) and raw:
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return {}
        return {}

    # -- phase 1: lease recovery ------------------------------------------

    def _recover_expired_leases(self, db_path: Path, now: str) -> int:
        recovered = recover_expired_leases(db_path, now=now)
        return len(recovered.get("recovered_leased", [])) + len(
            recovered.get("recovered_running", [])
        )

    # -- phase 2: retry requeue -------------------------------------------

    def _requeue_due_retryable(self, db_path: Path, now: str) -> int:
        requeued = 0
        for job in list_failed_jobs(db_path):
            if job.get("state") != "FAILED_RETRYABLE":
                continue
            next_retry_at = job.get("next_retry_at")
            attempt_count = job.get("attempt_count", 0)
            max_attempts = job.get("max_attempts", 5)
            if next_retry_at is not None and parse_iso(next_retry_at) > parse_iso(now):
                continue
            if attempt_count >= max_attempts:
                continue
            requeue_retryable_job(
                db_path, job["job_id"], reason="scheduler_retry_due", now=now
            )
            self._append_event(
                db_path,
                "scheduler_retry_requeued",
                canonical_id=job.get("canonical_id"),
                pipeline_run_id=job.get("pipeline_run_id"),
                job_id=job["job_id"],
                message="retryable job requeued by scheduler",
                now=now,
            )
            requeued += 1
        return requeued

    # -- phase 3: DISCOVER result processing ------------------------------

    def _ensure_control_asset(self, db_path: Path, source: PollSource, now: str) -> None:
        existing = get_asset(db_path, source.platform, CONTROL_CONTENT_ID)
        if existing is None:
            register_asset(
                db_path,
                source.platform,
                CONTROL_CONTENT_ID,
                source.control_canonical_id,
                metadata={"control_plane": True, "source_key": source.source_key},
                now=now,
            )

    def _process_discover_results(
        self, db_path: Path, now: str, counts: dict[str, int]
    ) -> None:
        from .store import get_scheduler_state as _get_state

        for source in self.poll_sources:
            control_cid = source.control_canonical_id
            state = _get_state(db_path, source.scheduler_key)
            processed = list(
                self._state_metadata(state).get(PROCESSED_DISCOVER_KEY, [])
            )

            succeeded = [
                j
                for j in list_jobs(
                    db_path, stage=JobStage.DISCOVER.value, canonical_id=control_cid
                )
                if j.get("state") == "SUCCEEDED" and j.get("job_id") not in processed
            ]
            for djob in succeeded:
                result = get_job_result(db_path, djob["job_id"])
                if result is None or not isinstance(result, dict):
                    counts["invariant_failures"] += 1
                    self._append_event(
                        db_path,
                        "orchestration_invariant_failure",
                        job_id=djob["job_id"],
                        message="DISCOVER SUCCEEDED but durable result missing",
                        now=now,
                    )
                    processed.append(djob["job_id"])
                    continue
                discovered = (result.get("metadata") or {}).get("discovered", []) or []
                self._handle_discovered(source, djob, discovered, now, counts)
                processed.append(djob["job_id"])

            if processed:
                set_scheduler_state(
                    db_path,
                    source.scheduler_key,
                    metadata={PROCESSED_DISCOVER_KEY: processed},
                    now=now,
                )

    def _handle_discovered(
        self,
        source: PollSource,
        discover_job: dict[str, Any],
        discovered: list[Any],
        now: str,
        counts: dict[str, int],
    ) -> None:
        db_path = self.store_path
        for item in discovered:
            platform, content_id = self._identity_from_item(source, item)
            if not content_id:
                continue
            content_id = str(content_id)
            canonical_id = f"{platform}_{content_id}"

            if get_asset(db_path, platform, content_id) is None:
                register_asset(
                    db_path,
                    platform,
                    content_id,
                    canonical_id,
                    metadata={"discovered_by_job": discover_job["job_id"]},
                    now=now,
                )
                counts["assets_registered"] += 1
                self._append_event(
                    db_path,
                    "scheduler_assets_registered",
                    canonical_id=canonical_id,
                    job_id=discover_job["job_id"],
                    message=f"asset registered from DISCOVER result: {canonical_id}",
                    now=now,
                )

            arch_jobs = list_jobs(
                db_path, stage=JobStage.ARCHIVE.value, canonical_id=canonical_id
            )
            latest_archive = arch_jobs[-1] if arch_jobs else None
            generation = self._next_discovery_generation(latest_archive)
            arch_input_fp = discovery_control_fingerprint(
                platform,
                content_id,
                generation=generation,
                policy_version=self.policy_version,
            )
            existing = get_job(
                db_path,
                compute_job_id(
                    platform,
                    content_id,
                    JobStage.ARCHIVE.value,
                    arch_input_fp,
                    self.policy_version,
                ),
            )
            if existing is not None:
                continue

            run = self._active_run_or_create(db_path, canonical_id, now, counts)
            enq = enqueue_job(
                db_path,
                platform,
                content_id,
                JobStage.ARCHIVE.value,
                arch_input_fp,
                policy_version=self.policy_version,
                canonical_id=canonical_id,
                pipeline_run_id=run["run_id"],
                required_capabilities=list(
                    required_capabilities_for_stage(JobStage.ARCHIVE.value)
                ),
                metadata={
                    "discover_job_id": discover_job["job_id"],
                    "discovery_generation": generation,
                },
                now=now,
            )
            if enq.created:
                counts["jobs_enqueued"] += 1
                self._append_event(
                    db_path,
                    "scheduler_downstream_enqueued",
                    canonical_id=canonical_id,
                    pipeline_run_id=run["run_id"],
                    job_id=enq.job_id,
                    message=f"ARCHIVE enqueued for {canonical_id}",
                    now=now,
                )

    @staticmethod
    def _identity_from_item(
        source: PollSource, item: Any
    ) -> tuple[str, Optional[str]]:
        if isinstance(item, dict):
            platform = item.get("platform") or source.platform
            content_id = item.get("platform_content_id") or item.get("content_id")
        else:
            platform = source.platform
            content_id = item
        return platform, content_id

    @staticmethod
    def _next_discovery_generation(
        latest_archive: Optional[dict[str, Any]],
    ) -> int:
        if latest_archive is None:
            return 0
        if latest_archive.get("state") not in ("FAILED_TERMINAL", "CANCELLED"):
            return 0
        meta = latest_archive.get("metadata_json")
        if isinstance(meta, str) and meta:
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return int(meta.get("discovery_generation", 0)) + 1

    def _active_run_or_create(
        self, db_path: Path, canonical_id: str, now: str, counts: dict[str, int]
    ) -> dict[str, Any]:
        runs = list_pipeline_runs(db_path, canonical_id=canonical_id, status="RUNNING")
        if runs:
            return runs[0]
        run = create_pipeline_run(
            db_path,
            canonical_id,
            TriggerType.DISCOVERY.value,
            metadata={"scheduler": True},
            now=now,
        )
        counts["pipeline_runs_created"] += 1
        self._append_event(
            db_path,
            "scheduler_pipeline_run_created",
            canonical_id=canonical_id,
            pipeline_run_id=run["run_id"],
            message=f"pipeline run created for {canonical_id}",
            now=now,
        )
        return run

    # -- phase 4: reconcile asset pipelines --------------------------------

    def _reconcile_runs(
        self, db_path: Path, now: str, counts: dict[str, int]
    ) -> None:
        for run in list_pipeline_runs(db_path, status="RUNNING"):
            canonical_id = run.get("canonical_id")
            if not canonical_id or canonical_id.startswith("control_"):
                continue
            asset = get_asset_by_canonical_id(db_path, canonical_id)
            if asset is None:
                continue
            self._reconcile_run(db_path, run, asset, now, counts)

    def _reconcile_run(
        self,
        db_path: Path,
        run: dict[str, Any],
        asset: dict[str, Any],
        now: str,
        counts: dict[str, int],
    ) -> None:
        run_id = run["run_id"]
        canonical_id = asset["canonical_id"]
        platform = asset["platform"]
        content_id = asset["platform_content_id"]

        for stage in ASSET_PIPELINE_GRAPH:
            job = self._run_stage_job(db_path, run_id, canonical_id, stage)
            if job is None:
                break
            state = job.get("state")
            if state in ("QUEUED", "LEASED", "RUNNING"):
                break
            if state == "FAILED_RETRYABLE":
                break
            if state == "FAILED_TERMINAL":
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.FAILED.value, now=now
                )
                counts["runs_failed"] += 1
                self._append_event(
                    db_path,
                    "scheduler_run_failed",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"pipeline run FAILED at {stage}",
                    now=now,
                )
                return
            if state == "CANCELLED":
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.CANCELLED.value, now=now
                )
                self._append_event(
                    db_path,
                    "scheduler_run_cancelled",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"pipeline run CANCELLED at {stage}",
                    now=now,
                )
                return
            if state != "SUCCEEDED":
                continue

            result = get_job_result(db_path, job["job_id"])
            if result is None or not isinstance(result, dict):
                counts["invariant_failures"] += 1
                self._append_event(
                    db_path,
                    "orchestration_invariant_failure",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"{stage} SUCCEEDED but durable result missing",
                    now=now,
                )
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.FAILED.value, now=now
                )
                counts["runs_failed"] += 1
                return

            milestone = STAGE_MILESTONES.get(stage)
            if milestone is not None:
                current = asset.get("lifecycle_state", "DISCOVERED")
                if LIFECYCLE_RANK.get(milestone, 0) > LIFECYCLE_RANK.get(current, 0):
                    transition_asset_lifecycle(
                        db_path, canonical_id, milestone, now=now
                    )
                    counts["lifecycles_advanced"] += 1
                    self._append_event(
                        db_path,
                        "scheduler_lifecycle_advanced",
                        canonical_id=canonical_id,
                        pipeline_run_id=run_id,
                        job_id=job["job_id"],
                        message=f"asset lifecycle advanced to {milestone}",
                        now=now,
                    )

            if stage == JobStage.STORE_INGEST.value:
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.SUCCEEDED.value, now=now
                )
                counts["runs_completed"] += 1
                self._append_event(
                    db_path,
                    "scheduler_run_completed",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"pipeline run SUCCEEDED: {canonical_id}",
                    now=now,
                )
                return

            next_stage = ASSET_PIPELINE_GRAPH[
                ASSET_PIPELINE_GRAPH.index(stage) + 1
            ]
            if (
                self._run_stage_job(db_path, run_id, canonical_id, next_stage)
                is not None
            ):
                continue
            output_fp = result.get("output_fingerprint")
            if not output_fp:
                counts["invariant_failures"] += 1
                self._append_event(
                    db_path,
                    "orchestration_invariant_failure",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"{stage} result missing output_fingerprint",
                    now=now,
                )
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.FAILED.value, now=now
                )
                counts["runs_failed"] += 1
                return
            media_type = (result.get("metadata") or {}).get("media_type")
            try:
                caps = list(
                    required_capabilities_for_stage(next_stage, media_type=media_type)
                )
            except ValueError as exc:
                counts["invariant_failures"] += 1
                self._append_event(
                    db_path,
                    "orchestration_invariant_failure",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=job["job_id"],
                    message=f"cannot route {next_stage}: {exc}",
                    now=now,
                )
                complete_pipeline_run(
                    db_path, run_id, PipelineRunStatus.FAILED.value, now=now
                )
                counts["runs_failed"] += 1
                return

            enq = enqueue_job(
                db_path,
                platform,
                content_id,
                next_stage,
                output_fp,
                policy_version=self.policy_version,
                canonical_id=canonical_id,
                pipeline_run_id=run_id,
                required_capabilities=caps,
                metadata=(
                    {"media_type": media_type}
                    if next_stage == JobStage.MEDIA_PROCESS.value
                    else None
                ),
                now=now,
            )
            if enq.created:
                counts["jobs_enqueued"] += 1
                self._append_event(
                    db_path,
                    "scheduler_downstream_enqueued",
                    canonical_id=canonical_id,
                    pipeline_run_id=run_id,
                    job_id=enq.job_id,
                    message=f"{next_stage} enqueued for {canonical_id}",
                    now=now,
                )
            return

    def _run_stage_job(
        self, db_path: Path, run_id: str, canonical_id: str, stage: str
    ) -> Optional[dict[str, Any]]:
        jobs = [
            j
            for j in list_jobs(db_path, stage=stage, canonical_id=canonical_id)
            if j.get("pipeline_run_id") == run_id
        ]
        return jobs[-1] if jobs else None

    # -- phase 5: poll scheduling -----------------------------------------

    def _schedule_polls(
        self, db_path: Path, now: str, counts: dict[str, int]
    ) -> None:
        from .store import get_scheduler_state as _get_state

        for source in self.poll_sources:
            state = _get_state(db_path, source.scheduler_key)
            next_due_at = state.get("next_due_at") if state is not None else None
            if next_due_at is not None and now < next_due_at:
                continue

            active = [
                j
                for j in list_jobs(
                    db_path,
                    stage=JobStage.DISCOVER.value,
                    canonical_id=source.control_canonical_id,
                )
                if j.get("state") in ("QUEUED", "LEASED", "RUNNING")
            ]
            if active:
                set_scheduler_state(
                    db_path,
                    source.scheduler_key,
                    last_scheduled_at=now,
                    next_due_at=_add_seconds(now, source.interval_seconds),
                    now=now,
                )
                continue

            self._ensure_control_asset(db_path, source, now)
            poll_slot = epoch_seconds(now) // source.interval_seconds
            input_fp = poll_slot_fingerprint(
                source.platform,
                poll_slot=poll_slot,
                source_key=source.source_key,
                policy_version=self.policy_version,
            )
            enq = enqueue_job(
                db_path,
                source.platform,
                CONTROL_CONTENT_ID,
                JobStage.DISCOVER.value,
                input_fp,
                policy_version=self.policy_version,
                canonical_id=source.control_canonical_id,
                required_capabilities=list(
                    required_capabilities_for_stage(JobStage.DISCOVER.value)
                ),
                metadata={"source_key": source.source_key, "poll_slot": poll_slot},
                now=now,
            )
            if enq.created:
                counts["polls_scheduled"] += 1
                self._append_event(
                    db_path,
                    "scheduler_poll_scheduled",
                    canonical_id=source.control_canonical_id,
                    job_id=enq.job_id,
                    message=(
                        f"DISCOVER poll scheduled for {source.scheduler_key} "
                        f"slot={poll_slot}"
                    ),
                    now=now,
                )
            set_scheduler_state(
                db_path,
                source.scheduler_key,
                last_scheduled_at=now,
                next_due_at=_add_seconds(now, source.interval_seconds),
                metadata={"last_poll_job_id": enq.job_id, "poll_slot": poll_slot},
                now=now,
            )