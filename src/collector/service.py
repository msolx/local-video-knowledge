"""Collector Service Orchestrator managing execution lifecycle and error boundaries."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .base import BaseCollector, CollectorMode, CollectorRunResult, CollectorStatus, utcnow_iso
from .config import CollectorConfig
from .errors import AuthNotReadyError, CollectorError, CollectorErrorCode, LockError
from .lock import ServiceLock
from .logging import configure_collector_logging


class CollectorService:
    """Orchestrates the lifecycle of collection operations with locking, logging, and error boundaries."""

    def __init__(self, collector: BaseCollector, config: CollectorConfig | None = None) -> None:
        self.collector = collector
        self.config = config or getattr(collector, "config", CollectorConfig())

    def execute(self, mode: CollectorMode, **kwargs: Any) -> CollectorRunResult:
        """Executes a collector action within the full lifecycle framework."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = utcnow_iso()

        # 1. Initialize environment & ensure directories
        try:
            self.config.ensure_directories()
        except Exception as e:
            return CollectorRunResult(
                run_id=run_id,
                platform=self.collector.platform,
                mode=mode,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error={
                    "code": CollectorErrorCode.CONFIG_ERROR.value,
                    "message": f"Failed to initialize runtime directories: {e}",
                },
            )

        # 2. Setup structured logging
        logger = configure_collector_logging(
            run_id=run_id,
            log_dir=self.config.log_dir,
            log_level=self.config.log_level,
        )
        logger.info(f"Initiating collector run for platform '{self.collector.platform}', mode '{mode.value}'")

        # 3. Single-instance service lock
        lock = ServiceLock(
            lock_path=self.config.lock_path,
            platform=self.collector.platform,
            timeout_sec=self.config.lock_timeout_sec,
        )

        try:
            lock.acquire(run_id)
        except LockError as e:
            logger.error(f"Service lock acquisition failed: {e.message}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.collector.platform,
                mode=mode,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error=e.to_dict(),
            )

        try:
            # 4. Initialize collector dependencies
            logger.info("Initializing collector dependencies...")
            self.collector.initialize(run_id)

            # 5. Preflight authentication check
            logger.info("Executing preflight authentication check...")
            preflight_res: Any = None
            if hasattr(self.collector, "run_preflight"):
                preflight_res = self.collector.run_preflight(run_id)
                can_sync = bool(getattr(preflight_res, "can_sync", False))
            else:
                can_sync = bool(self.collector.preflight_auth_check(run_id))

            if not can_sync:
                logger.warning(f"Preflight authentication check failed (run_id={run_id}).")

                err_code = CollectorErrorCode.AUTH_NOT_READY
                err_msg = "Preflight authentication check failed. Valid session required."
                details: dict[str, Any] = {}

                if preflight_res is not None:
                    details = preflight_res.to_dict() if hasattr(preflight_res, "to_dict") else {}
                    auth_state = getattr(preflight_res, "auth_state", None)
                    source_health = getattr(preflight_res, "source_health", None)
                    reason_code = getattr(preflight_res, "reason_code", "")
                    action = getattr(preflight_res, "recovery_action", None)
                    action_val = getattr(action, "value", str(action)) if action else "MANUAL_LOGIN"

                    auth_state_val = getattr(auth_state, "value", str(auth_state)) if auth_state else ""
                    health_val = getattr(source_health, "value", str(source_health)) if source_health else ""

                    if auth_state_val in ("AUTH_REQUIRED", "AUTH_CHALLENGE"):
                        err_code = CollectorErrorCode.AUTH_NOT_READY
                        err_msg = f"Authentication not ready ({reason_code}): action required '{action_val}'."
                    elif health_val == "RATE_LIMITED":
                        err_code = CollectorErrorCode.RUNTIME_ERROR
                        err_msg = f"Platform rate limit encountered during preflight ({reason_code})."
                    elif health_val in ("SERVER_ERROR", "NETWORK_ERROR"):
                        err_code = CollectorErrorCode.RUNTIME_ERROR
                        err_msg = f"Remote service/network error during preflight ({reason_code})."
                    elif reason_code == "BROWSER_NOT_READY":
                        err_code = CollectorErrorCode.DEPENDENCY_NOT_READY
                        err_msg = "Browser runtime dependency not ready during preflight."
                    else:
                        err_code = CollectorErrorCode.RUNTIME_ERROR
                        err_msg = f"Preflight validation failed ({reason_code})."

                return CollectorRunResult(
                    run_id=run_id,
                    platform=self.collector.platform,
                    mode=mode,
                    status=CollectorStatus.FAILED,
                    started_at=started_at,
                    finished_at=utcnow_iso(),
                    error={
                        "code": err_code.value,
                        "message": err_msg,
                        "details": details,
                    },
                )

            # 6. Dispatch operational mode
            logger.info(f"Executing operational mode: {mode.value}")
            if mode == CollectorMode.PROBE:
                result = self.collector.probe(run_id)
            elif mode == CollectorMode.SYNC:
                result = self.collector.sync(run_id, **kwargs)
            elif mode == CollectorMode.BACKFILL:
                result = self.collector.backfill(run_id, **kwargs)
            else:
                result = CollectorRunResult(
                    run_id=run_id,
                    platform=self.collector.platform,
                    mode=mode,
                    status=CollectorStatus.FAILED,
                    started_at=started_at,
                    finished_at=utcnow_iso(),
                    error={
                        "code": CollectorErrorCode.CONFIG_ERROR.value,
                        "message": f"Unsupported collector mode: {mode}",
                    },
                )

            result.finished_at = utcnow_iso()
            logger.info(f"Collector run finished with status: {result.status.value}")
            return result

        except CollectorError as e:
            logger.error(f"Collector error caught: [{e.code.value}] {e.message}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.collector.platform,
                mode=mode,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error=e.to_dict(),
            )
        except Exception as e:
            logger.exception(f"Unhandled exception during collector execution: {e}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.collector.platform,
                mode=mode,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error={
                    "code": CollectorErrorCode.UNKNOWN.value,
                    "message": f"Unhandled exception: {e}",
                    "details": {"exception_type": type(e).__name__},
                },
            )
        finally:
            # 7. Guaranteed teardown & lock release
            try:
                self.collector.shutdown(run_id)
            except Exception as e:
                logger.error(f"Error during collector shutdown: {e}")
            lock.release()
            logger.info("Service lock released and execution terminated.")

    run = execute
