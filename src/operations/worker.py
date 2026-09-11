"""M6-02 worker runtime — capability-aware claim loop, lease fencing, and
injected stage handlers.

Execution model
---------------
The M6 v1 runtime provides **at-least-once** execution semantics (durable
queue + atomic claim + lease + fencing token + idempotent stage execution),
NOT exactly-once. A worker may complete external side effects and then crash
before committing job SUCCEEDED; the lease expires and the job is retried.
Exactly-once is impossible under crash-between-effect-and-commit; M6-03 stage
adapters must keep artifact idempotency so a retried stage is safe.

Ownership rule
--------------
Every mutation of an active job commits (job_id, worker_id, lease_token).
The lease token is the fence: a stale token is rejected even if the worker_id
matches, because the same worker process may later reclaim the same job.

This module deliberately does NOT import src.collector / src.downloader /
M3 processors / M4 extraction / M5 ingest. Real stage adapters arrive in
M6-03; M6-02 uses injected callables.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    ClaimedJob,
    JobStage,
    normalize_capabilities,
    normalize_identity_component,
    utc_now_iso,
)
from .store import (
    StaleLeaseError,
)
from .transport import (
    LocalSQLiteWorkerTransport,
    RemoteUnavailableError,
    WorkerOperationsTransport,
)

__all__ = [
    "RetryableJobError",
    "TerminalJobError",
    "HandlerResult",
    "WorkerRunResult",
    "WorkerRuntime",
    "StageHandler",
]


def _is_valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _coerce_stage_result(result: Any, claimed: ClaimedJob) -> HandlerResult:
    """M6-03: coerce a typed StageExecutionResult returned by a handler into a
    HandlerResult whose metadata carries the JSON-safe stage result, after an
    identity audit against the claimed job.

    Identity audit (M6-03 §28): the stage result's stage, canonical_id and
    input_fingerprint must equal the job's; output_fingerprint must be a valid
    SHA-256; everything must be JSON-serializable. Any violation is terminal.
    """
    to_dict = getattr(result, "to_dict", None)
    if not callable(to_dict):
        raise TerminalJobError(
            f"handler for {claimed.stage!r} returned unsupported type "
            f"{type(result).__name__}"
        )
    payload = to_dict()
    if not isinstance(payload, dict):
        raise TerminalJobError(
            f"handler for {claimed.stage!r} returned non-dict stage result"
        )
    if payload.get("stage") != claimed.stage:
        raise TerminalJobError(
            f"stage result stage {payload.get('stage')!r} != job stage "
            f"{claimed.stage!r}"
        )
    if payload.get("canonical_id") != claimed.canonical_id:
        raise TerminalJobError(
            f"stage result canonical_id {payload.get('canonical_id')!r} != "
            f"job canonical_id {claimed.canonical_id!r}"
        )
    if payload.get("input_fingerprint") != claimed.input_fingerprint:
        raise TerminalJobError(
            f"stage result input_fingerprint {payload.get('input_fingerprint')!r} != "
            f"job input_fingerprint {claimed.input_fingerprint!r}"
        )
    if not _is_valid_sha256(payload.get("output_fingerprint")):
        raise TerminalJobError("stage result output_fingerprint is not a valid SHA-256")
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise TerminalJobError(f"stage result is not JSON-safe: {exc}") from exc
    return HandlerResult(ok=True, metadata={"stage_result": payload})


class RetryableJobError(Exception):
    """A stage handler error that should be treated as retryable.

    The store/protocol decides FAILED_RETRYABLE vs FAILED_TERMINAL based on
    attempt_count / max_attempts. The worker does NOT compute the final state.
    """


class TerminalJobError(Exception):
    """A stage handler error that must fail the job permanently."""


@dataclass(frozen=True)
class HandlerResult:
    """Typed return of an injected stage handler."""

    ok: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


StageHandler = Callable[[ClaimedJob], Any]


@dataclass(frozen=True)
class WorkerRunResult:
    """Result of one WorkerRuntime.run_once() cycle."""

    outcome: str  # "idle" | "completed" | "retryable" | "terminal" | "lease_lost" | "remote_unavailable"
    claimed_job_id: Optional[str] = None
    job_stage: Optional[str] = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None


class WorkerRuntime:
    """Generic worker loop using injected stage handlers.

    One ``run_once()`` does: heartbeat, claim one eligible job (capability
    matching, deterministic order), start it (creating attempt), run the
    injected handler, then commit success / retryable-failure / terminal-
    failure — all fenced by the lease token.

    Long jobs are kept alive by a lightweight heartbeat thread that renews the
    worker heartbeat AND the active job lease on an interval, so ASR/LLM stages
    that run for minutes do not lose their lease. If renewal ever raises
    StaleLeaseError the worker records ``lease_lost=True`` and the final
    completion attempt is fenced (rejected by the store); the job is then left
    to lease-expiry recovery. A running external handler cannot be force-killed,
    so M6-03 stage handlers must remain artifact-idempotent (documented
    execution property).
    """

    def __init__(
        self,
        *,
        store_path: str | Path | None = None,
        transport: Optional[WorkerOperationsTransport] = None,
        worker_id: str,
        display_name: Optional[str] = None,
        hostname: Optional[str] = None,
        capabilities: list[str] | None = None,
        handlers: Optional[dict[str, StageHandler]] = None,
        lease_duration_seconds: int = 120,
        heartbeat_interval_seconds: float = 10.0,
        poll_interval_seconds: float = 1.0,
        stale_threshold_seconds: int = 120,
        now: Optional[Callable[[], str]] = None,
        worker_metadata: Optional[dict[str, Any]] = None,
        allowed_stages: Optional[list[str]] = None,
    ) -> None:
        if transport is None:
            if store_path is None:
                raise ValueError(
                    "WorkerRuntime requires store_path (or an injected transport)"
                )
            transport = LocalSQLiteWorkerTransport(store_path)
        self._transport = transport
        self.store_path = Path(store_path) if store_path is not None else None
        self.worker_id = normalize_identity_component(worker_id)
        self.display_name = display_name
        self.hostname = hostname
        self.capabilities = normalize_capabilities(capabilities or [])
        self.allowed_stages: Optional[tuple[str, ...]] = (
            tuple(a.strip() for a in allowed_stages if a and a.strip())
            if allowed_stages
            else None
        )
        self.handlers: dict[str, StageHandler] = dict(handlers or {})
        self.lease_duration_seconds = int(lease_duration_seconds)
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.stale_threshold_seconds = int(stale_threshold_seconds)
        self._clock = now or utc_now_iso
        self._metadata = worker_metadata or {}
        self.lease_lost = False
        self._stop_event = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._active_token: Optional[str] = None
        self._active_job_id: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def register(self) -> dict[str, Any]:
        return self._transport.register_worker(
            self.worker_id,
            self.capabilities,
            display_name=self.display_name,
            hostname=self.hostname,
            metadata=self._metadata,
            allowed_stages=list(self.allowed_stages) if self.allowed_stages else None,
            now=self._clock(),
        )

    def heartbeat(self) -> dict[str, Any]:
        return self._transport.heartbeat_worker(
            self.worker_id,
            capabilities=self.capabilities,
            now=self._clock(),
        )

    def _renew_active_lease(self) -> None:
        if self._active_job_id is None or self._active_token is None:
            return
        self._transport.renew_job_lease(
            self._active_job_id,
            self.worker_id,
            self._active_token,
            lease_duration_seconds=self.lease_duration_seconds,
            now=self._clock(),
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self.heartbeat_interval_seconds)
            if self._stop_event.is_set():
                return
            try:
                self.heartbeat()
                self._renew_active_lease()
            except StaleLeaseError:
                self.lease_lost = True
            except Exception:
                # A transient heartbeat failure must not kill the worker; the
                # lease will simply not be renewed and eventually expires.
                pass

    def start_heartbeat_thread(self) -> None:
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._stop_event.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"ops-heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._hb_thread.start()

    def stop(self, *, wait: bool = True) -> None:
        """Graceful stop. Never marks a running job SUCCEEDED/FAILED itself;
        if a job is in flight it is left to complete or to lease expiry."""
        self._stop_event.set()
        if wait and self._hb_thread is not None:
            self._hb_thread.join(timeout=self.heartbeat_interval_seconds + 2)

    # -- core loop ---------------------------------------------------------

    def run_once(self) -> WorkerRunResult:
        """One worker cycle: heartbeat, claim, start, run handler, complete.

        Returns a typed WorkerRunResult. ``IDLE`` when no eligible job exists
        (including "no matching capability" / PC-offline semantics — jobs stay
        QUEUED; a missing worker is not a failure)."""
        try:
            self.heartbeat()
        except Exception:
            pass

        try:
            claimed = self._transport.claim_next_job(
                self.worker_id,
                self.capabilities,
                allowed_stages=list(self.allowed_stages)
                if self.allowed_stages
                else None,
                lease_duration_seconds=self.lease_duration_seconds,
                now=self._clock(),
            )
        except RemoteUnavailableError:
            # Control plane temporarily unreachable (NAS reboot/offline).
            # Keep the worker alive; lease-expiry recovery handles any
            # in-flight job. Do NOT crash the host.
            return WorkerRunResult(outcome="remote_unavailable")

        if claimed is None:
            return WorkerRunResult(outcome="idle")

        self._active_job_id = claimed.job_id
        self._active_token = claimed._lease_token
        self.lease_lost = False

        try:
            self._transport.start_claimed_job(
                claimed.job_id,
                self.worker_id,
                claimed._lease_token,
                now=self._clock(),
            )
        except RemoteUnavailableError:
            self._active_job_id = None
            self._active_token = None
            return WorkerRunResult(
                outcome="remote_unavailable",
                claimed_job_id=claimed.job_id,
                job_stage=claimed.stage,
            )
        except StaleLeaseError:
            self.lease_lost = True
            self._active_job_id = None
            self._active_token = None
            return WorkerRunResult(
                outcome="lease_lost",
                claimed_job_id=claimed.job_id,
                job_stage=claimed.stage,
            )

        handler = self.handlers.get(claimed.stage)
        try:
            if handler is None:
                raise TerminalJobError(
                    f"no handler registered for stage {claimed.stage!r}"
                )
            result = handler(claimed)
            if result is None:
                result = HandlerResult()
            elif isinstance(result, bool):
                if not result:
                    raise TerminalJobError(
                        f"handler for {claimed.stage!r} returned False"
                    )
                result = HandlerResult()
            elif not isinstance(result, HandlerResult):
                # M6-03 additive integration: a handler may return a typed
                # StageExecutionResult (JSON-safe, exposes to_dict()). The worker
                # audits identity before completion so a mismatched stage result
                # can never be committed as SUCCEEDED.
                result = _coerce_stage_result(result, claimed)

            if self.lease_lost:
                return WorkerRunResult(
                    outcome="lease_lost",
                    claimed_job_id=claimed.job_id,
                    job_stage=claimed.stage,
                )
            self._transport.complete_job_success(
                claimed.job_id,
                self.worker_id,
                claimed._lease_token,
                now=self._clock(),
                metadata=result.metadata or None,
            )
            return WorkerRunResult(
                outcome="completed", claimed_job_id=claimed.job_id, job_stage=claimed.stage
            )

        except TerminalJobError as exc:
            if self.lease_lost:
                return WorkerRunResult(
                    outcome="lease_lost",
                    claimed_job_id=claimed.job_id,
                    job_stage=claimed.stage,
                )
            self._transport.complete_job_terminal_failure(
                claimed.job_id,
                self.worker_id,
                claimed._lease_token,
                error_class=type(exc).__name__,
                error_message=str(exc),
                now=self._clock(),
            )
            return WorkerRunResult(
                outcome="terminal",
                claimed_job_id=claimed.job_id,
                job_stage=claimed.stage,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
        except RetryableJobError as exc:
            if self.lease_lost:
                return WorkerRunResult(
                    outcome="lease_lost",
                    claimed_job_id=claimed.job_id,
                    job_stage=claimed.stage,
                )
            self._transport.complete_job_retryable_failure(
                claimed.job_id,
                self.worker_id,
                claimed._lease_token,
                error_class=type(exc).__name__,
                error_message=str(exc),
                now=self._clock(),
            )
            return WorkerRunResult(
                outcome="retryable",
                claimed_job_id=claimed.job_id,
                job_stage=claimed.stage,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception as exc:
            # Unknown exception v1 policy: treat as retryable until
            # max_attempts; the store decides exhaustion. Diagnose, don't
            # swallow: error_class/error_message are persisted to the attempt.
            if self.lease_lost:
                return WorkerRunResult(
                    outcome="lease_lost",
                    claimed_job_id=claimed.job_id,
                    job_stage=claimed.stage,
                )
            self._transport.complete_job_retryable_failure(
                claimed.job_id,
                self.worker_id,
                claimed._lease_token,
                error_class=type(exc).__name__,
                error_message=str(exc),
                now=self._clock(),
            )
            return WorkerRunResult(
                outcome="retryable",
                claimed_job_id=claimed.job_id,
                job_stage=claimed.stage,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
        finally:
            self._active_job_id = None
            self._active_token = None
            self.lease_lost = False

    def run_forever(self, *, max_cycles: Optional[int] = None) -> int:
        """Loop run_once() until stop() is called or max_cycles is reached.

        Returns the number of completed (non-idle) cycles."""
        completed = 0
        cycles = 0
        while not self._stop_event.is_set():
            result = self.run_once()
            cycles += 1
            if result.outcome not in ("idle", "remote_unavailable"):
                completed += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._stop_event.wait(self.poll_interval_seconds)
        return completed