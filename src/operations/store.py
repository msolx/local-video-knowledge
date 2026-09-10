"""M6-01 Durable Operations Store & Job State Machine (SQLite).

Implements the sealed `operations-store-v1` schema:

  - operations_meta  schema/policy metadata (schema_version, policy_version, created_at)
  - assets           canonical asset identity (platform, platform_content_id) + lifecycle
  - pipeline_runs    one row per asset orchestration generation (trigger, status)
  - jobs             deterministic stage jobs (job_id, stage, state, fingerprint, retry/lease)
  - job_attempts     per-attempt execution history (monotonic attempt_number)
  - workers          capability-based worker registry (persistence only in M6-01)
  - event_log        append-only structured audit trail

Invariants (M6_OPERATIONS_ARCHITECTURE.md §5-§10, M6_DECISIONS D1-D6/D10/D11/D13):

  - The operations DB is durable orchestration state — never the knowledge Source
    of Truth, never M5 knowledge_store.sqlite3, never the M2 archive DB.
  - Canonical asset identity is (platform, platform_content_id); UNIQUE.
  - Asset lifecycle and job lifecycle are two distinct forward-only state machines.
  - job_id is deterministic (platform|content_id|stage|input_fingerprint|policy_version),
    never includes time/attempt/worker/lease/run components.
  - Same identity already SUCCEEDED => SKIP / existing-success. Changed input
    fingerprint => new identity, new job generation (history preserved).
  - Every state mutation is `BEGIN IMMEDIATE ... COMMIT` and writes its event in
    the same transaction. Terminal states are never silently revived.
  - event_log is append-only: the API exposes no update/delete.
  - Timestamps are UTC ISO-8601 (src.storage.utc_now), injectable for tests.
  - Secrets never enter job payloads/metadata; API never requires secret material.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .models import (
    OPERATIONS_SCHEMA_VERSION,
    OPERATIONS_POLICY_VERSION,
    OPERATIONS_USER_VERSION,
    DEFAULT_OPERATIONS_PATH,
    VALID_CAPABILITIES,
    AssetLifecycleState,
    JobStage,
    JobState,
    PipelineRunStatus,
    TriggerType,
    TERMINAL_JOB_STATES,
    LEASE_HELD_JOB_STATES,
    VALID_TRIGGER_TYPES,
    ClaimedJob,
    EnqueueResult,
    OperationsValidationResult,
    compute_job_id,
    compute_next_retry_at,
    format_iso,
    is_legal_asset_transition,
    is_legal_job_transition,
    is_terminal_job_state,
    is_valid_job_id,
    new_lease_token,
    normalize_capabilities,
    normalize_identity_component,
    normalize_input_fingerprint,
    normalize_job_stage,
    parse_iso,
    utc_now_iso,
)

__all__ = [
    "OPERATIONS_SCHEMA_VERSION",
    "OPERATIONS_POLICY_VERSION",
    "OPERATIONS_USER_VERSION",
    "DEFAULT_OPERATIONS_PATH",
    "VALID_CAPABILITIES",
    "OperationsError",
    "OperationsSchemaError",
    "OperationsStateError",
    "OperationsIntegrityError",
    "StaleLeaseError",
    "DEFAULT_LEASE_DURATION_SECONDS",
    "DEFAULT_WORKER_STALE_THRESHOLD_SECONDS",
    "create_operations_store",
    "open_operations_store",
    "register_asset",
    "get_asset",
    "get_asset_by_canonical_id",
    "transition_asset_lifecycle",
    "admin_requeue_asset_lifecycle",
    "create_pipeline_run",
    "complete_pipeline_run",
    "get_pipeline_run",
    "list_pipeline_runs",
    "get_scheduler_state",
    "set_scheduler_state",
    "enqueue_job",
    "get_job",
    "list_jobs",
    "list_pending_jobs",
    "list_failed_jobs",
    "transition_job_state",
    "begin_attempt",
    "finish_attempt",
    "list_job_attempts",
    "cancel_job",
    "requeue_retryable_job",
    "set_job_lease",
    "clear_job_lease",
    "register_worker",
    "worker_heartbeat",
    "list_workers",
    "list_events",
    "claim_next_job",
    "renew_job_lease",
    "start_claimed_job",
    "complete_job_success",
    "complete_job_retryable_failure",
    "complete_job_terminal_failure",
    "recover_expired_leases",
    "is_worker_stale",
    "list_workers_with_status",
    "validate_operations_store",
]

# Default max attempts per job stage (configurable per enqueue).
DEFAULT_MAX_ATTEMPTS = 5

# M6-02 lease / heartbeat defaults (configurable, injectable in tests).
DEFAULT_LEASE_DURATION_SECONDS = 120
DEFAULT_WORKER_STALE_THRESHOLD_SECONDS = 120

# event types
EVENT_ASSET_REGISTERED = "asset_registered"
EVENT_ASSET_OBSERVED_DUPLICATE = "asset_observed_duplicate"
EVENT_ASSET_LIFECYCLE_TRANSITION = "asset_lifecycle_transition"
EVENT_ASSET_LIFECYCLE_ADMIN = "asset_lifecycle_admin_override"
EVENT_RUN_CREATED = "pipeline_run_created"
EVENT_RUN_COMPLETED = "pipeline_run_completed"
EVENT_JOB_ENQUEUED = "job_enqueued"
EVENT_JOB_SKIPPED = "job_skipped_existing_success"
EVENT_JOB_STATE_TRANSITION = "job_state_transition"
EVENT_JOB_CANCELLED = "job_cancelled"
EVENT_JOB_REQUEUED = "job_requeued"
EVENT_ATTEMPT_BEGUN = "attempt_begun"
EVENT_ATTEMPT_FINISHED = "attempt_finished"
EVENT_LEASE_SET = "job_lease_set"
EVENT_LEASE_CLEARED = "job_lease_cleared"
EVENT_WORKER_REGISTERED = "worker_registered"
EVENT_WORKER_HEARTBEAT = "worker_heartbeat"
EVENT_JOB_CLAIMED = "job_claimed"
EVENT_LEASE_EXPIRED_REQUEUED = "lease_expired_requeued"
EVENT_ATTEMPT_CLOSED_EXPIRED = "attempt_closed_lease_expired"

# M6-04 scheduler event types
EVENT_POLL_SCHEDULED = "poll_scheduled"
EVENT_POLL_REQUEUED = "poll_requeued"
EVENT_DISCOVERY_ASSETS_REGISTERED = "scheduler_assets_registered"
EVENT_PIPELINE_RUN_CREATED = "scheduler_pipeline_run_created"
EVENT_DOWNSTREAM_ENQUEUED = "scheduler_downstream_enqueued"
EVENT_LIFECYCLE_ADVANCED = "scheduler_lifecycle_advanced"
EVENT_RUN_COMPLETED_SCHEDULER = "scheduler_run_completed"
EVENT_RUN_FAILED_SCHEDULER = "scheduler_run_failed"
EVENT_RETRY_REQUEUED = "scheduler_retry_requeued"
EVENT_ORCHESTRATION_INVARIANT = "orchestration_invariant_failure"

_META_SCHEMA_VERSION_KEY = "schema_version"
_META_POLICY_VERSION_KEY = "policy_version"
_META_CREATED_AT_KEY = "created_at"


class OperationsError(Exception):
    """Base class for operations store errors."""


class OperationsSchemaError(OperationsError):
    """Raised when the on-disk schema is missing or incompatible."""


class OperationsStateError(OperationsError):
    """Raised on an illegal state transition (asset or job)."""


class OperationsIntegrityError(OperationsError):
    """Raised on identity/fingerprint/reference integrity violations."""


class StaleLeaseError(OperationsError):
    """Raised when a job mutation is attempted with a lease that is no longer
    current (superseded by a newer claim, or the job left its leased state).

    The fence (lease_token) is the only proof of execution ownership: checking
    only ``lease_owner == worker_id`` is insufficient because the same worker
    process may later reclaim the same job with a fresh token.
    """


def _capability_match(required: list[str], worker_capabilities) -> bool:
    """Exact all-of capability matching. Empty requirement always matches."""
    return set(required).issubset(set(worker_capabilities or ()))


# ----------------------------------------------------------------------
# Canonical serialization helpers (project-wide convention)
# ----------------------------------------------------------------------


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _json_loads(value: Optional[str]) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


# ----------------------------------------------------------------------
# Connection helpers
# ----------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _required_tables_exist(conn: sqlite3.Connection) -> bool:
    required = {
        "operations_meta",
        "assets",
        "pipeline_runs",
        "jobs",
        "job_attempts",
        "workers",
        "event_log",
        "scheduler_state",
    }
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    present = {row["name"] for row in rows}
    return required.issubset(present)


def _any_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) AS n FROM sqlite_master WHERE type='table'"
    ).fetchone()
    return bool(row["n"])


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operations_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_rowid INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_content_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL UNIQUE,
    lifecycle_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    upstream_fingerprint TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (platform, platform_content_id)
);

CREATE INDEX IF NOT EXISTS idx_assets_lifecycle ON assets(lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_assets_platform ON assets(platform);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (canonical_id) REFERENCES assets(canonical_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_canonical ON pipeline_runs(canonical_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_content_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    state TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    pipeline_run_id TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    lease_owner TEXT,
    leased_at TEXT,
    lease_expires_at TEXT,
    lease_token TEXT,
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    enqueued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (canonical_id) REFERENCES assets(canonical_id),
    FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_next_retry ON jobs(next_retry_at);

CREATE TABLE IF NOT EXISTS job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error_class TEXT,
    error_message TEXT,
    retryable INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    UNIQUE (job_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_attempts_job ON job_attempts(job_id);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    display_name TEXT,
    hostname TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS event_log (
    event_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    canonical_id TEXT,
    pipeline_run_id TEXT,
    job_id TEXT,
    worker_id TEXT,
    from_state TEXT,
    to_state TEXT,
    message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_event_timestamp ON event_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_event_canonical ON event_log(canonical_id);
CREATE INDEX IF NOT EXISTS idx_event_job ON event_log(job_id);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_log(event_type);

CREATE TABLE IF NOT EXISTS scheduler_state (
    scheduler_key TEXT PRIMARY KEY,
    last_scheduled_at TEXT,
    last_completed_at TEXT,
    next_due_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _initialize_schema(conn: sqlite3.Connection, created_at: str) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {OPERATIONS_USER_VERSION}")
    meta_rows = {
        _META_SCHEMA_VERSION_KEY: OPERATIONS_SCHEMA_VERSION,
        _META_POLICY_VERSION_KEY: OPERATIONS_POLICY_VERSION,
        _META_CREATED_AT_KEY: created_at,
    }
    for key, value in meta_rows.items():
        conn.execute(
            "INSERT OR IGNORE INTO operations_meta(key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def create_operations_store(db_path: Path, *, now: Optional[str] = None) -> None:
    """Create the operations store at ``db_path``.

    Empty/missing file or user_version 0 → initialize. Incompatible schema →
    explicit OperationsSchemaError. No silent migration.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    conn = _connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, OPERATIONS_USER_VERSION):
            conn.close()
            raise OperationsSchemaError(
                f"operations store user_version={version} is incompatible with "
                f"expected {OPERATIONS_USER_VERSION}"
            )
        if _any_tables_exist(conn) and version == 0:
            conn.close()
            raise OperationsSchemaError(
                "operations store has tables but user_version=0 (unrecognized schema)"
            )
        if version == OPERATIONS_USER_VERSION and _required_tables_exist(conn):
            conn.close()
            return
        _initialize_schema(conn, now)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def open_operations_store(db_path: Path) -> sqlite3.Connection:
    """Open (creating if empty) an operations store; validate schema header."""
    db_path = Path(db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        create_operations_store(db_path)
    conn = _connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != OPERATIONS_USER_VERSION:
            conn.close()
            raise OperationsSchemaError(
                f"operations store user_version={version} != "
                f"expected {OPERATIONS_USER_VERSION}"
            )
        if not _required_tables_exist(conn):
            conn.close()
            raise OperationsSchemaError(
                "operations store is missing required tables"
            )
        return conn
    except BaseException:
        conn.close()
        raise


# ----------------------------------------------------------------------
# Event log (append-only)
# ----------------------------------------------------------------------


def _append_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    timestamp: str,
    canonical_id: Optional[str] = None,
    pipeline_run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO event_log(
            timestamp, event_type, canonical_id, pipeline_run_id, job_id,
            worker_id, from_state, to_state, message, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            event_type,
            canonical_id,
            pipeline_run_id,
            job_id,
            worker_id,
            from_state,
            to_state,
            message,
            _json_dumps(metadata or {}),
        ),
    )
    return cur.lastrowid


# ----------------------------------------------------------------------
# Asset lifecycle
# ----------------------------------------------------------------------


def register_asset(
    db_path: Path,
    platform: str,
    platform_content_id: str,
    canonical_id: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    upstream_fingerprint: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Register an asset under its frozen identity (platform, platform_content_id).

    Idempotent: re-registering the same identity returns the existing asset and
    records a duplicate-observation event (never a second row).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    platform = normalize_identity_component(platform)
    content_id = normalize_identity_component(platform_content_id)
    canonical_id = normalize_identity_component(canonical_id)
    if upstream_fingerprint is not None:
        upstream_fingerprint = normalize_input_fingerprint(upstream_fingerprint)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM assets WHERE platform=? AND platform_content_id=?",
                (platform, content_id),
            ).fetchone()
            if existing is not None:
                if existing["canonical_id"] != canonical_id:
                    conn.rollback()
                    raise OperationsIntegrityError(
                        f"asset identity ({platform}, {content_id}) already maps to "
                        f"canonical_id={existing['canonical_id']}, cannot map to "
                        f"{canonical_id}"
                    )
                _append_event(
                    conn,
                    EVENT_ASSET_OBSERVED_DUPLICATE,
                    timestamp=now,
                    canonical_id=canonical_id,
                    message="duplicate registration of existing asset",
                    metadata={"platform": platform, "platform_content_id": content_id},
                )
                conn.commit()
                return _row_dict(existing)

            by_canonical = conn.execute(
                "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if by_canonical is not None:
                conn.rollback()
                raise OperationsIntegrityError(
                    f"canonical_id={canonical_id} already registered for "
                    f"({by_canonical['platform']}, {by_canonical['platform_content_id']})"
                )

            conn.execute(
                """
                INSERT INTO assets(
                    platform, platform_content_id, canonical_id, lifecycle_state,
                    created_at, updated_at, upstream_fingerprint, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    content_id,
                    canonical_id,
                    AssetLifecycleState.DISCOVERED.value,
                    now,
                    now,
                    upstream_fingerprint,
                    _json_dumps(metadata or {}),
                ),
            )
            _append_event(
                conn,
                EVENT_ASSET_REGISTERED,
                timestamp=now,
                canonical_id=canonical_id,
                message="asset registered as DISCOVERED",
                metadata={"platform": platform, "platform_content_id": content_id},
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def get_asset(db_path: Path, platform: str, platform_content_id: str) -> Optional[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE platform=? AND platform_content_id=?",
            (
                normalize_identity_component(platform),
                normalize_identity_component(platform_content_id),
            ),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def get_asset_by_canonical_id(db_path: Path, canonical_id: str) -> Optional[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE canonical_id=?",
            (normalize_identity_component(canonical_id),),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def transition_asset_lifecycle(
    db_path: Path,
    canonical_id: str,
    to_state: str,
    *,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Forward-only asset lifecycle transition (normal stage success path)."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    canonical_id = normalize_identity_component(canonical_id)
    to_state = normalize_identity_component(to_state).upper()

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise OperationsIntegrityError(
                    f"asset canonical_id={canonical_id} is not registered"
                )
            from_state = row["lifecycle_state"]
            if from_state == to_state:
                conn.commit()
                return _row_dict(row)
            if not is_legal_asset_transition(from_state, to_state):
                conn.rollback()
                raise OperationsStateError(
                    f"illegal asset lifecycle transition "
                    f"{from_state} -> {to_state} for {canonical_id}"
                )
            conn.execute(
                "UPDATE assets SET lifecycle_state=?, updated_at=? WHERE canonical_id=?",
                (to_state, now, canonical_id),
            )
            _append_event(
                conn,
                EVENT_ASSET_LIFECYCLE_TRANSITION,
                timestamp=now,
                canonical_id=canonical_id,
                from_state=from_state,
                to_state=to_state,
                message=f"asset lifecycle {from_state} -> {to_state}",
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def admin_requeue_asset_lifecycle(
    db_path: Path,
    canonical_id: str,
    to_state: str,
    *,
    reason: str,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Explicit admin recovery/reconcile: any asset state change, always logged.

    Only for explicit admin reconciliation — the normal pipeline path must use
    transition_asset_lifecycle (forward-only).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    canonical_id = normalize_identity_component(canonical_id)
    to_state = normalize_identity_component(to_state).upper()
    if to_state not in {s.value for s in AssetLifecycleState}:
        raise OperationsStateError(f"unknown asset lifecycle state: {to_state}")

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise OperationsIntegrityError(
                    f"asset canonical_id={canonical_id} is not registered"
                )
            from_state = row["lifecycle_state"]
            conn.execute(
                "UPDATE assets SET lifecycle_state=?, updated_at=? WHERE canonical_id=?",
                (to_state, now, canonical_id),
            )
            _append_event(
                conn,
                EVENT_ASSET_LIFECYCLE_ADMIN,
                timestamp=now,
                canonical_id=canonical_id,
                from_state=from_state,
                to_state=to_state,
                message=f"admin override {from_state} -> {to_state}: {reason}",
                metadata={"reason": reason},
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Pipeline runs
# ----------------------------------------------------------------------


def create_pipeline_run(
    db_path: Path,
    canonical_id: str,
    trigger_type: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    canonical_id = normalize_identity_component(canonical_id)
    trigger_type = normalize_identity_component(trigger_type).lower()
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise OperationsIntegrityError(f"unknown trigger_type: {trigger_type!r}")

    import hashlib

    run_raw = f"{canonical_id}|{trigger_type}|{now}"
    run_id = "run_" + hashlib.sha256(run_raw.encode("utf-8")).hexdigest()[:16]

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            asset = conn.execute(
                "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if asset is None:
                conn.rollback()
                raise OperationsIntegrityError(
                    f"asset canonical_id={canonical_id} is not registered"
                )
            conn.execute(
                """
                INSERT INTO pipeline_runs(
                    run_id, canonical_id, trigger_type, status, started_at,
                    completed_at, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    canonical_id,
                    trigger_type,
                    PipelineRunStatus.RUNNING.value,
                    now,
                    None,
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            _append_event(
                conn,
                EVENT_RUN_CREATED,
                timestamp=now,
                canonical_id=canonical_id,
                pipeline_run_id=run_id,
                message=f"pipeline run {run_id} started ({trigger_type})",
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def complete_pipeline_run(
    db_path: Path,
    run_id: str,
    status: str,
    *,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    run_id = normalize_identity_component(run_id)
    status = normalize_identity_component(status).upper()
    if status not in {s.value for s in PipelineRunStatus}:
        raise OperationsStateError(f"unknown pipeline run status: {status!r}")

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise OperationsIntegrityError(f"pipeline run {run_id} not found")
            from_status = row["status"]
            if from_status != PipelineRunStatus.RUNNING.value:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot complete pipeline run {run_id} from {from_status}"
                )
            conn.execute(
                "UPDATE pipeline_runs SET status=?, completed_at=? WHERE run_id=?",
                (status, now, run_id),
            )
            _append_event(
                conn,
                EVENT_RUN_COMPLETED,
                timestamp=now,
                canonical_id=row["canonical_id"],
                pipeline_run_id=run_id,
                to_state=status,
                message=f"pipeline run {run_id} -> {status}",
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def get_pipeline_run(db_path: Path, run_id: str) -> Optional[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id=?",
            (normalize_identity_component(run_id),),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def list_pipeline_runs(
    db_path: Path,
    *,
    canonical_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List pipeline runs, newest first, optionally filtered by asset/status.

    Additive M6-04 read helper (pipeline progression persistence).
    """
    conn = open_operations_store(Path(db_path))
    try:
        where: list[str] = []
        params: list[Any] = []
        if canonical_id is not None:
            where.append("canonical_id = ?")
            params.append(normalize_identity_component(canonical_id))
        if status is not None:
            where.append("status = ?")
            params.append(normalize_identity_component(status).upper())
        sql = "SELECT * FROM pipeline_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, run_id DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Jobs: enqueue / idempotency / transitions
# ----------------------------------------------------------------------


def enqueue_job(
    db_path: Path,
    platform: str,
    platform_content_id: str,
    stage: str,
    input_fingerprint: str,
    *,
    policy_version: str,
    canonical_id: str,
    pipeline_run_id: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    priority: int = 0,
    required_capabilities: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> EnqueueResult:
    """Enqueue a stage job for an asset, deduplicated by deterministic identity.

    Idempotency contract (§13 M6-01 spec):
      - QUEUED/LEASED/RUNNING  -> no duplicate created (exists_active).
      - SUCCEEDED              -> existing-success SKIP.
      - FAILED_RETRYABLE       -> no new job; retry/requeue handles the same job.
      - FAILED_TERMINAL/CANCELLED -> not silently resurrected.
    Changed input_fingerprint or policy_version -> new identity -> new job.

    ``required_capabilities`` (M6-02) is capability-dispatch metadata: an
    all-of requirement the claiming worker must satisfy. It is NOT part of the
    deterministic job identity — a job re-enqueued with identical identity but
    different capability requirements collapses to the same job.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    platform = normalize_identity_component(platform)
    content_id = normalize_identity_component(platform_content_id)
    stage = normalize_job_stage(stage)
    fingerprint = normalize_input_fingerprint(input_fingerprint)
    policy_version = normalize_identity_component(policy_version)
    canonical_id = normalize_identity_component(canonical_id)
    if max_attempts < 1:
        raise OperationsIntegrityError("max_attempts must be >= 1")
    required_caps = normalize_capabilities(required_capabilities)

    job_id = compute_job_id(platform, content_id, stage, fingerprint, policy_version)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            asset = conn.execute(
                "SELECT * FROM assets WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
            if asset is None:
                conn.rollback()
                raise OperationsIntegrityError(
                    f"asset canonical_id={canonical_id} is not registered"
                )
            if (
                asset["platform"] != platform
                or asset["platform_content_id"] != content_id
            ):
                conn.rollback()
                raise OperationsIntegrityError(
                    f"asset {canonical_id} identity does not match "
                    f"({platform}, {content_id})"
                )
            if pipeline_run_id is not None:
                run = conn.execute(
                    "SELECT * FROM pipeline_runs WHERE run_id=?", (pipeline_run_id,)
                ).fetchone()
                if run is None:
                    conn.rollback()
                    raise OperationsIntegrityError(
                        f"pipeline run {pipeline_run_id} not found"
                    )

            existing = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()

            if existing is not None:
                state = existing["state"]
                message = "duplicate job enqueue suppressed"
                if state == JobState.SUCCEEDED.value:
                    _append_event(
                        conn,
                        EVENT_JOB_SKIPPED,
                        timestamp=now,
                        canonical_id=canonical_id,
                        job_id=job_id,
                        pipeline_run_id=pipeline_run_id,
                        message="existing-success SKIP (idempotency cache hit)",
                    )
                    conn.commit()
                    return EnqueueResult(
                        job_id=job_id,
                        state=state,
                        outcome="existing_success",
                        created=False,
                        message=message,
                    )
                if state == JobState.FAILED_RETRYABLE.value:
                    _append_event(
                        conn,
                        EVENT_JOB_ENQUEUED,
                        timestamp=now,
                        canonical_id=canonical_id,
                        job_id=job_id,
                        pipeline_run_id=pipeline_run_id,
                        message="job exists in FAILED_RETRYABLE; retry handles it",
                    )
                    conn.commit()
                    return EnqueueResult(
                        job_id=job_id,
                        state=state,
                        outcome="exists_retryable",
                        created=False,
                        message=message,
                    )
                if state in TERMINAL_JOB_STATES:
                    conn.commit()
                    outcome = (
                        "exists_terminal"
                        if state == JobState.FAILED_TERMINAL.value
                        else "exists_cancelled"
                    )
                    return EnqueueResult(
                        job_id=job_id,
                        state=state,
                        outcome=outcome,
                        created=False,
                        message=f"job exists in {state}; not silently revived",
                    )
                conn.commit()
                return EnqueueResult(
                    job_id=job_id,
                    state=state,
                    outcome="exists_active",
                    created=False,
                    message=message,
                )

            conn.execute(
                """
                INSERT INTO jobs(
                    job_id, platform, platform_content_id, canonical_id, stage,
                    state, input_fingerprint, policy_version, pipeline_run_id,
                    priority, max_attempts, attempt_count, next_retry_at,
                    lease_owner, leased_at, lease_expires_at, lease_token,
                    required_capabilities_json,
                    enqueued_at, updated_at, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    platform,
                    content_id,
                    canonical_id,
                    stage,
                    JobState.QUEUED.value,
                    fingerprint,
                    policy_version,
                    pipeline_run_id,
                    priority,
                    max_attempts,
                    _json_dumps(required_caps),
                    now,
                    now,
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            _append_event(
                conn,
                EVENT_JOB_ENQUEUED,
                timestamp=now,
                canonical_id=canonical_id,
                job_id=job_id,
                pipeline_run_id=pipeline_run_id,
                to_state=JobState.QUEUED.value,
                message=f"job {job_id} enqueued ({stage})",
            )
            conn.commit()
            return EnqueueResult(
                job_id=job_id,
                state=JobState.QUEUED.value,
                outcome="enqueued",
                created=True,
                message="job enqueued",
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def get_job(db_path: Path, job_id: str) -> Optional[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (normalize_identity_component(job_id),)
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def list_jobs(
    db_path: Path,
    *,
    state: Optional[str] = None,
    stage: Optional[str] = None,
    canonical_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        where: list[str] = []
        params: list[Any] = []
        if state is not None:
            where.append("state = ?")
            params.append(normalize_identity_component(state).upper())
        if stage is not None:
            where.append("stage = ?")
            params.append(normalize_job_stage(stage))
        if canonical_id is not None:
            where.append("canonical_id = ?")
            params.append(normalize_identity_component(canonical_id))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY priority DESC, enqueued_at ASC, job_id ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


def list_pending_jobs(
    db_path: Path, *, stage: Optional[str] = None, canonical_id: Optional[str] = None
) -> list[dict[str, Any]]:
    return list_jobs(
        db_path, state=JobState.QUEUED.value, stage=stage, canonical_id=canonical_id
    )


def list_failed_jobs(
    db_path: Path, *, stage: Optional[str] = None, canonical_id: Optional[str] = None
) -> list[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        where = ["state IN (?, ?)"]
        params: list[Any] = [
            JobState.FAILED_RETRYABLE.value,
            JobState.FAILED_TERMINAL.value,
        ]
        if stage is not None:
            where.append("stage = ?")
            params.append(normalize_job_stage(stage))
        if canonical_id is not None:
            where.append("canonical_id = ?")
            params.append(normalize_identity_component(canonical_id))
        sql = "SELECT * FROM jobs WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at ASC, job_id ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


def _load_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise OperationsIntegrityError(f"job {job_id} not found")
    return row


def _write_job_state(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    to_state: str,
    *,
    now: str,
    reason: Optional[str] = None,
    next_retry_at: Optional[str] = None,
    lease_cleared: bool = False,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    from_state = job["state"]
    conn.execute(
        """
        UPDATE jobs SET state=?, updated_at=?, next_retry_at=?,
            lease_owner=CASE WHEN ? THEN NULL ELSE lease_owner END,
            leased_at=CASE WHEN ? THEN NULL ELSE leased_at END,
            lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
            lease_token=CASE WHEN ? THEN NULL ELSE lease_token END
        WHERE job_id=?
        """,
        (
            to_state,
            now,
            next_retry_at,
            int(lease_cleared),
            int(lease_cleared),
            int(lease_cleared),
            int(lease_cleared),
            job["job_id"],
        ),
    )
    _append_event(
        conn,
        EVENT_JOB_STATE_TRANSITION,
        timestamp=now,
        canonical_id=job["canonical_id"],
        job_id=job["job_id"],
        pipeline_run_id=job["pipeline_run_id"],
        from_state=from_state,
        to_state=to_state,
        message=f"job {job['job_id']} {from_state} -> {to_state}"
        + (f": {reason}" if reason else ""),
        metadata=metadata,
    )


def transition_job_state(
    db_path: Path,
    job_id: str,
    to_state: str,
    *,
    reason: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Central validated job state transition.

    Raises OperationsStateError on any illegal transition. A job in a terminal
    state cannot be moved by a normal transition (no silent revival).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)
    to_state = normalize_identity_component(to_state).upper()
    if to_state not in {s.value for s in JobState}:
        raise OperationsStateError(f"unknown job state: {to_state!r}")

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            from_state = job["state"]
            if from_state == to_state:
                conn.commit()
                return _row_dict(job)
            if not is_legal_job_transition(from_state, to_state):
                conn.rollback()
                raise OperationsStateError(
                    f"illegal job state transition {from_state} -> {to_state} "
                    f"for {job_id}"
                )
            if to_state == JobState.LEASED.value and not job["lease_owner"]:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot LEASED job {job_id} without an active lease "
                    f"(set_job_lease first)"
                )
            # LEASED -> QUEUED is the lease-expiry recovery path: clear the lease.
            lease_cleared = (
                from_state == JobState.LEASED.value
                and to_state == JobState.QUEUED.value
            )
            _write_job_state(
                conn,
                job,
                to_state,
                now=now,
                reason=reason,
                lease_cleared=lease_cleared,
                metadata={"reason": reason} if reason else None,
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def cancel_job(
    db_path: Path,
    job_id: str,
    *,
    reason: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Admin cancel. QUEUED/FAILED_RETRYABLE -> CANCELLED; otherwise no-op.

    Terminal (SUCCEEDED/FAILED_TERMINAL/CANCELLED) and active (LEASED/RUNNING)
    jobs are rejected as a deterministic no-op.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            from_state = job["state"]
            if from_state not in (
                JobState.QUEUED.value,
                JobState.FAILED_RETRYABLE.value,
            ):
                conn.commit()
                return _row_dict(job)
            _write_job_state(
                conn,
                job,
                JobState.CANCELLED.value,
                now=now,
                reason=reason or "admin cancel",
                lease_cleared=True,
                metadata={"reason": reason} if reason else None,
            )
            _append_event(
                conn,
                EVENT_JOB_CANCELLED,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                from_state=from_state,
                to_state=JobState.CANCELLED.value,
                message=f"job {job_id} cancelled",
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def requeue_retryable_job(
    db_path: Path,
    job_id: str,
    *,
    reason: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Requeue a FAILED_RETRYABLE job back to QUEUED (backoff already elapsed).

    Retry-exhausted jobs (attempt_count >= max_attempts) cannot be requeued —
    they must be FAILED_TERMINAL (finish_attempt enforces this).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            from_state = job["state"]
            if from_state in TERMINAL_JOB_STATES:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot requeue terminal job {job_id} in {from_state}"
                )
            if from_state != JobState.FAILED_RETRYABLE.value:
                conn.commit()
                return _row_dict(job)
            if job["attempt_count"] >= job["max_attempts"]:
                conn.rollback()
                raise OperationsStateError(
                    f"job {job_id} retries exhausted "
                    f"({job['attempt_count']}/{job['max_attempts']})"
                )
            _write_job_state(
                conn,
                job,
                JobState.QUEUED.value,
                now=now,
                reason=reason or "requeue after retryable failure",
                next_retry_at=None,
                lease_cleared=True,
                metadata={"reason": reason} if reason else None,
            )
            _append_event(
                conn,
                EVENT_JOB_REQUEUED,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                from_state=from_state,
                to_state=JobState.QUEUED.value,
                message=f"job {job_id} requeued",
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Lease fields (persistence only in M6-01; protocol is M6-02)
# ----------------------------------------------------------------------


def set_job_lease(
    db_path: Path,
    job_id: str,
    *,
    lease_owner: str,
    lease_expires_at: str,
    lease_token: Optional[str] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)
    lease_owner = normalize_identity_component(lease_owner)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            if job["state"] in TERMINAL_JOB_STATES:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot lease terminal job {job_id} ({job['state']})"
                )
            conn.execute(
                """
                UPDATE jobs SET lease_owner=?, leased_at=?, lease_expires_at=?,
                    lease_token=?, updated_at=? WHERE job_id=?
                """,
                (lease_owner, now, lease_expires_at, lease_token, now, job_id),
            )
            _append_event(
                conn,
                EVENT_LEASE_SET,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                message=f"lease set for job {job_id}",
                metadata={"lease_owner": lease_owner, "lease_expires_at": lease_expires_at},
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def clear_job_lease(
    db_path: Path,
    job_id: str,
    *,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            conn.execute(
                """
                UPDATE jobs SET lease_owner=NULL, leased_at=NULL,
                    lease_expires_at=NULL, lease_token=NULL, updated_at=?
                WHERE job_id=?
                """,
                (now, job_id),
            )
            _append_event(
                conn,
                EVENT_LEASE_CLEARED,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                message=f"lease cleared for job {job_id}",
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Job attempts
# ----------------------------------------------------------------------

_ATTEMPT_ID_PREFIX = "att_"


def _attempt_id(job_id: str, attempt_number: int) -> str:
    import hashlib

    raw = f"{job_id}|{attempt_number}"
    return _ATTEMPT_ID_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def begin_attempt(
    db_path: Path,
    job_id: str,
    *,
    worker_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Begin a new attempt for a job (monotonic attempt_number from 1)."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job(conn, job_id)
            if job["state"] not in (JobState.LEASED.value, JobState.RUNNING.value):
                conn.rollback()
                raise OperationsStateError(
                    f"cannot begin attempt for job {job_id} in state {job['state']}"
                )
            last = conn.execute(
                "SELECT MAX(attempt_number) AS n FROM job_attempts WHERE job_id=?",
                (job_id,),
            ).fetchone()
            attempt_number = (last["n"] or 0) + 1
            attempt_id = _attempt_id(job_id, attempt_number)
            conn.execute(
                """
                INSERT INTO job_attempts(
                    attempt_id, job_id, attempt_number, worker_id, started_at,
                    finished_at, outcome, error_class, error_message, retryable,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    attempt_number,
                    worker_id,
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            conn.execute(
                "UPDATE jobs SET attempt_count=?, updated_at=? WHERE job_id=?",
                (attempt_number, now, job_id),
            )
            _append_event(
                conn,
                EVENT_ATTEMPT_BEGUN,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                worker_id=worker_id,
                message=f"attempt {attempt_number} begun for job {job_id}",
                metadata={"attempt_number": attempt_number, "worker_id": worker_id},
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM job_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def finish_attempt(
    db_path: Path,
    attempt_id: str,
    *,
    outcome: str,
    retryable: bool = False,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Finish an attempt and drive the job state atomically.

    outcome == "succeeded"  -> job RUNNING -> SUCCEEDED
    outcome == "failed" and retryable and attempt < max_attempts
                           -> job RUNNING -> FAILED_RETRYABLE (next_retry_at set)
    otherwise              -> job RUNNING -> FAILED_TERMINAL (exhaustion or non-retryable)

    All job counters, attempt row, and events are committed in one transaction.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    attempt_id = normalize_identity_component(attempt_id)
    outcome = normalize_identity_component(outcome).lower()
    if outcome not in ("succeeded", "failed"):
        raise OperationsStateError(f"unknown attempt outcome: {outcome!r}")

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            attempt = conn.execute(
                "SELECT * FROM job_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                conn.rollback()
                raise OperationsIntegrityError(f"attempt {attempt_id} not found")
            if attempt["finished_at"] is not None:
                conn.rollback()
                raise OperationsStateError(f"attempt {attempt_id} already finished")

            job = _load_job(conn, attempt["job_id"])
            if job["state"] not in (
                JobState.LEASED.value,
                JobState.RUNNING.value,
            ):
                conn.rollback()
                raise OperationsStateError(
                    f"cannot finish attempt for job {job['job_id']} in {job['state']}"
                )

            conn.execute(
                """
                UPDATE job_attempts SET finished_at=?, outcome=?, error_class=?,
                    error_message=?, retryable=? WHERE attempt_id=?
                """,
                (
                    now,
                    outcome,
                    error_class,
                    error_message,
                    int(bool(retryable)),
                    attempt_id,
                ),
            )

            if outcome == "succeeded":
                to_state = JobState.SUCCEEDED.value
                next_retry_at = None
            elif retryable and attempt["attempt_number"] < job["max_attempts"]:
                to_state = JobState.FAILED_RETRYABLE.value
                next_retry_at = compute_next_retry_at(
                    now, attempt["attempt_number"]
                )
            else:
                to_state = JobState.FAILED_TERMINAL.value
                next_retry_at = None

            _write_job_state(
                conn,
                job,
                to_state,
                now=now,
                reason=(
                    f"attempt {attempt['attempt_number']} {outcome}"
                    + (
                        " (retryable)"
                        if retryable and to_state == JobState.FAILED_RETRYABLE.value
                        else ""
                    )
                ),
                next_retry_at=next_retry_at,
                lease_cleared=(
                    to_state in TERMINAL_JOB_STATES
                    or to_state == JobState.FAILED_RETRYABLE.value
                ),
                metadata={
                    "attempt_number": attempt["attempt_number"],
                    "outcome": outcome,
                    "retryable": bool(retryable),
                    "error_class": error_class,
                },
            )
            _append_event(
                conn,
                EVENT_ATTEMPT_FINISHED,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job["job_id"],
                pipeline_run_id=job["pipeline_run_id"],
                to_state=to_state,
                message=(
                    f"attempt {attempt['attempt_number']} finished for job "
                    f"{job['job_id']} -> {to_state}"
                ),
                metadata={
                    "attempt_number": attempt["attempt_number"],
                    "outcome": outcome,
                    "retryable": bool(retryable),
                    "error_class": error_class,
                },
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM job_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def list_job_attempts(db_path: Path, job_id: str) -> list[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        rows = conn.execute(
            "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number ASC",
            (normalize_identity_component(job_id),),
        ).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


def get_job_result(db_path: Path, job_id: str) -> Optional[dict[str, Any]]:
    """Recover the durable StageExecutionResult of a job's latest successful
    attempt (M6-03 §27 downstream fingerprint handoff).

    Returns the ``stage_result`` metadata dict (containing output_fingerprint
    and artifact descriptors) or ``None`` when no successful attempt recorded a
    stage result yet. The output_fingerprint of stage N becomes the
    input_fingerprint of stage N+1; M6-04 reads it here instead of rescanning
    artifacts.
    """
    db_path = Path(db_path)
    job_id = normalize_identity_component(job_id)
    conn = open_operations_store(db_path)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM job_attempts "
            "WHERE job_id=? AND outcome='succeeded' "
            "ORDER BY attempt_number DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        meta = _json_loads(row["metadata_json"]) or {}
        stage_result = meta.get("stage_result")
        return stage_result if isinstance(stage_result, dict) else None
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Workers (persistence only)
# ----------------------------------------------------------------------


def register_worker(
    db_path: Path,
    worker_id: str,
    capabilities: list[str],
    *,
    display_name: Optional[str] = None,
    hostname: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    worker_id = normalize_identity_component(worker_id)
    capabilities = [normalize_identity_component(c) for c in capabilities]

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE workers SET display_name=?, hostname=?, capabilities_json=?,
                        status=?, last_heartbeat_at=?, updated_at=?, metadata_json=?
                    WHERE worker_id=?
                    """,
                    (
                        display_name,
                        hostname,
                        _json_dumps(capabilities),
                        "registered",
                        now,
                        now,
                        _json_dumps(metadata or {}),
                        worker_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO workers(
                        worker_id, display_name, hostname, capabilities_json,
                        status, last_heartbeat_at, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, 'registered', ?, ?, ?, ?)
                    """,
                    (
                        worker_id,
                        display_name,
                        hostname,
                        _json_dumps(capabilities),
                        now,
                        now,
                        now,
                        _json_dumps(metadata or {}),
                    ),
                )
            _append_event(
                conn,
                EVENT_WORKER_REGISTERED,
                timestamp=now,
                worker_id=worker_id,
                message=f"worker {worker_id} registered",
                metadata={"capabilities": capabilities, "display_name": display_name},
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def worker_heartbeat(
    db_path: Path,
    worker_id: str,
    *,
    capabilities: Optional[list[str]] = None,
    status: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    now = now or utc_now_iso()
    worker_id = normalize_identity_component(worker_id)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise OperationsIntegrityError(f"worker {worker_id} is not registered")
            new_caps = _json_dumps(
                [normalize_identity_component(c) for c in capabilities]
                if capabilities is not None
                else row["capabilities_json"]
            )
            new_status = status or row["status"]
            conn.execute(
                """
                UPDATE workers SET capabilities_json=?, status=?, last_heartbeat_at=?,
                    updated_at=? WHERE worker_id=?
                """,
                (new_caps, new_status, now, now, worker_id),
            )
            _append_event(
                conn,
                EVENT_WORKER_HEARTBEAT,
                timestamp=now,
                worker_id=worker_id,
                message=f"worker {worker_id} heartbeat",
                metadata={"capabilities": _json_loads(new_caps), "status": new_status},
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def list_workers(db_path: Path) -> list[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        rows = conn.execute("SELECT * FROM workers ORDER BY worker_id ASC").fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# M6-02 Capability-aware claim + lease protocol
#
# Execution model: durable queue + atomic claim + lease + fencing token +
# idempotent stage execution => at-least-once semantics (NOT exactly-once).
# Only the current lease holder may mutate an active job; every ownership
# mutation carries (job_id, worker_id, lease_token).
# ----------------------------------------------------------------------


def _load_job_or_none(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def _check_lease_holder(
    conn: sqlite3.Connection, job: sqlite3.Row, worker_id: str, lease_token: str
) -> None:
    """Verify the mutation caller still owns the current lease. Raises
    StaleLeaseError on any mismatch (token is the fence, not just worker_id)."""
    if job["state"] not in LEASE_HELD_JOB_STATES:
        raise StaleLeaseError(
            f"job {job['job_id']} is in {job['state']}; no active lease to mutate"
        )
    if job["lease_owner"] != worker_id or job["lease_token"] != lease_token:
        raise StaleLeaseError(
            f"job {job['job_id']} lease no longer held by "
            f"{worker_id}/{lease_token} (current {job['lease_owner']}/"
            f"{job['lease_token']})"
        )


def _job_to_claimed(
    job: sqlite3.Row, worker_id: str, now: str, lease_expires_at: str, lease_token: str
) -> ClaimedJob:
    return ClaimedJob(
        job_id=job["job_id"],
        stage=job["stage"],
        canonical_id=job["canonical_id"],
        platform=job["platform"],
        platform_content_id=job["platform_content_id"],
        input_fingerprint=job["input_fingerprint"],
        required_capabilities=frozenset(_json_loads(job["required_capabilities_json"])),
        lease_owner=worker_id,
        leased_at=now,
        lease_expires_at=lease_expires_at,
        _lease_token=lease_token,
        metadata=_json_loads(job["metadata_json"]) or {},
    )


def claim_next_job(
    db_path: Path,
    worker_id: str,
    capabilities: list[str],
    *,
    lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    now: Optional[str] = None,
) -> Optional[ClaimedJob]:
    """Atomically claim the next eligible QUEUED job this worker can run.

    Eligibility (checked inside one ``BEGIN IMMEDIATE`` transaction):
      - state == QUEUED
      - retry deadline reached: ``next_retry_at`` is NULL or <= now
      - required_capabilities ⊆ worker capabilities (exact names; empty = generic)
    Ordering: priority DESC, enqueued_at ASC, job_id ASC (deterministic v1).

    On success the job is moved QUEUED -> LEASED with a fresh lease token and
    the lease fields written; the claim event is committed atomically. Returns
    None when no eligible job exists (including "no matching capability").
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    worker_id = normalize_identity_component(worker_id)
    caps = normalize_capabilities(capabilities)
    lease_expires_at = format_iso(
        parse_iso(now) + timedelta(seconds=lease_duration_seconds)
    )
    lease_token = new_lease_token()

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state = ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority DESC, enqueued_at ASC, job_id ASC
                """,
                (JobState.QUEUED.value, now),
            ).fetchall()
            for job in rows:
                required = _json_loads(job["required_capabilities_json"])
                if not _capability_match(required, caps):
                    continue
                conn.execute(
                    """
                    UPDATE jobs SET state=?, lease_owner=?, leased_at=?,
                        lease_expires_at=?, lease_token=?, updated_at=?
                    WHERE job_id=?
                    """,
                    (
                        JobState.LEASED.value,
                        worker_id,
                        now,
                        lease_expires_at,
                        lease_token,
                        now,
                        job["job_id"],
                    ),
                )
                _append_event(
                    conn,
                    EVENT_JOB_CLAIMED,
                    timestamp=now,
                    canonical_id=job["canonical_id"],
                    job_id=job["job_id"],
                    pipeline_run_id=job["pipeline_run_id"],
                    worker_id=worker_id,
                    from_state=JobState.QUEUED.value,
                    to_state=JobState.LEASED.value,
                    message=f"job {job['job_id']} claimed by {worker_id}",
                    metadata={"lease_expires_at": lease_expires_at},
                )
                conn.commit()
                return _job_to_claimed(
                    job, worker_id, now, lease_expires_at, lease_token
                )
            conn.rollback()
            return None
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def renew_job_lease(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Renew the current holder's lease. Requires state ∈ {LEASED, RUNNING} and
    owner + token match. No audit event per renewal (avoids event-log churn)."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)
    worker_id = normalize_identity_component(worker_id)
    lease_token = normalize_identity_component(lease_token)
    lease_expires_at = format_iso(
        parse_iso(now) + timedelta(seconds=lease_duration_seconds)
    )

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job_or_none(conn, job_id)
            if job is None:
                conn.rollback()
                raise OperationsIntegrityError(f"job {job_id} not found")
            _check_lease_holder(conn, job, worker_id, lease_token)
            conn.execute(
                """
                UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE job_id=?
                """,
                (lease_expires_at, now, job_id),
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def start_claimed_job(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    now: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Atomically start a claimed job: LEASED -> RUNNING and create attempt #n.

    The attempt is created in the SAME transaction as the RUNNING transition,
    so attempt accounting reflects "entered RUNNING" (a crash right after start
    already consumed one attempt). Caller must hold the current lease token.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)
    worker_id = normalize_identity_component(worker_id)
    lease_token = normalize_identity_component(lease_token)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job_or_none(conn, job_id)
            if job is None:
                conn.rollback()
                raise OperationsIntegrityError(f"job {job_id} not found")
            _check_lease_holder(conn, job, worker_id, lease_token)
            if job["state"] != JobState.LEASED.value:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot start job {job_id} from {job['state']} (expected LEASED)"
                )
            if job["lease_expires_at"] is not None and job["lease_expires_at"] <= now:
                conn.rollback()
                raise StaleLeaseError(
                    f"job {job_id} lease expired at {job['lease_expires_at']}"
                )

            from_state = job["state"]
            conn.execute(
                """
                UPDATE jobs SET state=?, updated_at=? WHERE job_id=?
                """,
                (JobState.RUNNING.value, now, job_id),
            )
            _append_event(
                conn,
                EVENT_JOB_STATE_TRANSITION,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                worker_id=worker_id,
                from_state=from_state,
                to_state=JobState.RUNNING.value,
                message=f"job {job_id} LEASED -> RUNNING",
            )

            last = conn.execute(
                "SELECT MAX(attempt_number) AS n FROM job_attempts WHERE job_id=?",
                (job_id,),
            ).fetchone()
            attempt_number = (last["n"] or 0) + 1
            attempt_id = _attempt_id(job_id, attempt_number)
            conn.execute(
                """
                INSERT INTO job_attempts(
                    attempt_id, job_id, attempt_number, worker_id, started_at,
                    finished_at, outcome, error_class, error_message, retryable,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    attempt_number,
                    worker_id,
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            conn.execute(
                "UPDATE jobs SET attempt_count=?, updated_at=? WHERE job_id=?",
                (attempt_number, now, job_id),
            )
            _append_event(
                conn,
                EVENT_ATTEMPT_BEGUN,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                worker_id=worker_id,
                message=f"attempt {attempt_number} begun for job {job_id}",
                metadata={"attempt_number": attempt_number, "worker_id": worker_id},
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _active_attempt(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM job_attempts
        WHERE job_id=? AND finished_at IS NULL
        ORDER BY attempt_number DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()


def _complete_with_attempt_outcome(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    outcome: str,
    retryable: bool,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    now: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Shared completion primitive: finish the active attempt and drive the job
    terminal/retryable state atomically. Token is the fence; stale token can
    never commit a completion."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    job_id = normalize_identity_component(job_id)
    worker_id = normalize_identity_component(worker_id)
    lease_token = normalize_identity_component(lease_token)

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = _load_job_or_none(conn, job_id)
            if job is None:
                conn.rollback()
                raise OperationsIntegrityError(f"job {job_id} not found")
            _check_lease_holder(conn, job, worker_id, lease_token)
            if job["state"] != JobState.RUNNING.value:
                conn.rollback()
                raise OperationsStateError(
                    f"cannot complete job {job_id} from {job['state']} "
                    f"(expected RUNNING)"
                )
            attempt = _active_attempt(conn, job_id)
            if attempt is None:
                conn.rollback()
                raise OperationsStateError(
                    f"job {job_id} RUNNING without an active attempt"
                )

            conn.execute(
                """
                UPDATE job_attempts SET finished_at=?, outcome=?, error_class=?,
                    error_message=?, retryable=?, metadata_json=?
                WHERE attempt_id=?
                """,
                (
                    now,
                    outcome,
                    error_class,
                    error_message,
                    int(bool(retryable)),
                    _json_dumps(metadata or {}),
                    attempt["attempt_id"],
                ),
            )

            if outcome == "succeeded":
                to_state = JobState.SUCCEEDED.value
                next_retry_at = None
            elif retryable and attempt["attempt_number"] < job["max_attempts"]:
                to_state = JobState.FAILED_RETRYABLE.value
                next_retry_at = compute_next_retry_at(now, attempt["attempt_number"])
            else:
                to_state = JobState.FAILED_TERMINAL.value
                next_retry_at = None

            _write_job_state(
                conn,
                job,
                to_state,
                now=now,
                reason=(
                    f"attempt {attempt['attempt_number']} {outcome}"
                    + (
                        " (retryable)"
                        if retryable and to_state == JobState.FAILED_RETRYABLE.value
                        else ""
                    )
                ),
                next_retry_at=next_retry_at,
                lease_cleared=(
                    to_state in TERMINAL_JOB_STATES
                    or to_state == JobState.FAILED_RETRYABLE.value
                ),
                metadata={
                    "attempt_number": attempt["attempt_number"],
                    "outcome": outcome,
                    "retryable": bool(retryable),
                    "error_class": error_class,
                },
            )
            _append_event(
                conn,
                EVENT_ATTEMPT_FINISHED,
                timestamp=now,
                canonical_id=job["canonical_id"],
                job_id=job_id,
                pipeline_run_id=job["pipeline_run_id"],
                worker_id=worker_id,
                to_state=to_state,
                message=(
                    f"attempt {attempt['attempt_number']} finished for job "
                    f"{job_id} -> {to_state}"
                ),
                metadata={
                    "attempt_number": attempt["attempt_number"],
                    "outcome": outcome,
                    "retryable": bool(retryable),
                    "error_class": error_class,
                },
            )
            conn.commit()
            return _row_dict(
                conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def complete_job_success(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    now: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Commit a successful execution. Requires RUNNING + current lease token."""
    return _complete_with_attempt_outcome(
        db_path,
        job_id,
        worker_id,
        lease_token,
        outcome="succeeded",
        retryable=False,
        now=now,
        metadata=metadata,
    )


def complete_job_retryable_failure(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    now: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Report a retryable failure; store/protocol decides FAILED_RETRYABLE vs
    FAILED_TERMINAL from attempt_count/max_attempts. Requires RUNNING + token."""
    return _complete_with_attempt_outcome(
        db_path,
        job_id,
        worker_id,
        lease_token,
        outcome="failed",
        retryable=True,
        error_class=error_class,
        error_message=error_message,
        now=now,
        metadata=metadata,
    )


def complete_job_terminal_failure(
    db_path: Path,
    job_id: str,
    worker_id: str,
    lease_token: str,
    *,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    now: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Report a terminal failure. Requires RUNNING + current lease token."""
    return _complete_with_attempt_outcome(
        db_path,
        job_id,
        worker_id,
        lease_token,
        outcome="failed",
        retryable=False,
        error_class=error_class,
        error_message=error_message,
        now=now,
        metadata=metadata,
    )


def recover_expired_leases(
    db_path: Path, *, now: Optional[str] = None
) -> dict[str, Any]:
    """Control-plane recovery of every job whose lease has expired.

    - LEASED (claimed but not started): -> QUEUED, clear lease, no attempt
      consumed, ``LEASE_EXPIRED_REQUEUED`` event.
    - RUNNING (worker vanished mid-execution): close the active attempt as a
      lease-expired retryable failure (the attempt counts), then the job follows
      the frozen recovery policy: attempt < max -> FAILED_RETRYABLE with backoff
      (never immediately reclaimed — avoids crash loops); exhausted ->
      FAILED_TERMINAL.
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    recovered_leased: list[str] = []
    recovered_running: list[str] = []
    terminal: list[str] = []

    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                ORDER BY job_id ASC
                """,
                (JobState.LEASED.value, JobState.RUNNING.value, now),
            ).fetchall()
            for job in rows:
                jid = job["job_id"]
                if job["state"] == JobState.LEASED.value:
                    _write_job_state(
                        conn,
                        job,
                        JobState.QUEUED.value,
                        now=now,
                        reason="lease expired before start",
                        lease_cleared=True,
                    )
                    _append_event(
                        conn,
                        EVENT_LEASE_EXPIRED_REQUEUED,
                        timestamp=now,
                        canonical_id=job["canonical_id"],
                        job_id=jid,
                        pipeline_run_id=job["pipeline_run_id"],
                        worker_id=job["lease_owner"],
                        from_state=JobState.LEASED.value,
                        to_state=JobState.QUEUED.value,
                        message=f"job {jid} lease expired before start; requeued",
                    )
                    recovered_leased.append(jid)
                    continue

                # RUNNING: close active attempt as lease-expired retryable.
                attempt = _active_attempt(conn, jid)
                if attempt is None:
                    _write_job_state(
                        conn,
                        job,
                        JobState.FAILED_RETRYABLE.value,
                        now=now,
                        reason="lease expired with no active attempt",
                        next_retry_at=compute_next_retry_at(now, job["attempt_count"] + 1),
                        lease_cleared=True,
                    )
                    _append_event(
                        conn,
                        EVENT_LEASE_EXPIRED_REQUEUED,
                        timestamp=now,
                        canonical_id=job["canonical_id"],
                        job_id=jid,
                        pipeline_run_id=job["pipeline_run_id"],
                        worker_id=job["lease_owner"],
                        from_state=JobState.RUNNING.value,
                        to_state=JobState.FAILED_RETRYABLE.value,
                        message=f"job {jid} lease expired without active attempt",
                    )
                    recovered_running.append(jid)
                    continue

                conn.execute(
                    """
                    UPDATE job_attempts SET finished_at=?, outcome='failed',
                        error_class='WorkerLeaseExpired',
                        error_message='worker lease expired while running',
                        retryable=1 WHERE attempt_id=?
                    """,
                    (now, attempt["attempt_id"]),
                )
                attempt_no = attempt["attempt_number"]
                if attempt_no < job["max_attempts"]:
                    to_state = JobState.FAILED_RETRYABLE.value
                    next_retry_at = compute_next_retry_at(now, attempt_no)
                    recovered_running.append(jid)
                else:
                    to_state = JobState.FAILED_TERMINAL.value
                    next_retry_at = None
                    terminal.append(jid)
                _write_job_state(
                    conn,
                    job,
                    to_state,
                    now=now,
                    reason=f"lease expired during attempt {attempt_no}",
                    next_retry_at=next_retry_at,
                    lease_cleared=True,
                )
                _append_event(
                    conn,
                    EVENT_ATTEMPT_CLOSED_EXPIRED,
                    timestamp=now,
                    canonical_id=job["canonical_id"],
                    job_id=jid,
                    pipeline_run_id=job["pipeline_run_id"],
                    worker_id=job["lease_owner"],
                    from_state=JobState.RUNNING.value,
                    to_state=to_state,
                    message=(
                        f"attempt {attempt_no} closed as lease-expired -> {to_state}"
                    ),
                    metadata={"attempt_number": attempt_no},
                )
            conn.commit()
            return {
                "recovered_leased": recovered_leased,
                "recovered_running": recovered_running,
                "terminal": terminal,
            }
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def is_worker_stale(
    db_path: Path,
    worker_id: str,
    *,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
    now: Optional[str] = None,
) -> bool:
    """Derived staleness from last_heartbeat_at + threshold. Never relies on the
    human ``status`` field alone. A worker that never heartbeated is stale."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    worker_id = normalize_identity_component(worker_id)
    conn = open_operations_store(db_path)
    try:
        row = conn.execute(
            "SELECT last_heartbeat_at FROM workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    if row["last_heartbeat_at"] is None:
        return True
    cutoff = format_iso(
        parse_iso(now) - timedelta(seconds=stale_threshold_seconds)
    )
    return row["last_heartbeat_at"] < cutoff


def list_workers_with_status(
    db_path: Path,
    *,
    stale_threshold_seconds: int = DEFAULT_WORKER_STALE_THRESHOLD_SECONDS,
    now: Optional[str] = None,
) -> list[dict[str, Any]]:
    """list_workers() plus a derived ``derived_status`` (active|stale) computed
    from last_heartbeat_at + threshold. Worker staleness never mutates jobs."""
    db_path = Path(db_path)
    now = now or utc_now_iso()
    workers = list_workers(db_path)
    for worker in workers:
        worker["derived_status"] = (
            "stale"
            if is_worker_stale(
                db_path,
                worker["worker_id"],
                stale_threshold_seconds=stale_threshold_seconds,
                now=now,
            )
            else "active"
        )
    return workers


# ----------------------------------------------------------------------
# Event log reads
# ----------------------------------------------------------------------


def list_events(
    db_path: Path,
    *,
    job_id: Optional[str] = None,
    canonical_id: Optional[str] = None,
    event_type: Optional[str] = None,
    worker_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    conn = open_operations_store(Path(db_path))
    try:
        where: list[str] = []
        params: list[Any] = []
        if job_id is not None:
            where.append("job_id = ?")
            params.append(normalize_identity_component(job_id))
        if canonical_id is not None:
            where.append("canonical_id = ?")
            params.append(normalize_identity_component(canonical_id))
        if event_type is not None:
            where.append("event_type = ?")
            params.append(normalize_identity_component(event_type))
        if worker_id is not None:
            where.append("worker_id = ?")
            params.append(normalize_identity_component(worker_id))
        sql = "SELECT * FROM event_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_rowid ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [_row_dict(r) for r in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Scheduler state (M6-04 additive control-plane persistence)
# ----------------------------------------------------------------------


def get_scheduler_state(db_path: Path, scheduler_key: str) -> Optional[dict[str, Any]]:
    """Read scheduler control state for a control-plane key (e.g. a source poll)."""
    conn = open_operations_store(Path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM scheduler_state WHERE scheduler_key=?",
            (normalize_identity_component(scheduler_key),),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def set_scheduler_state(
    db_path: Path,
    scheduler_key: str,
    *,
    last_scheduled_at: Optional[str] = None,
    last_completed_at: Optional[str] = None,
    next_due_at: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    now: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert scheduler control state for a control-plane key.

    Additive M6-04 persistence: poll timing/control state lives here, never a
    second copy of the M2 collector cursor/watermark (that stays in M2).
    """
    db_path = Path(db_path)
    now = now or utc_now_iso()
    key = normalize_identity_component(scheduler_key)
    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM scheduler_state WHERE scheduler_key=?", (key,)
            ).fetchone()
            merged_meta: dict[str, Any] = {}
            if existing is not None:
                merged_meta = _json_loads(existing["metadata_json"]) or {}
            if metadata is not None:
                merged_meta.update(dict(metadata))
            conn.execute(
                """
                INSERT INTO scheduler_state(
                    scheduler_key, last_scheduled_at, last_completed_at,
                    next_due_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scheduler_key) DO UPDATE SET
                    last_scheduled_at = COALESCE(excluded.last_scheduled_at, scheduler_state.last_scheduled_at),
                    last_completed_at = COALESCE(excluded.last_completed_at, scheduler_state.last_completed_at),
                    next_due_at = COALESCE(excluded.next_due_at, scheduler_state.next_due_at),
                    metadata_json = excluded.metadata_json
                """,
                (
                    key,
                    last_scheduled_at,
                    last_completed_at,
                    next_due_at,
                    _json_dumps(merged_meta),
                ),
            )
            conn.commit()
            return _row_dict(
                conn.execute(
                    "SELECT * FROM scheduler_state WHERE scheduler_key=?", (key,)
                ).fetchone()
            )
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def validate_operations_store(
    db_path: Path, *, now: Optional[str] = None
) -> OperationsValidationResult:
    """Structured validation of an operations store.

    Checks: schema version, integrity_check, foreign_key_check, enum validity
    (asset lifecycle / job state / job stage / run status / trigger type), job
    deterministic ID correctness, attempt numbering + attempt_count consistency,
    terminal-state invariants, lease/retry field coherence, event reference
    integrity. Returns structured result; never raises for content violations.
    """
    db_path = Path(db_path)
    violations: list[str] = []
    checks: dict[str, Any] = {}

    conn = open_operations_store(db_path)
    try:
        checks["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks["integrity_check"] = integrity
        if integrity != "ok":
            violations.append(f"PRAGMA integrity_check returned {integrity!r}")

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks["foreign_key_violations"] = len(fk)
        for row in fk:
            violations.append(f"foreign_key_check: {dict(row)}")

        schema_version = conn.execute(
            "SELECT value FROM operations_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = schema_version["value"] if schema_version else None
        policy_version = conn.execute(
            "SELECT value FROM operations_meta WHERE key='policy_version'"
        ).fetchone()
        policy_version = policy_version["value"] if policy_version else None

        asset_count = conn.execute("SELECT count(*) AS n FROM assets").fetchone()["n"]
        run_count = conn.execute("SELECT count(*) AS n FROM pipeline_runs").fetchone()["n"]
        job_count = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
        attempt_count = conn.execute(
            "SELECT count(*) AS n FROM job_attempts"
        ).fetchone()["n"]
        worker_count = conn.execute("SELECT count(*) AS n FROM workers").fetchone()["n"]
        event_count = conn.execute("SELECT count(*) AS n FROM event_log").fetchone()["n"]
        counts = {
            "assets": asset_count,
            "pipeline_runs": run_count,
            "jobs": job_count,
            "job_attempts": attempt_count,
            "workers": worker_count,
            "events": event_count,
        }

        valid_lifecycle = {s.value for s in AssetLifecycleState}
        valid_states = {s.value for s in JobState}
        valid_stages = {s.value for s in JobStage}
        valid_run_status = {s.value for s in PipelineRunStatus}

        bad_assets = conn.execute(
            "SELECT canonical_id, lifecycle_state FROM assets"
        ).fetchall()
        for row in bad_assets:
            if row["lifecycle_state"] not in valid_lifecycle:
                violations.append(
                    f"asset {row['canonical_id']} invalid lifecycle_state "
                    f"{row['lifecycle_state']!r}"
                )

        jobs = conn.execute("SELECT * FROM jobs").fetchall()
        for job in jobs:
            jid = job["job_id"]
            if job["state"] not in valid_states:
                violations.append(f"job {jid} invalid state {job['state']!r}")
            if job["stage"] not in valid_stages:
                violations.append(f"job {jid} invalid stage {job['stage']!r}")

            # Deterministic job ID correctness
            try:
                expected = compute_job_id(
                    job["platform"],
                    job["platform_content_id"],
                    job["stage"],
                    job["input_fingerprint"],
                    job["policy_version"],
                )
                if expected != jid:
                    violations.append(f"job {jid} id mismatch (expected {expected})")
            except ValueError as exc:
                violations.append(f"job {jid} identity components invalid: {exc}")

            # Retry field coherence
            if job["state"] == JobState.FAILED_RETRYABLE.value:
                if job["next_retry_at"] is None:
                    violations.append(f"job {jid} FAILED_RETRYABLE without next_retry_at")
                if job["attempt_count"] >= job["max_attempts"]:
                    violations.append(
                        f"job {jid} FAILED_RETRYABLE but attempts exhausted "
                        f"({job['attempt_count']}/{job['max_attempts']})"
                    )
            else:
                if job["next_retry_at"] is not None:
                    violations.append(
                        f"job {jid} has next_retry_at but state {job['state']}"
                    )

            # Lease field coherence
            if job["state"] in LEASE_HELD_JOB_STATES:
                if job["lease_owner"] is None or job["lease_expires_at"] is None:
                    violations.append(
                        f"job {jid} in {job['state']} without active lease"
                    )
                if (
                    job["leased_at"] is None
                    or job["lease_token"] is None
                ):
                    violations.append(
                        f"job {jid} in {job['state']} with incomplete lease "
                        f"(leased_at/lease_token required)"
                    )
            else:
                if (
                    job["lease_owner"] is not None
                    or job["leased_at"] is not None
                    or job["lease_expires_at"] is not None
                    or job["lease_token"] is not None
                ):
                    violations.append(
                        f"job {jid} has residual lease fields in state {job['state']}"
                    )

            # Required-capabilities JSON validity (M6-02 capability dispatch)
            try:
                required = _json_loads(job["required_capabilities_json"])
                if not isinstance(required, list) or not all(
                    isinstance(c, str) for c in required
                ):
                    violations.append(
                        f"job {jid} required_capabilities_json is not a list of strings"
                    )
                else:
                    for cap in required:
                        if cap not in VALID_CAPABILITIES:
                            violations.append(
                                f"job {jid} invalid required capability {cap!r}"
                            )
            except Exception:
                violations.append(
                    f"job {jid} required_capabilities_json not valid JSON"
                )

            # Active-attempt invariant (M6-02):
            #   RUNNING   -> exactly one unfinished attempt
            #   non-RUNNING -> zero unfinished attempts
            active_attempts = conn.execute(
                "SELECT attempt_number FROM job_attempts "
                "WHERE job_id=? AND finished_at IS NULL",
                (jid,),
            ).fetchall()
            active_n = len(active_attempts)
            if job["state"] == JobState.RUNNING.value:
                if active_n != 1:
                    violations.append(
                        f"job {jid} RUNNING must have exactly one active attempt "
                        f"(found {active_n})"
                    )
            else:
                if active_n != 0:
                    violations.append(
                        f"job {jid} in {job['state']} must have no active attempt "
                        f"(found {active_n})"
                    )

            # Attempt numbering + consistency
            attempts = conn.execute(
                "SELECT attempt_number FROM job_attempts WHERE job_id=? ORDER BY attempt_number ASC",
                (jid,),
            ).fetchall()
            numbers = [a["attempt_number"] for a in attempts]
            if numbers != list(range(1, len(numbers) + 1)):
                violations.append(f"job {jid} attempt numbering not contiguous: {numbers}")
            if job["attempt_count"] != len(numbers):
                violations.append(
                    f"job {jid} attempt_count={job['attempt_count']} != "
                    f"{len(numbers)} attempts"
                )

            # Terminal-state invariants
            if is_terminal_job_state(job["state"]) and job["attempt_count"] == 0 and job["state"] != JobState.CANCELLED.value:
                violations.append(
                    f"job {jid} terminal {job['state']} with zero attempts"
                )

        # Pipeline run status validity
        runs = conn.execute(
            "SELECT run_id, status, trigger_type FROM pipeline_runs"
        ).fetchall()
        for run in runs:
            if run["status"] not in valid_run_status:
                violations.append(f"run {run['run_id']} invalid status {run['status']!r}")
            if run["trigger_type"] not in VALID_TRIGGER_TYPES:
                violations.append(
                    f"run {run['run_id']} invalid trigger_type {run['trigger_type']!r}"
                )

        # Event reference integrity: job_id and canonical_id must point at real
        # rows. worker_id on an event/attempt is a diagnostic label (workers may
        # register at claim time in M6-02), so it is not a hard reference.
        event_refs = conn.execute(
            "SELECT event_rowid, job_id, canonical_id FROM event_log"
        ).fetchall()
        valid_canonicals = {
            r["canonical_id"] for r in conn.execute("SELECT canonical_id FROM assets")
        }
        bad_event_refs = 0
        for ev in event_refs:
            if ev["job_id"] is not None and get_job_ref(conn, ev["job_id"]) is None:
                bad_event_refs += 1
                violations.append(
                    f"event {ev['event_rowid']} references unknown job {ev['job_id']}"
                )
            if (
                ev["canonical_id"] is not None
                and ev["canonical_id"] not in valid_canonicals
            ):
                bad_event_refs += 1
                violations.append(
                    f"event {ev['event_rowid']} references unknown asset "
                    f"{ev['canonical_id']}"
                )
        checks["event_reference_violations"] = bad_event_refs

        # Worker capability JSON validity (M6-02 capability dispatch)
        workers = conn.execute(
            "SELECT worker_id, capabilities_json FROM workers"
        ).fetchall()
        bad_worker_caps = 0
        for wrow in workers:
            try:
                caps = _json_loads(wrow["capabilities_json"])
                if not isinstance(caps, list) or not all(
                    isinstance(c, str) for c in caps
                ):
                    violations.append(
                        f"worker {wrow['worker_id']} capabilities_json is not "
                        "a list of strings"
                    )
                    bad_worker_caps += 1
                else:
                    for cap in caps:
                        if cap not in VALID_CAPABILITIES:
                            violations.append(
                                f"worker {wrow['worker_id']} invalid capability "
                                f"{cap!r}"
                            )
                            bad_worker_caps += 1
            except Exception:
                violations.append(
                    f"worker {wrow['worker_id']} capabilities_json not valid JSON"
                )
                bad_worker_caps += 1
        checks["worker_capability_json_valid"] = bad_worker_caps == 0
        checks["active_attempt_invariant_valid"] = True
        checks["required_capability_json_valid"] = True

        checks["schema_version"] = schema_version
        checks["policy_version"] = policy_version
        checks["asset_lifecycle_enum_valid"] = all(
            r["lifecycle_state"] in valid_lifecycle for r in bad_assets
        )
        checks["job_state_stage_enum_valid"] = all(
            job["state"] in valid_states and job["stage"] in valid_stages for job in jobs
        )
        checks["run_status_enum_valid"] = all(
            r["status"] in valid_run_status for r in runs
        )
        checks["job_id_correctness_valid"] = all(
            compute_job_id(
                job["platform"],
                job["platform_content_id"],
                job["stage"],
                job["input_fingerprint"],
                job["policy_version"],
            )
            == job["job_id"]
            for job in jobs
        )
        checks["attempt_numbering_valid"] = True
        checks["terminal_lease_cleared"] = True
        checks["retry_field_coherent"] = True

        return OperationsValidationResult(
            valid=not violations,
            schema_version=schema_version or "",
            policy_version=policy_version or "",
            counts=counts,
            checks=checks,
            violations=violations,
        )
    finally:
        conn.close()


def get_job_ref(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT job_id FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()