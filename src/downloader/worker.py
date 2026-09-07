"""Downloader Worker Service & CLI Entry Point (DY-D10).

Orchestrates the asynchronous intake, scheduling, execution, error handling, and
lifecycle management of download tasks between Collector C09 Outbox and Downloader
Execution Subsystems.

Key Invariants:
1. Single Active Worker Instance (Process Lock):
   - Controlled by DownloaderServiceLock. Multiple processes cannot run concurrently
     colliding on F2 process-global state.
2. Sequential F2 Execution:
   - One active download execution per worker process. No thread pools around F2.
3. Durable Accept Before Dispatch:
   - Coordinated via OutboxConsumerBridge.
4. Separation of Storage Domains:
   - Collector C09 outbox tracks delivery; DownloaderJobStore tracks execution.
5. D08 Retry & Cooldown Scheduling:
   - Delay seconds are persisted as next_attempt_at. Worker never blocks with sleep()
     inside job transactions.
6. Scope Pause & Rate Limiting:
   - 429 rate limits pause the offending scope without halting other scopes.
7. BLOCKED_AUTH Persistence:
   - Preserved across restarts; resumes automatically when auth is restored or manually
     via resume_blocked_scope().
8. Preflight Formal Asset Idempotency:
   - Detects previously published formal assets before calling backend, enabling safe
     crash recovery after D07 promotion.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.collector.download_models import DownloadPriority, DownloadTask
from src.downloader.contracts import (
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadResultContract,
    scrub_secrets,
)
from src.downloader.job_store import DownloaderJobStore, JobRecord, JobState
from src.downloader.outbox_bridge import OutboxConsumerBridge
from src.downloader.promoter import (
    ProductionArchivePromoter,
    verify_archived_asset,
)
from src.downloader.retry_policy import (
    AttemptRecord,
    DownloadFailureFact,
    ProductionDownloadErrorPolicy,
    RetryAction,
    RetryDecision,
    WorkerHealthAction,
)
from src.downloader.service_lock import (
    DownloaderServiceLock,
    ServiceLockError,
    WorkerIdentity,
)

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DownloaderWorkerService:
    """Production single-worker orchestration service for the Downloader subsystem."""

    def __init__(
        self,
        job_store: DownloaderJobStore,
        downloader: Any,
        collector_repo: Any | None = None,
        outbox_bridge: OutboxConsumerBridge | None = None,
        retry_policy: ProductionDownloadErrorPolicy | None = None,
        promoter: Any | None = None,
        service_lock: DownloaderServiceLock | None = None,
        worker_id: str | None = None,
        auth_recheck_interval_sec: float = 60.0,
        poll_interval_sec: float = 2.0,
    ) -> None:
        self.job_store = job_store
        self.downloader = downloader
        self.collector_repo = collector_repo
        self.outbox_bridge = outbox_bridge or (
            OutboxConsumerBridge(collector_repo, job_store) if collector_repo else None
        )
        self.retry_policy = retry_policy or ProductionDownloadErrorPolicy()
        self.promoter = promoter or getattr(downloader, "promoter", None)
        self.identity = (
            service_lock.identity
            if service_lock is not None
            else WorkerIdentity.current(instance_id=worker_id)
        )
        self.worker_id = worker_id or self.identity.serialize()
        self.service_lock = service_lock
        self.auth_recheck_interval_sec = auth_recheck_interval_sec
        self.poll_interval_sec = poll_interval_sec

        self._last_auth_check: float = 0.0
        self._is_healthy: bool = True
        self._running: bool = False

    @property
    def is_healthy(self) -> bool:
        return self._is_healthy

    def startup_recovery(self) -> int:
        """Startup crash recovery: resets orphan RUNNING jobs from dead worker sessions."""
        if self.service_lock is not None and not self.service_lock.is_acquired:
            raise ServiceLockError(
                "Cannot perform startup recovery without holding DownloaderServiceLock."
            )
        recovered = self.job_store.recover_stale_running()
        if recovered > 0:
            logger.warning(
                "Worker %s startup recovery: reset %d orphan RUNNING jobs to READY.",
                self.worker_id,
                recovered,
            )
        return recovered

    def check_blocked_auth(self, force: bool = False) -> int:
        """Periodically checks if authentication is restored and resumes BLOCKED_AUTH jobs."""
        now = time.time()
        if not force and (now - self._last_auth_check < self.auth_recheck_interval_sec):
            return 0

        self._last_auth_check = now
        # Check credential provider if available
        cred_provider = getattr(self.downloader, "credential_provider", None)
        if cred_provider is not None and hasattr(cred_provider, "is_auth_ready"):
            try:
                if not cred_provider.is_auth_ready():
                    return 0
            except Exception:
                return 0

        resumed = self.job_store.resume_blocked_auth()
        if resumed > 0:
            logger.info("Auth check: resumed %d BLOCKED_AUTH jobs to READY.", resumed)
        return resumed

    def resume_blocked_scope(self, scope_id: str) -> int:
        """Manually resumes BLOCKED_AUTH jobs for a specific account scope."""
        resumed = self.job_store.resume_blocked_auth(scope_id=scope_id)
        self.job_store.resume_scope("douyin", scope_id)
        logger.info("Manually resumed scope '%s': %d jobs set to READY.", scope_id, resumed)
        return resumed

    def run_once(self) -> bool:
        """Performs a single intake and execution step.

        Returns True if any work was performed (outbox intake or job executed),
        False if system is idle.
        """
        if not self._is_healthy:
            logger.error("DownloaderWorkerService is marked UNHEALTHY. Refusing execution.")
            return False

        if self.service_lock:
            self.service_lock.heartbeat()

        work_done = False

        # 1. Intake batch from C09 Outbox
        if self.outbox_bridge:
            try:
                intake_count = self.outbox_bridge.intake_batch(limit=10)
                if intake_count > 0:
                    work_done = True
            except Exception as e:
                logger.warning("Error during outbox intake: %s", e)

        # 2. Check auth restoration
        self.check_blocked_auth()

        # 3. Claim next due job
        job = self.job_store.claim_next_due(worker_id=self.worker_id)
        if not job:
            return work_done

        # 4. Execute claimed job
        self._execute_job(job)
        return True

    def _execute_job(self, job: JobRecord) -> None:
        """Executes a claimed job and records attempts, outcomes, and retries."""
        task = job.to_task()
        t0 = time.time()
        execution_id = f"exec_{uuid.uuid4().hex[:12]}"
        attempt_number = job.attempt_count + 1

        logger.info(
            "Worker %s executing job '%s' (attempt %d, execution '%s', content_type='%s')",
            self.worker_id,
            job.task_id,
            attempt_number,
            execution_id,
            job.content_type,
        )

        # Preflight Check: Does formal local asset already exist and pass verification?
        # Handles Section R (crash after D07 formal promotion) and Section AQ (cross-scope reuse).
        if self.promoter is not None and hasattr(self.promoter, "resolve_canonical_destination"):
            try:
                dest = self.promoter.resolve_canonical_destination(task.platform, task.platform_content_id)
                if dest.exists() and (dest / "asset_manifest.json").exists():
                    verif = verify_archived_asset(dest)
                    if verif.valid:
                        logger.info(
                            "Preflight check: Formal asset for '%s' is already valid in %s. Bypassing physical download.",
                            task.platform_content_id,
                            dest,
                        )
                        t1 = time.time()
                        self.job_store.record_attempt(
                            task_id=job.task_id,
                            attempt_number=attempt_number,
                            execution_id=execution_id,
                            started_at=_utcnow_iso(),
                            finished_at=_utcnow_iso(),
                            status="SUCCEEDED",
                            retry_action=None,
                            delay_seconds=0.0,
                        )
                        self.job_store.update_job_outcome(
                            task_id=job.task_id,
                            state=JobState.SUCCEEDED,
                            attempt_count=attempt_number,
                            result_summary={
                                "status": "SKIPPED",
                                "message": "Formal asset already verified in canonical archive (preflight idempotent bypass).",
                                "destination": str(dest),
                            },
                        )
                        return
            except Exception as e:
                logger.debug("Preflight formal asset check non-fatal error: %s", e)

        # Execute physical download via SafeDouyinDownloader
        result: DownloadResultContract
        try:
            result = self.downloader.execute(task)
        except Exception as exc:
            logger.exception("Unhandled exception executing task '%s': %s", job.task_id, exc)
            result = DownloadResultContract(
                source_url=task.source_url,
                platform_content_id=task.platform_content_id,
                task_id=task.task_id,
                scope_id=task.scope_id,
                status=DownloaderStatus.FAILED,
                error_code=DownloaderErrorCode.DOWNLOAD_UNKNOWN,
                message=str(exc),
                elapsed_sec=time.time() - t0,
            )

        t1 = time.time()
        is_success = result.status in (DownloaderStatus.SUCCESS, DownloaderStatus.SKIPPED, "SUCCESS", "SKIPPED")

        if is_success:
            logger.info("Job '%s' completed successfully.", job.task_id)
            self.job_store.record_attempt(
                task_id=job.task_id,
                attempt_number=attempt_number,
                execution_id=getattr(result, "execution_id", execution_id) or execution_id,
                started_at=_utcnow_iso(),
                finished_at=_utcnow_iso(),
                status="SUCCEEDED",
                retry_action=None,
                delay_seconds=0.0,
            )
            self.job_store.update_job_outcome(
                task_id=job.task_id,
                state=JobState.SUCCEEDED,
                attempt_count=attempt_number,
                result_summary=result.to_dict(),
            )
            # Clear scope pause if this scope succeeded
            self.job_store.resume_scope(job.platform, job.scope_id)
            return

        # Handle failure via D08 Retry Policy
        err_code = result.error_code or DownloaderErrorCode.DOWNLOAD_UNKNOWN
        stage_val = result.stage.value if hasattr(getattr(result, "stage", None), "value") else str(getattr(result, "stage", ""))
        subreason = (
            getattr(result, "subreason", "")
            or (getattr(result, "raw_diagnostics", {}).get("subreason") if isinstance(getattr(result, "raw_diagnostics", None), dict) else "")
            or getattr(result, "failed_stage", "")
            or stage_val
            or ""
        )
        fact = DownloadFailureFact(
            error_code=err_code,
            subreason=subreason,
            message=result.message or "Download failed",
            retry_after=result.retry_after,
            scope_id=job.scope_id,
            platform=job.platform,
            platform_content_id=job.platform_content_id,
            task_id=job.task_id,
            execution_id=execution_id,
        )

        decision: RetryDecision = self.retry_policy.decide(fact, attempt_history=attempt_number)
        logger.info(
            "D08 decision for '%s': action=%s, retryable=%s, delay=%.1fs, reason=%s",
            job.task_id,
            decision.action.value,
            decision.retryable,
            decision.delay_seconds,
            decision.reason,
        )

        # Record attempt history
        dec_err_code = (
            decision.error_code.value
            if hasattr(decision.error_code, "value")
            else (str(decision.error_code) if decision.error_code else None)
        )
        self.job_store.record_attempt(
            task_id=job.task_id,
            attempt_number=attempt_number,
            execution_id=execution_id,
            started_at=_utcnow_iso(),
            finished_at=_utcnow_iso(),
            status="FAILED",
            error_code=dec_err_code,
            subreason=decision.subreason,
            retry_action=decision.action.value,
            delay_seconds=decision.delay_seconds,
        )

        # State transition according to D08 decision
        if decision.action == RetryAction.BLOCKED_AUTH:
            self.job_store.update_job_outcome(
                task_id=job.task_id,
                state=JobState.BLOCKED_AUTH,
                attempt_count=attempt_number,
                last_error_code=dec_err_code,
                last_subreason=decision.subreason,
                result_summary=result.to_dict(),
            )
            logger.warning("Job '%s' paused in BLOCKED_AUTH: %s", job.task_id, decision.reason)

        elif decision.worker_action == WorkerHealthAction.WORKER_UNHEALTHY:
            self._is_healthy = False
            self.job_store.update_job_outcome(
                task_id=job.task_id,
                state=JobState.WORKER_UNHEALTHY,
                attempt_count=attempt_number,
                last_error_code=dec_err_code,
                last_subreason=decision.subreason,
                result_summary=result.to_dict(),
            )
            logger.critical("Worker %s marked UNHEALTHY by D08: %s", self.worker_id, decision.reason)

        elif decision.retryable and decision.action in (
            RetryAction.RETRY,
            RetryAction.WAIT_AND_RETRY,
            RetryAction.RETRY_AFTER_RERESOLVE,
            RetryAction.RETRY_AFTER_CREDENTIAL_REFRESH,
        ):
            next_attempt_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                seconds=decision.delay_seconds
            )
            next_attempt_iso = next_attempt_dt.isoformat()

            if decision.action == RetryAction.WAIT_AND_RETRY or decision.worker_action == WorkerHealthAction.PAUSE_SCOPE:
                # Persist scope-level pause
                self.job_store.pause_scope(
                    platform=job.platform,
                    scope_id=job.scope_id,
                    pause_until=next_attempt_iso,
                    reason=decision.reason,
                )
                logger.info(
                    "Paused scope '%s' until %s (reason: %s)",
                    job.scope_id,
                    next_attempt_iso,
                    decision.reason,
                )

            self.job_store.update_job_outcome(
                task_id=job.task_id,
                state=JobState.RETRY_WAIT,
                attempt_count=attempt_number,
                next_attempt_at=next_attempt_iso,
                last_error_code=dec_err_code,
                last_subreason=decision.subreason,
                result_summary=result.to_dict(),
            )

        else:
            # Terminal failure
            self.job_store.update_job_outcome(
                task_id=job.task_id,
                state=JobState.TERMINAL_FAILED,
                attempt_count=attempt_number,
                last_error_code=dec_err_code,
                last_subreason=decision.subreason,
                result_summary=result.to_dict(),
            )
            logger.error("Job '%s' reached TERMINAL_FAILED: %s", job.task_id, decision.reason)

    def run_until_idle(self, max_iterations: int = 1000) -> int:
        """Processes available jobs until no further work can be executed immediately."""
        processed = 0
        for _ in range(max_iterations):
            if not self._is_healthy:
                break
            worked = self.run_once()
            if worked:
                processed += 1
            else:
                break
        return processed

    def serve(
        self,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Long-running worker loop polling outbox and due retries."""
        logger.info("Starting DownloaderWorkerService %s (PID %d)...", self.worker_id, os.getpid())

        if self.service_lock:
            self.service_lock.acquire()

        try:
            self.startup_recovery()
            self._running = True

            while self._running:
                if stop_event and stop_event.is_set():
                    break
                if not self._is_healthy:
                    logger.error("Worker marked UNHEALTHY. Terminating server loop.")
                    break

                worked = self.run_once()
                if not worked:
                    time.sleep(self.poll_interval_sec)
        finally:
            self._running = False
            if self.service_lock:
                self.service_lock.release()
            logger.info("DownloaderWorkerService %s halted.", self.worker_id)


def main() -> None:
    """CLI entry point for Downloader Worker daemon."""
    parser = argparse.ArgumentParser(description="Douyin Downloader Worker Service (DY-D10)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Execute single intake/work iteration and exit.")
    group.add_argument("--drain", "--until-idle", action="store_true", help="Process all available jobs until idle.")
    group.add_argument("--serve", action="store_true", help="Run continuously as background daemon.")

    parser.add_argument("--collector-db", default="data/metadata.db", help="Path to Collector SQLite database.")
    parser.add_argument("--job-db", default="data/downloader_state.sqlite3", help="Path to Downloader job store.")
    parser.add_argument("--archive-root", default="archive", help="Formal media archive root directory.")
    parser.add_argument("--sandbox-root", default=None, help="Base directory for task sandboxes.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval in seconds.")
    parser.add_argument("--auth-recheck-interval", type=float, default=60.0, help="Auth recheck interval in seconds.")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from src.collector.repository import CollectorRepository
    from src.downloader.f2_backend import F2InProcessBackendAdapter
    from src.downloader.normalizer import ProductionAssetNormalizer
    from src.downloader.promoter import ProductionArchivePromoter
    from src.downloader.router import ProductionContentRouter
    from src.downloader.safe_downloader import SafeDouyinDownloader
    from src.downloader.sandbox import ProductionTaskSandboxProvider
    from src.downloader.validator import ProductionMediaValidator

    collector_repo = CollectorRepository(args.collector_db)
    job_store = DownloaderJobStore(args.job_db)
    archive_root = Path(args.archive_root)
    promoter = ProductionArchivePromoter(archive_root=archive_root)
    normalizer = ProductionAssetNormalizer()
    validator = ProductionMediaValidator()
    router = ProductionContentRouter()
    sandbox_provider = ProductionTaskSandboxProvider(
        base_dir=Path(args.sandbox_root) if args.sandbox_root else None
    )
    backend = F2InProcessBackendAdapter()

    downloader = SafeDouyinDownloader(
        sandbox_provider=sandbox_provider,
        backend=backend,
        normalizer=normalizer,
        validator=validator,
        promoter=promoter,
        router=router,
        require_auth=False,
    )

    lock_path = Path(args.job_db).parent / ".worker.lock"
    service_lock = DownloaderServiceLock(lock_path)

    service = DownloaderWorkerService(
        job_store=job_store,
        downloader=downloader,
        collector_repo=collector_repo,
        promoter=promoter,
        service_lock=service_lock,
        poll_interval_sec=args.poll_interval,
        auth_recheck_interval_sec=args.auth_recheck_interval,
    )

    if args.once:
        with service_lock:
            service.startup_recovery()
            worked = service.run_once()
            print(f"Worker run_once completed: worked={worked}")
    elif args.drain:
        with service_lock:
            service.startup_recovery()
            count = service.run_until_idle()
            print(f"Worker drain completed: {count} iterations executed.")
    elif args.serve:
        try:
            service.serve()
        except KeyboardInterrupt:
            print("Worker shutting down gracefully...")
            sys.exit(0)


if __name__ == "__main__":
    main()
