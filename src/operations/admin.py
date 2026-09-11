"""M6-05 explicit recovery / admin mutations over the Operations store.

Division of labor (frozen M6-05):

  observability.py  : read-only diagnostics / health projection
  admin.py          : explicit recovery + admin mutations (never read-only)
  cli.py            : thin presentation layer over both

Every admin mutation reuses the sealed store primitives (recover_expired_leases,
requeue_retryable_job, cancel_job, scheduler reconciliation, ...). Nothing here
re-implements recovery rules; ``run_recovery_pass`` composes the existing rules
into a control-plane startup / manual-repair pass.

Secrets policy: admin operations accept and echo job/stage/worker identities and
error classes/messages only. No lease token, cookie, credential or API key is
ever accepted, returned or logged here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import (
    DEFAULT_OPERATIONS_PATH,
    JobState,
    JobStage,
    PipelineRunStatus,
    TriggerType,
    compute_job_id,
    utc_now_iso,
)
from .scheduler import Scheduler, SchedulerError
from .stages import required_capabilities_for_stage
from .store import (
    cancel_job as _cancel_job,
    complete_pipeline_run,
    create_pipeline_run,
    enqueue_job,
    get_asset,
    get_job,
    list_failed_jobs,
    open_operations_store,
    recover_expired_leases,
    requeue_retryable_job,
    validate_operations_store,
)

__all__ = [
    "ADMIN_POLICY_VERSION",
    "RecoveryResult",
    "AdminRetryResult",
    "AdminCancelResult",
    "run_recovery_pass",
    "startup_recovery",
    "admin_retry_job",
    "admin_cancel_job",
    "admin_requeue_asset",
]

ADMIN_POLICY_VERSION = "m6-admin-policy-v1"
RECOVERY_RESULT_SCHEMA_VERSION = "m6-recovery-result-v1"


class AdminError(Exception):
    """Raised for invalid admin operations (never for transient failures)."""


# ----------------------------------------------------------------------
# Frozen result models
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryResult:
    """JSON-safe summary of one control-plane recovery pass."""

    schema_version: str = RECOVERY_RESULT_SCHEMA_VERSION
    expired_leases_recovered: int = 0
    running_attempts_closed: int = 0
    retry_jobs_requeued: int = 0
    scheduler_cycles_run: int = 0
    lifecycles_advanced: int = 0
    jobs_enqueued: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    invariant_failures: int = 0
    store_valid: Optional[bool] = None
    store_violation_count: int = 0
    poll_cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "expired_leases_recovered": self.expired_leases_recovered,
            "running_attempts_closed": self.running_attempts_closed,
            "retry_jobs_requeued": self.retry_jobs_requeued,
            "scheduler_cycles_run": self.scheduler_cycles_run,
            "lifecycles_advanced": self.lifecycles_advanced,
            "jobs_enqueued": self.jobs_enqueued,
            "runs_completed": self.runs_completed,
            "runs_failed": self.runs_failed,
            "invariant_failures": self.invariant_failures,
            "store_valid": self.store_valid,
            "store_violation_count": self.store_violation_count,
            "poll_cycles": self.poll_cycles,
        }


@dataclass(frozen=True)
class AdminRetryResult:
    """Result of an explicit admin ``retry <job_id>``.

    outcome is one of:
      - requeued                       : FAILED_RETRYABLE requeued (QUEUED)
      - already_active                 : job QUEUED/LEASED/RUNNING, no-op
      - already_succeeded              : job already SUCCEEDED, no-op
      - waiting_backoff                : FAILED_RETRYABLE but next_retry_at in the
                                        future; non-force admin respects the backoff
      - attempts_exhausted             : FAILED_RETRYABLE but attempt >= max_attempts;
                                        requires force + new fingerprint override
      - terminal_requires_override     : FAILED_TERMINAL; requires force + reason +
                                        new_input_fingerprint to start a new generation
      - cancelled_no_retry             : CANCELLED; requires force + override to retry
      - new_generation_enqueued        : terminal/cancelled/exhausted overridden with a
                                        new input fingerprint + new pipeline run
      - not_found                      : unknown job_id
    """

    job_id: str
    outcome: str
    message: str
    requeued_job_id: Optional[str] = None
    new_generation_job_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "outcome": self.outcome,
            "message": self.message,
            "requeued_job_id": self.requeued_job_id,
            "new_generation_job_id": self.new_generation_job_id,
        }


@dataclass(frozen=True)
class AdminCancelResult:
    """Result of ``admin cancel <job_id>`` (wraps the frozen cancel_job)."""

    job_id: str
    outcome: str  # "cancelled" | "noop" | "not_found"
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "outcome": self.outcome,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "message": self.message,
        }


# ----------------------------------------------------------------------
# Recovery pass
# ----------------------------------------------------------------------


def run_recovery_pass(
    db_path: Path | str = DEFAULT_OPERATIONS_PATH,
    *,
    now: Optional[str] = None,
    scheduler: Optional[Scheduler] = None,
    run_poll: bool = False,
    poll_sources=None,
    poll_interval_seconds: float = 60.0,
) -> RecoveryResult:
    """One control-plane recovery pass, composing the frozen recovery rules.

    Order (M6-05 §14 startup recovery):
      1. validate_operations_store
      2. recover expired leases (frozen recover_expired_leases)
      3. requeue due retryables (scheduler phase 2)
      4. scheduler reconciliation (scheduler run_once, poll_sources=[] so no
         DISCOVER poll is emitted unless run_poll=True)
      5. optional poll scheduling pass (run_poll=True -> full scheduler with
         the caller's poll_sources)

    Every step is idempotent and safe to call repeatedly (repair command).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()

    validation = validate_operations_store(db_path, now=now)

    # Step 2: recover expired leases (returns detail we surface in the report).
    recovery = recover_expired_leases(db_path, now=now)
    expired = len(recovery.get("recovered_leased", [])) + len(
        recovery.get("recovered_running", [])
    )
    running_closed = len(recovery.get("recovered_running", [])) + len(
        recovery.get("terminal", [])
    )

    # Steps 3+4: scheduler reconciliation without poll scheduling (phase 2
    # requeues due retryables; phase 4 advances pipelines). Reuse the scheduler
    # so retry/requeue and downstream-enqueue rules are not re-implemented.
    sched = scheduler or Scheduler(
        db_path, poll_sources=[], now=lambda: now
    )
    cycle = sched.run_once(now=now)

    # Step 5 (optional): a full scheduler pass including DISCOVER polls.
    poll_cycles = 0
    if run_poll:
        if poll_sources is None:
            poll_sources = []
        poll_sched = scheduler or Scheduler(
            db_path, poll_sources=poll_sources, now=lambda: now
        )
        poll_sched.run_once(now=now)
        poll_cycles = 1

    return RecoveryResult(
        expired_leases_recovered=expired,
        running_attempts_closed=running_closed,
        retry_jobs_requeued=cycle.retry_jobs_requeued,
        scheduler_cycles_run=1,
        lifecycles_advanced=cycle.lifecycles_advanced,
        jobs_enqueued=cycle.jobs_enqueued,
        runs_completed=cycle.runs_completed,
        runs_failed=cycle.runs_failed,
        invariant_failures=cycle.invariant_failures,
        store_valid=validation.valid,
        store_violation_count=len(validation.violations),
        poll_cycles=poll_cycles,
    )


def startup_recovery(
    db_path: Path | str = DEFAULT_OPERATIONS_PATH,
    *,
    now: Optional[str] = None,
    scheduler: Optional[Scheduler] = None,
) -> RecoveryResult:
    """Control-plane startup recovery (M6-05 §14).

    Same as run_recovery_pass but never schedules a poll — polling is left to
    the normal scheduler loop after startup.
    """
    return run_recovery_pass(db_path, now=now, scheduler=scheduler, run_poll=False)


# ----------------------------------------------------------------------
# Admin retry / cancel
# ----------------------------------------------------------------------


def _is_valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def admin_retry_job(
    db_path: Path | str,
    job_id: str,
    *,
    force: bool = False,
    reason: Optional[str] = None,
    new_input_fingerprint: Optional[str] = None,
    now: Optional[str] = None,
) -> AdminRetryResult:
    """Explicit admin retry with the frozen retry policy (M6-05 §28).

    - FAILED_RETRYABLE (attempt < max): requeued when due; ``force=True``
      ignores the backoff timer. Never resurrects exhausted jobs silently.
    - FAILED_TERMINAL / CANCELLED / exhausted: rejected unless ``force=True``
      **and** an explicit ``reason`` **and** a ``new_input_fingerprint``; in that
      case a new pipeline generation is started (new job identity, new run).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job = get_job(db_path, job_id)
    if job is None:
        return AdminRetryResult(
            job_id=job_id, outcome="not_found", message=f"job {job_id} not found"
        )

    state = job.get("state")
    stage = job.get("stage")
    attempt_count = job.get("attempt_count", 0)
    max_attempts = job.get("max_attempts", 0)

    if state in ("QUEUED", "LEASED", "RUNNING"):
        return AdminRetryResult(
            job_id=job_id,
            outcome="already_active",
            message=f"job {job_id} is {state}; nothing to retry",
        )
    if state == "SUCCEEDED":
        return AdminRetryResult(
            job_id=job_id,
            outcome="already_succeeded",
            message=f"job {job_id} already SUCCEEDED; nothing to retry",
        )

    # FAILED_RETRYABLE normal path.
    if state == "FAILED_RETRYABLE":
        if attempt_count >= max_attempts:
            # Exhausted: not a plain requeue; requires override below.
            pass
        else:
            next_retry_at = job.get("next_retry_at")
            if next_retry_at is not None and next_retry_at > now and not force:
                return AdminRetryResult(
                    job_id=job_id,
                    outcome="waiting_backoff",
                    message=(
                        f"job {job_id} FAILED_RETRYABLE, next retry at "
                        f"{next_retry_at}; use --force to requeue now"
                    ),
                )
            requeue_retryable_job(
                db_path, job_id, reason=reason or "admin_retry", now=now
            )
            return AdminRetryResult(
                job_id=job_id,
                outcome="requeued",
                message=f"job {job_id} requeued (QUEUED)",
                requeued_job_id=job_id,
            )

    # Terminal / cancelled / exhausted path: require explicit override.
    if not (force and reason and new_input_fingerprint):
        if state == "FAILED_TERMINAL":
            return AdminRetryResult(
                job_id=job_id,
                outcome="terminal_requires_override",
                message=(
                    f"job {job_id} is FAILED_TERMINAL; retry requires "
                    f"--force --reason <reason> --input-fingerprint <sha256> "
                    f"to start a new pipeline generation"
                ),
            )
        if state == "CANCELLED":
            return AdminRetryResult(
                job_id=job_id,
                outcome="cancelled_no_retry",
                message=f"job {job_id} is CANCELLED; not silently resurrected",
            )
        return AdminRetryResult(
            job_id=job_id,
            outcome="attempts_exhausted",
            message=(
                f"job {job_id} attempts exhausted "
                f"({attempt_count}/{max_attempts}); retry requires --force "
                f"--reason <reason> --input-fingerprint <sha256>"
            ),
        )

    if not _is_valid_sha256(new_input_fingerprint):
        return AdminRetryResult(
            job_id=job_id,
            outcome="terminal_requires_override",
            message="new_input_fingerprint must be a lowercase 64-char sha256 hex digest",
        )

    # Start a new pipeline generation with a new input fingerprint + new run.
    canonical_id = job.get("canonical_id")
    platform = job.get("platform")
    content_id = job.get("platform_content_id")
    policy_version = job.get("policy_version")
    new_job_id = compute_job_id(
        platform, content_id, stage, new_input_fingerprint, policy_version
    )
    existing = get_job(db_path, new_job_id)
    if existing is not None:
        return AdminRetryResult(
            job_id=job_id,
            outcome="new_generation_enqueued",
            message=f"new generation job {new_job_id} already exists",
            new_generation_job_id=new_job_id,
        )
    run = create_pipeline_run(
        db_path,
        canonical_id,
        TriggerType.RECOVERY.value,
        metadata={"admin_retry": True, "from_job": job_id, "reason": reason},
        now=now,
    )
    caps = required_capabilities_for_stage(stage)
    enq = enqueue_job(
        db_path,
        platform,
        content_id,
        stage,
        new_input_fingerprint,
        policy_version=policy_version,
        canonical_id=canonical_id,
        pipeline_run_id=run["run_id"],
        required_capabilities=list(caps),
        metadata={"admin_retry": True, "from_job": job_id, "reason": reason},
        now=now,
    )
    return AdminRetryResult(
        job_id=job_id,
        outcome="new_generation_enqueued",
        message=(
            f"new pipeline generation enqueued as {enq.job_id} "
            f"(run {run['run_id']}, reason={reason!r})"
        ),
        new_generation_job_id=enq.job_id,
    )


def admin_cancel_job(
    db_path: Path | str,
    job_id: str,
    *,
    reason: Optional[str] = None,
    now: Optional[str] = None,
) -> AdminCancelResult:
    """Explicit admin cancel (wraps the frozen cancel_job primitive)."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job = get_job(db_path, job_id)
    if job is None:
        return AdminCancelResult(
            job_id=job_id, outcome="not_found", message=f"job {job_id} not found"
        )
    from_state = job.get("state")
    result = _cancel_job(db_path, job_id, reason=reason, now=now)
    to_state = result.get("state") if isinstance(result, dict) else None
    if to_state == "CANCELLED":
        return AdminCancelResult(
            job_id=job_id,
            outcome="cancelled",
            from_state=from_state,
            to_state=to_state,
            message=f"job {job_id} cancelled",
        )
    return AdminCancelResult(
        job_id=job_id,
        outcome="noop",
        from_state=from_state,
        to_state=to_state,
        message=f"job {job_id} not cancellable from {from_state}",
    )


def admin_requeue_asset(
    db_path: Path | str,
    canonical_id: str,
    to_state: str,
    *,
    reason: str,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Explicit admin asset-lifecycle override (wraps the frozen primitive).

    This must never be used by the normal pipeline path; it is for explicit
    admin reconciliation only (frozen M6-01). Prefer observability's
    historically_searchable/current_refresh_failed projection over ever
    stepping a SEARCHABLE asset backwards.
    """
    from .store import admin_requeue_asset_lifecycle

    return admin_requeue_asset_lifecycle(
        db_path, canonical_id, to_state, reason=reason, now=now
    )