"""Durable Downloader Job Store (DY-D10).

Transactional SQLite-backed execution store decoupling Downloader internal execution
state from Collector delivery state (C09 outbox).

Key Invariants:
1. Domain Separation:
   - download_outbox (Collector DB) tracks DELIVERY state (PENDING / DISPATCHED / FAILED).
   - download_jobs (Downloader DB) tracks EXECUTION state (READY / RUNNING / RETRY_WAIT /
     BLOCKED_AUTH / SUCCEEDED / TERMINAL_FAILED / WORKER_UNHEALTHY).
2. Short Transactions:
   - No SQLite transaction ever wraps network downloads or F2 execution.
   - Claims and state transitions are short, atomic transactions.
3. Zero Secret Persistence:
   - Cookies, CredentialContext, and signed URLs are NEVER persisted in job store
     or attempt history.
4. Idempotent Acceptance:
   - Duplicate delivery of an existing task_id with matching immutable identity
     is accepted idempotently, refreshing volatile hints if needed.
   - Conflicting immutable identity raises a protocol violation error.
5. Scope Pause & Priority:
   - Scope pauses are persisted and enforced during job claims.
   - Priority-based claiming respects C09 priority order (higher priority claimed first).
"""

from __future__ import annotations

import datetime
import enum
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.collector.download_models import DownloadPriority, DownloadTask
from src.downloader.contracts import scrub_secrets
from src.downloader.service_lock import WorkerIdentity, check_process_liveness

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class JobState(str, enum.Enum):
    """Canonical lifecycle states of a download job in the Downloader subsystem."""

    READY = "READY"
    """Job is available to be claimed and executed by a worker."""

    RUNNING = "RUNNING"
    """Job is currently actively executing inside a worker process."""

    RETRY_WAIT = "RETRY_WAIT"
    """Job failed with a retryable error and is cooling down until next_attempt_at."""

    BLOCKED_AUTH = "BLOCKED_AUTH"
    """Job cannot proceed due to missing credentials or captcha challenge; paused pending operator recovery."""

    SUCCEEDED = "SUCCEEDED"
    """Media download, normalization, validation, and formal archive promotion completed successfully."""

    TERMINAL_FAILED = "TERMINAL_FAILED"
    """Job reached terminal fatal failure or exhausted retry budget; will not be retried."""

    WORKER_UNHEALTHY = "WORKER_UNHEALTHY"
    """Job was halted due to host environment structural failure (missing ffmpeg, broken F2, disk fault)."""


class JobStoreError(Exception):
    """Base exception for job store operations."""


class ImmutableIdentityConflictError(JobStoreError):
    """Raised when duplicate delivery has mismatched immutable identity fields."""


class JobNotFoundError(JobStoreError):
    """Raised when a specified job is not found."""


@dataclass(frozen=True)
class JobRecord:
    """Read-only view of a job record in the Downloader Job Store."""

    task_id: str
    task_payload_json: str
    platform: str
    scope_id: str
    platform_content_id: str
    content_type: str
    state: JobState
    priority: int
    accepted_at: str
    updated_at: str
    next_attempt_at: str
    attempt_count: int = 0
    last_error_code: str | None = None
    last_subreason: str | None = None
    source_outbox_id: str | None = None
    source_sync_run_id: str | None = None
    result_summary_json: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None

    def to_task(self) -> DownloadTask:
        """Reconstructs the DownloadTask from persisted payload."""
        data = json.loads(self.task_payload_json)
        return DownloadTask.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "scope_id": self.scope_id,
            "platform_content_id": self.platform_content_id,
            "content_type": self.content_type,
            "state": self.state.value,
            "priority": self.priority,
            "accepted_at": self.accepted_at,
            "updated_at": self.updated_at,
            "next_attempt_at": self.next_attempt_at,
            "attempt_count": self.attempt_count,
            "last_error_code": self.last_error_code,
            "last_subreason": self.last_subreason,
            "source_outbox_id": self.source_outbox_id,
            "source_sync_run_id": self.source_sync_run_id,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
        }


@dataclass(frozen=True)
class ScopePauseRecord:
    """Record of an active or historical scope pause."""

    platform: str
    scope_id: str
    pause_until: str
    reason: str
    created_at: str

    def is_active(self, now_iso: str | None = None) -> bool:
        current = now_iso or _utcnow_iso()
        return self.pause_until > current


@dataclass(frozen=True)
class AttemptLogRecord:
    """Persisted record of a single physical download execution attempt."""

    attempt_id: int
    task_id: str
    attempt_number: int
    execution_id: str
    started_at: str
    finished_at: str
    status: str
    error_code: str | None = None
    subreason: str | None = None
    retry_action: str | None = None
    delay_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "attempt_number": self.attempt_number,
            "execution_id": self.execution_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error_code": self.error_code,
            "subreason": scrub_secrets(self.subreason or ""),
            "retry_action": self.retry_action,
            "delay_seconds": round(self.delay_seconds, 3),
        }


class DownloaderJobStore:
    """Production Durable Job Store for Downloader worker execution state."""

    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 5000) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms};")
        return conn

    def _init_schema(self) -> None:
        """Initializes database schema with indexes."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS download_jobs (
                    task_id TEXT PRIMARY KEY,
                    task_payload_json TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    accepted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    last_subreason TEXT,
                    source_outbox_id TEXT,
                    source_sync_run_id TEXT,
                    result_summary_json TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_state_next_prio
                ON download_jobs (state, next_attempt_at, priority DESC, accepted_at ASC);

                CREATE INDEX IF NOT EXISTS idx_jobs_item
                ON download_jobs (platform, platform_content_id);

                CREATE INDEX IF NOT EXISTS idx_jobs_scope
                ON download_jobs (platform, scope_id);

                CREATE TABLE IF NOT EXISTS download_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    execution_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    subreason TEXT,
                    retry_action TEXT,
                    delay_seconds REAL DEFAULT 0.0,
                    FOREIGN KEY (task_id) REFERENCES download_jobs(task_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_task
                ON download_attempts (task_id, attempt_number);

                CREATE TABLE IF NOT EXISTS scope_pauses (
                    platform TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    pause_until TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (platform, scope_id)
                );
                """
            )
            conn.commit()

    def accept_task(
        self,
        task: DownloadTask,
        outbox_id: str | None = None,
        sync_run_id: str | None = None,
        now_iso: str | None = None,
    ) -> tuple[JobRecord, bool]:
        """Durably persists a task from outbox into the job store.

        Returns (JobRecord, was_created).
        If task_id already exists:
        - If immutable identity matches: idempotent accept (refreshes hint if changed).
        - If immutable identity differs: raises ImmutableIdentityConflictError.
        """
        now = now_iso or _utcnow_iso()
        task_dict = task.to_dict()
        sanitized_json = scrub_secrets(json.dumps(task_dict, ensure_ascii=False))

        # Determine integer priority from task
        prio_val = 50
        if hasattr(task, "priority"):
            if isinstance(task.priority, DownloadPriority):
                prio_val = int(task.priority.value)
            elif isinstance(task.priority, int):
                prio_val = task.priority

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM download_jobs WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()

            if row:
                # Validate immutable identity
                if (
                    row["platform"] != task.platform
                    or row["scope_id"] != task.scope_id
                    or row["platform_content_id"] != task.platform_content_id
                    or row["content_type"] != task.content_type
                ):
                    raise ImmutableIdentityConflictError(
                        f"Task '{task.task_id}' exists with conflicting identity: "
                        f"existing=({row['platform']}, {row['scope_id']}, {row['platform_content_id']}, {row['content_type']}) vs "
                        f"incoming=({task.platform}, {task.scope_id}, {task.platform_content_id}, {task.content_type})"
                    )

                # Idempotent re-delivery: update payload if volatile hints refreshed
                conn.execute(
                    """
                    UPDATE download_jobs
                    SET task_payload_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (sanitized_json, now, task.task_id),
                )
                conn.commit()
                updated_row = conn.execute(
                    "SELECT * FROM download_jobs WHERE task_id = ?",
                    (task.task_id,),
                ).fetchone()
                return self._row_to_job(updated_row), False

            # Insert new job
            conn.execute(
                """
                INSERT INTO download_jobs (
                    task_id, task_payload_json, platform, scope_id, platform_content_id,
                    content_type, state, priority, accepted_at, updated_at, next_attempt_at,
                    attempt_count, source_outbox_id, source_sync_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    task.task_id,
                    sanitized_json,
                    task.platform,
                    task.scope_id,
                    task.platform_content_id,
                    task.content_type,
                    JobState.READY.value,
                    prio_val,
                    now,
                    now,
                    now,
                    outbox_id,
                    sync_run_id,
                ),
            )
            conn.commit()
            created_row = conn.execute(
                "SELECT * FROM download_jobs WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
            return self._row_to_job(created_row), True

    def claim_next_due(
        self,
        worker_id: str,
        now_iso: str | None = None,
        allowed_platforms: Sequence[str] | None = None,
    ) -> JobRecord | None:
        """Atomically claims the next highest priority due job in a short transaction."""
        now = now_iso or _utcnow_iso()

        with self._connect() as conn:
            # Query candidate jobs that are READY or RETRY_WAIT with next_attempt_at <= now
            query = """
                SELECT j.*
                FROM download_jobs j
                LEFT JOIN scope_pauses sp
                    ON j.platform = sp.platform AND j.scope_id = sp.scope_id
                WHERE (j.state = 'READY' OR (j.state = 'RETRY_WAIT' AND j.next_attempt_at <= ?))
                  AND (sp.pause_until IS NULL OR sp.pause_until <= ?)
            """
            params: list[Any] = [now, now]

            if allowed_platforms:
                placeholders = ",".join("?" for _ in allowed_platforms)
                query += f" AND j.platform IN ({placeholders})"
                params.extend(allowed_platforms)

            query += " ORDER BY j.priority DESC, j.next_attempt_at ASC, j.accepted_at ASC LIMIT 1"

            cur = conn.cursor()
            candidate = cur.execute(query, tuple(params)).fetchone()
            if not candidate:
                return None

            task_id = candidate["task_id"]
            # Atomic transition to RUNNING with worker claim
            cur.execute(
                """
                UPDATE download_jobs
                SET state = ?, claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE task_id = ? AND state IN ('READY', 'RETRY_WAIT')
                """,
                (JobState.RUNNING.value, worker_id, now, now, task_id),
            )
            if cur.rowcount == 0:
                # Concurrent race lost, try again or return None
                return None

            conn.commit()
            updated_row = conn.execute(
                "SELECT * FROM download_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_job(updated_row)

    def record_attempt(
        self,
        task_id: str,
        attempt_number: int,
        execution_id: str,
        started_at: str,
        finished_at: str,
        status: str,
        error_code: str | None = None,
        subreason: str | None = None,
        retry_action: str | None = None,
        delay_seconds: float = 0.0,
    ) -> AttemptLogRecord:
        """Appends an attempt execution record to the append-only attempt table."""
        sanitized_sub = scrub_secrets(subreason or "")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO download_attempts (
                    task_id, attempt_number, execution_id, started_at, finished_at,
                    status, error_code, subreason, retry_action, delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt_number,
                    execution_id,
                    started_at,
                    finished_at,
                    status,
                    error_code,
                    sanitized_sub,
                    retry_action,
                    float(delay_seconds),
                ),
            )
            conn.commit()
            attempt_id = cur.lastrowid
            return AttemptLogRecord(
                attempt_id=attempt_id,
                task_id=task_id,
                attempt_number=attempt_number,
                execution_id=execution_id,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                error_code=error_code,
                subreason=sanitized_sub,
                retry_action=retry_action,
                delay_seconds=delay_seconds,
            )

    def update_job_outcome(
        self,
        task_id: str,
        state: JobState,
        attempt_count: int,
        next_attempt_at: str | None = None,
        last_error_code: str | None = None,
        last_subreason: str | None = None,
        result_summary: dict[str, Any] | None = None,
        now_iso: str | None = None,
    ) -> JobRecord:
        """Transitions job to finished, retrying, or paused state and clears claim."""
        now = now_iso or _utcnow_iso()
        nxt = next_attempt_at or now
        sanitized_summary = scrub_secrets(json.dumps(result_summary, ensure_ascii=False)) if result_summary else None
        sanitized_sub = scrub_secrets(last_subreason or "")

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE download_jobs
                SET state = ?,
                    attempt_count = ?,
                    next_attempt_at = ?,
                    last_error_code = ?,
                    last_subreason = ?,
                    result_summary_json = ?,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    state.value,
                    attempt_count,
                    nxt,
                    last_error_code,
                    sanitized_sub,
                    sanitized_summary,
                    now,
                    task_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM download_jobs WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise JobNotFoundError(f"Job '{task_id}' not found.")
            return self._row_to_job(row)

    def pause_scope(
        self,
        platform: str,
        scope_id: str,
        pause_until: str,
        reason: str,
        now_iso: str | None = None,
    ) -> ScopePauseRecord:
        """Persists a scope rate-limit or cooldown pause."""
        now = now_iso or _utcnow_iso()
        sanitized_reason = scrub_secrets(reason)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scope_pauses (platform, scope_id, pause_until, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform, scope_id) DO UPDATE SET
                    pause_until = excluded.pause_until,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (platform, scope_id, pause_until, sanitized_reason, now),
            )
            conn.commit()
            return ScopePauseRecord(
                platform=platform,
                scope_id=scope_id,
                pause_until=pause_until,
                reason=sanitized_reason,
                created_at=now,
            )

    def resume_scope(self, platform: str, scope_id: str) -> bool:
        """Manually clears an active scope pause."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM scope_pauses WHERE platform = ? AND scope_id = ?",
                (platform, scope_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_active_pause(
        self, platform: str, scope_id: str, now_iso: str | None = None
    ) -> ScopePauseRecord | None:
        """Retrieves active scope pause if current time is within pause window."""
        now = now_iso or _utcnow_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM scope_pauses
                WHERE platform = ? AND scope_id = ? AND pause_until > ?
                """,
                (platform, scope_id, now),
            ).fetchone()
            if not row:
                return None
            return ScopePauseRecord(
                platform=row["platform"],
                scope_id=row["scope_id"],
                pause_until=row["pause_until"],
                reason=row["reason"],
                created_at=row["created_at"],
            )

    def is_scope_paused(
        self, platform: str, scope_id: str, now_iso: str | None = None
    ) -> bool:
        return self.get_active_pause(platform, scope_id, now_iso) is not None

    def resume_blocked_auth(self, scope_id: str | None = None) -> int:
        """Transitions jobs from BLOCKED_AUTH back to READY upon auth restoration."""
        now = _utcnow_iso()
        query = "UPDATE download_jobs SET state = 'READY', next_attempt_at = ?, updated_at = ? WHERE state = 'BLOCKED_AUTH'"
        params: list[Any] = [now, now]
        if scope_id:
            query += " AND scope_id = ?"
            params.append(scope_id)

        with self._connect() as conn:
            cur = conn.execute(query, tuple(params))
            conn.commit()
            return cur.rowcount

    def recover_stale_running(
        self,
        dead_worker_ids: set[str] | None = None,
        now_iso: str | None = None,
    ) -> int:
        """Startup crash recovery: resets orphan RUNNING jobs belonging to dead workers.

        Recovery Rules:
        - If dead_worker_ids is explicitly provided: resets jobs whose claimed_by IN dead_worker_ids.
        - If dead_worker_ids is None:
          1. Queries all jobs with state = 'RUNNING'.
          2. For each job, inspects claimed_by via WorkerIdentity and OS process liveness:
             - PID does not exist in OS -> OWNER_DEAD -> reset to READY.
             - PID exists, but create_time does not match -> PID_REUSED -> reset to READY.
             - PID exists and create_time matches -> OWNER_ALIVE -> DO NOT RECOVER (keep RUNNING).
             - Unparseable / legacy claimed_by without active live process -> reset to READY.
        """
        now = now_iso or _utcnow_iso()
        with self._connect() as conn:
            if dead_worker_ids is not None:
                if not dead_worker_ids:
                    return 0
                placeholders = ",".join("?" for _ in dead_worker_ids)
                cur = conn.execute(
                    f"""
                    UPDATE download_jobs
                    SET state = 'READY', claimed_by = NULL, claimed_at = NULL, updated_at = ?
                    WHERE state = 'RUNNING' AND claimed_by IN ({placeholders})
                    """,
                    (now, *dead_worker_ids),
                )
                conn.commit()
                return cur.rowcount

            # dead_worker_ids is None: inspect each RUNNING job
            rows = conn.execute(
                "SELECT task_id, claimed_by FROM download_jobs WHERE state = 'RUNNING'"
            ).fetchall()
            if not rows:
                return 0

            to_recover: list[str] = []
            for row in rows:
                task_id = row["task_id"]
                claimed_by = row["claimed_by"]
                ident = WorkerIdentity.parse(claimed_by)
                if ident is not None:
                    is_alive, reason = check_process_liveness(
                        ident.pid, ident.process_started_at
                    )
                    if is_alive:
                        # Live original process! KEEP RUNNING!
                        # Job age or heartbeat alone is NOT sufficient to steal.
                        logger.info(
                            "Preserving RUNNING job '%s': claimed by live worker PID %d (started_at=%.2f, instance='%s').",
                            task_id,
                            ident.pid,
                            ident.process_started_at,
                            ident.instance_id,
                        )
                        continue
                    else:
                        logger.warning(
                            "Recovering RUNNING job '%s': owner PID %d is dead (%s).",
                            task_id,
                            ident.pid,
                            reason,
                        )
                        to_recover.append(task_id)
                else:
                    # Legacy or unparseable worker id:
                    logger.warning(
                        "Recovering RUNNING job '%s': unparseable or legacy owner '%s'.",
                        task_id,
                        claimed_by,
                    )
                    to_recover.append(task_id)

            if not to_recover:
                return 0

            placeholders = ",".join("?" for _ in to_recover)
            cur = conn.execute(
                f"""
                UPDATE download_jobs
                SET state = 'READY', claimed_by = NULL, claimed_at = NULL, updated_at = ?
                WHERE task_id IN ({placeholders})
                """,
                (now, *to_recover),
            )
            conn.commit()
            return cur.rowcount

    def get_job(self, task_id: str) -> JobRecord | None:
        """Retrieves a single job by task_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM download_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_job(row) if row else None

    def list_attempts(self, task_id: str) -> list[AttemptLogRecord]:
        """Retrieves chronological attempt history for a task."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM download_attempts
                WHERE task_id = ?
                ORDER BY attempt_number ASC
                """,
                (task_id,),
            ).fetchall()
            return [
                AttemptLogRecord(
                    attempt_id=r["id"],
                    task_id=r["task_id"],
                    attempt_number=r["attempt_number"],
                    execution_id=r["execution_id"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    status=r["status"],
                    error_code=r["error_code"],
                    subreason=r["subreason"],
                    retry_action=r["retry_action"],
                    delay_seconds=r["delay_seconds"],
                )
                for r in rows
            ]

    def count_jobs_by_state(self) -> dict[str, int]:
        """Returns aggregate job counts grouped by state."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM download_jobs GROUP BY state"
            ).fetchall()
            return {r["state"]: r["cnt"] for r in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            task_id=row["task_id"],
            task_payload_json=row["task_payload_json"],
            platform=row["platform"],
            scope_id=row["scope_id"],
            platform_content_id=row["platform_content_id"],
            content_type=row["content_type"],
            state=JobState(row["state"]),
            priority=row["priority"],
            accepted_at=row["accepted_at"],
            updated_at=row["updated_at"],
            next_attempt_at=row["next_attempt_at"],
            attempt_count=row["attempt_count"],
            last_error_code=row["last_error_code"],
            last_subreason=row["last_subreason"],
            source_outbox_id=row["source_outbox_id"],
            source_sync_run_id=row["source_sync_run_id"],
            result_summary_json=row["result_summary_json"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
        )
