"""SQLite Metadata Repository Engine for Douyin Collections (DY-C08).

Implements durable metadata persistence, run-scoped staging, atomic success
finalization, rollback semantics on failure, and append-only observation history.

Core Invariants:
1. RAW BEFORE TRANSFORM BEFORE DB PERSISTENCE:
   C04 Fetch -> C06 Archive -> C07 Transform -> C08 Persistence.
2. FAILED RUNS NEVER POLLUTE COMMITTED STATE:
   Failed runs never advance watermarks, never mark items inactive, and never
   mutate production collection_items.
3. DURABLE RUN STAGING + SHORT ATOMIC FINALIZE TRANSACTION:
   Network page requests never hold an open SQLite transaction. Mutations are
   staged in `run_item_staging` and applied in a single atomic BEGIN IMMEDIATE.
4. APPEND-ONLY OBSERVATION EVIDENCE:
   Discrete observations are immutable historical facts. Idempotent on identical
   replay; conflicting payloads for the same observation ID are rejected.
5. EXPLICIT INACTIVE TRANSITIONS:
   The repository never infers `active=false` on missing items. Inactive state
   changes are strictly driven by explicit caller instructions from verified runs.
6. CURSORS AS EXACT TEXT:
   All cursors and watermarks are stored as decimal TEXT strings, preserving precision.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .douyin.transform import CanonicalTransformResult, PriorCollectionState
from .download_models import DownloadTask, OutboxRecord
from .models import Watermark
from .repository_errors import (
    RepositoryConflictError,
    RepositoryCorruptError,
    RepositoryError,
    RepositoryInvalidInputError,
    RepositoryIOError,
    RepositoryNotFoundError,
    RepositoryNotInitializedError,
    RepositorySchemaError,
    RepositoryStateConflictError,
    RepositoryTransactionError,
)
from .repository_models import (
    CollectionItemRecord,
    CollectionObservationRecord,
    StagedItemRecord,
    SyncRunMode,
    SyncRunRecord,
    SyncRunStatus,
    SyncState,
)

logger = logging.getLogger(__name__)

DEFAULT_SCOPE_ID = "douyin:default"
DEFAULT_PLATFORM = "douyin"


def _utcnow_iso() -> str:
    """Returns the current UTC timestamp formatted as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_text(text: str | None) -> str:
    """Sanitizes text fields to prevent credential leakage in error messages."""
    if not text:
        return ""
    # Strip any potential cookie or token substring patterns
    lowered = text.lower()
    for sensitive in ("sessionid=", "cookie=", "authorization=", "token="):
        if sensitive in lowered:
            return "Sanitized error message (contained potential credential pattern)"
    return text


def _deterministic_json(data: Any) -> str:
    """Serializes data to deterministic, compact UTF-8 JSON text."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqliteMetadataRepository:
    """Production SQLite Metadata Repository for Collector services."""

    CURRENT_SCHEMA_VERSION = 3

    def __init__(
        self,
        db_path: str | Path,
        auto_init: bool = True,
        synchronous_mode: str = "FULL",
    ) -> None:
        self.db_path = Path(db_path)
        self.synchronous_mode = synchronous_mode.upper()
        self._conn: sqlite3.Connection | None = None
        self._initialized = False

        if auto_init:
            self.initialize()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def _get_connection(self) -> sqlite3.Connection:
        """Returns the active SQLite connection with required PRAGMAs configured."""
        if self._conn is not None:
            return self._conn

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # isolation_level=None enables autocommit mode, granting explicit
            # control over BEGIN IMMEDIATE / COMMIT / ROLLBACK transactions.
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=5.0,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row

            # Configure high-durability production PRAGMAs
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute(f"PRAGMA synchronous = {self.synchronous_mode};")

            self._conn = conn
            return self._conn
        except sqlite3.Error as e:
            raise RepositoryIOError(f"Failed to connect to SQLite database at {self.db_path}: {e}") from e

    def initialize(self) -> None:
        """Runs idempotent migrations to setup tables, indexes, and versioning."""
        conn = self._get_connection()

        try:
            # Check DB integrity first
            integrity_row = conn.execute("PRAGMA integrity_check;").fetchone()
            if integrity_row and integrity_row[0] != "ok":
                raise RepositoryCorruptError(f"Database integrity check failed: {integrity_row[0]}")

            # Migration metadata table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                """
            )

            # Sequentially apply migrations
            v1_row = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if not v1_row:
                self._apply_migration_v1(conn)

            v2_row = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = 2"
            ).fetchone()
            if not v2_row:
                self._apply_migration_v2(conn)

            v3_row = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = 3"
            ).fetchone()
            if not v3_row:
                self._apply_migration_v3(conn)

            self._initialized = True
            logger.info("Metadata repository initialized successfully.")
        except RepositoryError:
            raise
        except sqlite3.Error as e:
            raise RepositorySchemaError(f"Failed to initialize database schema: {e}") from e

    def _apply_migration_v1(self, conn: sqlite3.Connection) -> None:
        """Applies initial v1 schema migration inside an atomic transaction."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Sync State: current committed watermark per (scope, platform)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    committed_watermark_cursor TEXT,
                    incremental_head_watermark_cursor TEXT,
                    history_complete INTEGER NOT NULL DEFAULT 0,
                    backfill_checkpoint_cursor TEXT,
                    last_successful_sync_run_id TEXT,
                    head_anchor_content_id TEXT,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (scope_id, platform)
                );
                """
            )

            # 2. Sync Runs: session lifecycle, status, cursors, and metrics
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_runs (
                    sync_run_id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    collector_type TEXT NOT NULL DEFAULT 'collection',
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    previous_watermark_cursor TEXT,
                    candidate_watermark_cursor TEXT,
                    stop_reason TEXT,
                    stop_cursor TEXT,
                    has_more INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    execution_context_json TEXT NOT NULL DEFAULT '{}',
                    canonical_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

            # 3. Collection Observations: discrete append-only event records
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collection_observations (
                    observation_id TEXT PRIMARY KEY,
                    sync_run_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    position_in_page INTEGER NOT NULL,
                    global_rank_seen INTEGER NOT NULL,
                    page_request_cursor TEXT NOT NULL,
                    page_response_cursor TEXT NOT NULL,
                    is_first_observation INTEGER NOT NULL,
                    is_reappearance INTEGER NOT NULL,
                    raw_ref_json TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    FOREIGN KEY (sync_run_id) REFERENCES sync_runs(sync_run_id) ON DELETE RESTRICT
                );
                """
            )

            # 4. Collection Items: current canonical entity & membership state
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collection_items (
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    reappeared_at TEXT,
                    observed_count INTEGER NOT NULL DEFAULT 1,
                    last_seen_position INTEGER,
                    canonical_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_id, platform, platform_content_id)
                );
                """
            )

            # 5. Run Item Staging: run-scoped staging table for atomic commits
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_item_staging (
                    sync_run_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    canonical_item_json TEXT NOT NULL,
                    is_reappearance INTEGER NOT NULL DEFAULT 0,
                    staged_at TEXT NOT NULL,
                    PRIMARY KEY (sync_run_id, platform_content_id),
                    FOREIGN KEY (sync_run_id) REFERENCES sync_runs(sync_run_id) ON DELETE CASCADE
                );
                """
            )

            # Indexes for high-frequency queries
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_run ON collection_observations(sync_run_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_item ON collection_observations(scope_id, platform, platform_content_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_time ON collection_observations(observed_at);"
            )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_active ON collection_items(scope_id, platform, active);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_last_seen ON collection_items(scope_id, platform, last_seen_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_published ON collection_items(scope_id, platform, published_at);"
            )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_status ON sync_runs(status);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_started ON sync_runs(started_at);"
            )

            # Record migration and set PRAGMA user_version
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                (1, _utcnow_iso(), "v1 Initial Collector Metadata Schema"),
            )
            conn.execute("PRAGMA user_version = 1;")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RepositorySchemaError(f"Migration v1 failed: {e}") from e

    def _apply_migration_v2(self, conn: sqlite3.Connection) -> None:
        """Applies v2 schema migration adding incremental/history separation to sync_state."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Check existing columns in sync_state
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_state);").fetchall()}
            if "incremental_head_watermark_cursor" not in cols:
                conn.execute("ALTER TABLE sync_state ADD COLUMN incremental_head_watermark_cursor TEXT;")
            if "history_complete" not in cols:
                conn.execute("ALTER TABLE sync_state ADD COLUMN history_complete INTEGER NOT NULL DEFAULT 0;")
            if "backfill_checkpoint_cursor" not in cols:
                conn.execute("ALTER TABLE sync_state ADD COLUMN backfill_checkpoint_cursor TEXT;")

            # Backfill existing committed_watermark_cursor to incremental_head_watermark_cursor if null
            conn.execute(
                """
                UPDATE sync_state
                SET incremental_head_watermark_cursor = committed_watermark_cursor
                WHERE incremental_head_watermark_cursor IS NULL AND committed_watermark_cursor IS NOT NULL;
                """
            )

            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                (2, _utcnow_iso(), "v2 Separation of incremental head watermark and historical backfill coverage"),
            )
            conn.execute("PRAGMA user_version = 2;")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RepositorySchemaError(f"Migration v2 failed: {e}") from e

    def _apply_migration_v3(self, conn: sqlite3.Connection) -> None:
        """Applies v3 schema migration adding download_outbox table and indexes."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    scope_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_content_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    source_sync_run_id TEXT,
                    first_source_sync_run_id TEXT,
                    last_seen_sync_run_id TEXT
                );
                """
            )

            # Check existing columns in download_outbox and add any missing (for pre-existing v3 tables)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(download_outbox);").fetchall()}
            if "first_source_sync_run_id" not in cols:
                conn.execute("ALTER TABLE download_outbox ADD COLUMN first_source_sync_run_id TEXT;")
            if "last_seen_sync_run_id" not in cols:
                conn.execute("ALTER TABLE download_outbox ADD COLUMN last_seen_sync_run_id TEXT;")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_status_avail ON download_outbox(status, available_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_task ON download_outbox(task_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_item ON download_outbox(scope_id, platform, platform_content_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_run ON download_outbox(source_sync_run_id);"
            )

            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                (3, _utcnow_iso(), "v3 Transactional Download Outbox Queue"),
            )
            conn.execute("PRAGMA user_version = 3;")
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RepositorySchemaError(f"Migration v3 failed: {e}") from e

    def close(self) -> None:
        """Closes the underlying SQLite connection cleanly."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error while closing SQLite connection: {e}")
            finally:
                self._conn = None
                self._initialized = False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RepositoryNotInitializedError("Metadata repository is not initialized. Call initialize() first.")

    # =========================================================================
    # Sync State / Watermark Queries
    # =========================================================================

    def get_sync_state(
        self,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> SyncState | None:
        """Retrieves current committed watermark and sync state for a scope."""
        self._ensure_initialized()
        conn = self._get_connection()

        row = conn.execute(
            """
            SELECT scope_id, platform, committed_watermark_cursor,
                   incremental_head_watermark_cursor, history_complete,
                   backfill_checkpoint_cursor,
                   last_successful_sync_run_id, head_anchor_content_id,
                   updated_at, metadata_json
            FROM sync_state
            WHERE scope_id = ? AND platform = ?
            """,
            (scope_id, platform),
        ).fetchone()

        if not row:
            return None

        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except Exception:
                metadata = {}

        inc_head = row["incremental_head_watermark_cursor"]
        comm_wm = row["committed_watermark_cursor"]
        if inc_head is None and comm_wm is not None:
            inc_head = comm_wm
        elif comm_wm is None and inc_head is not None:
            comm_wm = inc_head

        return SyncState(
            scope_id=row["scope_id"],
            platform=row["platform"],
            committed_watermark_cursor=comm_wm,
            incremental_head_watermark_cursor=inc_head,
            history_complete=bool(row["history_complete"]),
            backfill_checkpoint_cursor=row["backfill_checkpoint_cursor"],
            last_successful_sync_run_id=row["last_successful_sync_run_id"],
            head_anchor_content_id=row["head_anchor_content_id"],
            updated_at=row["updated_at"],
            metadata=metadata,
        )

    # =========================================================================
    # Prior Collection State Lookup (for C07 Consumption)
    # =========================================================================

    def get_prior_state(
        self,
        platform_content_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> PriorCollectionState:
        """Retrieves persistent prior entity state to feed into C07 CanonicalTransformer.

        Returns PriorCollectionState(exists=False, active=False) if unseen.
        """
        self._ensure_initialized()
        conn = self._get_connection()

        row = conn.execute(
            """
            SELECT active, first_seen_at, last_seen_at, reappeared_at,
                   observed_count, last_seen_position
            FROM collection_items
            WHERE scope_id = ? AND platform = ? AND platform_content_id = ?
            """,
            (scope_id, platform, platform_content_id),
        ).fetchone()

        if not row:
            return PriorCollectionState(
                exists=False,
                active=False,
                first_seen_at=None,
                last_seen_at=None,
                reappeared_at=None,
                observed_count=0,
                last_seen_position=None,
            )

        return PriorCollectionState(
            exists=True,
            active=bool(row["active"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            reappeared_at=row["reappeared_at"],
            observed_count=int(row["observed_count"]),
            last_seen_position=row["last_seen_position"],
        )

    # =========================================================================
    # SyncRun Lifecycle APIs
    # =========================================================================

    def begin_sync_run(
        self,
        sync_run_id: str,
        mode: str = SyncRunMode.INCREMENTAL.value,
        page_size: int = 10,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
        execution_context: dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> SyncRunRecord:
        """Begins a new synchronization run, recording RUNNING status and current watermark.

        Idempotent on repeated calls for the identical RUNNING session.
        Rejects calls if the session ID already concluded (COMPLETED/FAILED/ABORTED).
        """
        self._ensure_initialized()
        if not sync_run_id or not isinstance(sync_run_id, str):
            raise RepositoryInvalidInputError("sync_run_id must be a non-empty string.")

        conn = self._get_connection()
        t_started = started_at or _utcnow_iso()
        exec_ctx = execution_context or {}
        if "page_size" not in exec_ctx:
            exec_ctx["page_size"] = page_size

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Check existing run
            existing = conn.execute(
                "SELECT * FROM sync_runs WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()

            if existing:
                status = existing["status"]
                if status == SyncRunStatus.RUNNING.value:
                    # Idempotent return if scope and platform match
                    if existing["scope_id"] == scope_id and existing["platform"] == platform:
                        conn.execute("COMMIT")
                        return self._row_to_sync_run_record(existing)
                    raise RepositoryConflictError(
                        f"SyncRun '{sync_run_id}' already running with different scope/platform."
                    )
                raise RepositoryConflictError(
                    f"SyncRun '{sync_run_id}' has already concluded with status '{status}'."
                )

            # Retrieve previous committed watermark for this scope
            state = conn.execute(
                "SELECT committed_watermark_cursor, incremental_head_watermark_cursor FROM sync_state WHERE scope_id = ? AND platform = ?",
                (scope_id, platform),
            ).fetchone()

            previous_wm: str | None = None
            if state:
                previous_wm = state["incremental_head_watermark_cursor"] or state["committed_watermark_cursor"]
            else:
                # Ensure a base row exists for sync_state
                conn.execute(
                    """
                    INSERT INTO sync_state (
                        scope_id, platform, committed_watermark_cursor,
                        incremental_head_watermark_cursor, history_complete,
                        backfill_checkpoint_cursor,
                        last_successful_sync_run_id, updated_at
                    ) VALUES (?, ?, NULL, NULL, 0, NULL, NULL, ?)
                    """,
                    (scope_id, platform, t_started),
                )

            canonical_dict = self._build_sync_run_canonical_dict(
                sync_run_id=sync_run_id,
                scope_id=scope_id,
                platform=platform,
                mode=mode,
                status=SyncRunStatus.RUNNING.value,
                started_at=t_started,
                finished_at=None,
                execution_context=exec_ctx,
                previous_watermark_cursor=previous_wm,
                candidate_watermark_cursor=None,
                stop_reason=None,
                stop_cursor=None,
                has_more=None,
                metrics={},
                error=None,
            )
            canonical_json = _deterministic_json(canonical_dict)

            conn.execute(
                """
                INSERT INTO sync_runs (
                    sync_run_id, scope_id, platform, collector_type,
                    mode, status, started_at, finished_at,
                    previous_watermark_cursor, candidate_watermark_cursor,
                    stop_reason, stop_cursor, has_more,
                    error_code, error_message,
                    metrics_json, execution_context_json, canonical_json
                ) VALUES (?, ?, ?, 'collection', ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, '{}', ?, ?)
                """,
                (
                    sync_run_id,
                    scope_id,
                    platform,
                    mode,
                    SyncRunStatus.RUNNING.value,
                    t_started,
                    previous_wm,
                    _deterministic_json(exec_ctx),
                    canonical_json,
                ),
            )

            conn.execute("COMMIT")
            return self.get_sync_run(sync_run_id)  # type: ignore[return-value]
        except Exception as e:
            conn.execute("ROLLBACK")
            if isinstance(e, RepositoryError):
                raise
            raise RepositoryIOError(f"Failed to begin sync run '{sync_run_id}': {e}") from e

    def get_sync_run(self, sync_run_id: str) -> SyncRunRecord | None:
        """Retrieves full sync run record by ID."""
        self._ensure_initialized()
        conn = self._get_connection()

        row = conn.execute(
            "SELECT * FROM sync_runs WHERE sync_run_id = ?",
            (sync_run_id,),
        ).fetchone()

        if not row:
            return None
        return self._row_to_sync_run_record(row)

    def _row_to_sync_run_record(self, row: sqlite3.Row) -> SyncRunRecord:
        metrics = {}
        if row["metrics_json"]:
            try:
                metrics = json.loads(row["metrics_json"])
            except Exception:
                metrics = {}

        exec_ctx = {}
        if row["execution_context_json"]:
            try:
                exec_ctx = json.loads(row["execution_context_json"])
            except Exception:
                exec_ctx = {}

        return SyncRunRecord(
            sync_run_id=row["sync_run_id"],
            scope_id=row["scope_id"],
            platform=row["platform"],
            mode=row["mode"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            previous_watermark_cursor=row["previous_watermark_cursor"],
            candidate_watermark_cursor=row["candidate_watermark_cursor"],
            stop_reason=row["stop_reason"],
            stop_cursor=row["stop_cursor"],
            has_more=row["has_more"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            metrics=metrics,
            execution_context=exec_ctx,
            canonical_json=row["canonical_json"],
        )

    # =========================================================================
    # Staging & Observation Persistence
    # =========================================================================

    def stage_item(
        self,
        sync_run_id: str,
        item: dict[str, Any],
        is_reappearance: bool = False,
        staged_at: str | None = None,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> None:
        """Stages a canonical item candidate for the active RUNNING session.

        Does NOT mutate collection_items until finalize_success().
        Idempotent on repeated calls for the same item.
        """
        self._ensure_initialized()
        conn = self._get_connection()

        # Validate run status
        run_row = conn.execute(
            "SELECT status FROM sync_runs WHERE sync_run_id = ?",
            (sync_run_id,),
        ).fetchone()

        if not run_row:
            raise RepositoryNotFoundError(f"SyncRun '{sync_run_id}' not found.")
        if run_row["status"] != SyncRunStatus.RUNNING.value:
            raise RepositoryStateConflictError(
                f"Cannot stage item: SyncRun '{sync_run_id}' is in status '{run_row['status']}', expected 'running'."
            )

        content_id = item.get("platform_content_id")
        content_type = item.get("content_type", "video")
        if not content_id:
            raise RepositoryInvalidInputError("Item lacks 'platform_content_id'.")

        t_staged = staged_at or _utcnow_iso()
        item_json = _deterministic_json(item)

        try:
            conn.execute(
                """
                INSERT INTO run_item_staging (
                    sync_run_id, scope_id, platform, platform_content_id,
                    content_type, canonical_item_json, is_reappearance, staged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sync_run_id, platform_content_id) DO UPDATE SET
                    canonical_item_json = excluded.canonical_item_json,
                    is_reappearance = excluded.is_reappearance,
                    staged_at = excluded.staged_at
                """,
                (
                    sync_run_id,
                    scope_id,
                    platform,
                    content_id,
                    content_type,
                    item_json,
                    1 if is_reappearance else 0,
                    t_staged,
                ),
            )
        except sqlite3.Error as e:
            raise RepositoryIOError(f"Failed to stage item '{content_id}': {e}") from e

    def append_observation(
        self,
        observation: dict[str, Any],
        scope_id: str = DEFAULT_SCOPE_ID,
    ) -> None:
        """Persists a discrete observation event to the append-only collection_observations log.

        Guarantees:
        - Append-only historical event record.
        - Idempotent on identical payload replay.
        - Raises RepositoryConflictError if observation_id exists with differing payload.
        """
        self._ensure_initialized()
        obs_id = observation.get("observation_id")
        sync_run_id = observation.get("sync_run_id")
        content_id = observation.get("platform_content_id")

        if not obs_id or not sync_run_id or not content_id:
            raise RepositoryInvalidInputError(
                "Observation lacks required identifiers ('observation_id', 'sync_run_id', 'platform_content_id')."
            )

        conn = self._get_connection()

        # Check existing observation
        existing = conn.execute(
            "SELECT canonical_json FROM collection_observations WHERE observation_id = ?",
            (obs_id,),
        ).fetchone()

        obs_json = _deterministic_json(observation)

        if existing:
            if existing["canonical_json"] == obs_json:
                # Perfectly idempotent
                return
            raise RepositoryConflictError(
                f"Observation '{obs_id}' already exists with a different payload. History cannot be rewritten."
            )

        # Ensure run exists
        run_row = conn.execute(
            "SELECT 1 FROM sync_runs WHERE sync_run_id = ?",
            (sync_run_id,),
        ).fetchone()
        if not run_row:
            raise RepositoryNotFoundError(f"Parent SyncRun '{sync_run_id}' not found for observation '{obs_id}'.")

        # Cursors must be exact decimal strings
        req_cursor = str(observation.get("page_request_cursor", "0"))
        resp_cursor = str(observation.get("page_response_cursor", "0"))
        raw_ref_dict = observation.get("raw_ref", {})
        raw_ref_json = _deterministic_json(raw_ref_dict)

        try:
            conn.execute(
                """
                INSERT INTO collection_observations (
                    observation_id, sync_run_id, scope_id, platform,
                    platform_content_id, observed_at, page_number,
                    position_in_page, global_rank_seen,
                    page_request_cursor, page_response_cursor,
                    is_first_observation, is_reappearance,
                    raw_ref_json, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs_id,
                    sync_run_id,
                    scope_id,
                    observation.get("platform", DEFAULT_PLATFORM),
                    content_id,
                    observation.get("observed_at", _utcnow_iso()),
                    int(observation.get("page_number", 1)),
                    int(observation.get("position_in_page", 0)),
                    int(observation.get("global_rank_seen", 1)),
                    req_cursor,
                    resp_cursor,
                    1 if observation.get("is_first_observation") else 0,
                    1 if observation.get("is_reappearance") else 0,
                    raw_ref_json,
                    obs_json,
                ),
            )
        except sqlite3.Error as e:
            raise RepositoryIOError(f"Failed to append observation '{obs_id}': {e}") from e

    def stage_transform_result(
        self,
        result: CanonicalTransformResult,
        sync_run_id: str,
        staged_at: str | None = None,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> None:
        """Convenience bridge for C07 CanonicalTransformResult: stages item and appends observation."""
        self.stage_item(
            sync_run_id=sync_run_id,
            item=result.item,
            is_reappearance=result.is_reappearance,
            staged_at=staged_at,
            scope_id=scope_id,
            platform=platform,
        )
        self.append_observation(result.observation, scope_id=scope_id)

    # =========================================================================
    # Atomic Finalization (Success & Failure Transactions)
    # =========================================================================

    def finalize_success(
        self,
        sync_run_id: str,
        candidate_watermark: str | None,
        stop_reason: str = "watermark_reached",
        metrics: dict[str, Any] | None = None,
        finished_at: str | None = None,
        items_to_mark_inactive: list[str] | None = None,
        expected_previous_watermark: str | None = None,
        expected_last_successful_run_id: str | None = None,
        stop_cursor: str | None = None,
        has_more: int | None = None,
        history_complete: bool | None = None,
        backfill_checkpoint_cursor: str | None = None,
        download_tasks: list[DownloadTask] | None = None,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> SyncRunRecord:
        """Short atomic transaction finalizing a successful sync run.

        Atomically performs:
        1. Validates run is currently RUNNING.
        2. Optimistic concurrency verification against committed sync state.
        3. Applies all staged item mutations into collection_items.
        4. Applies explicit inactive transitions requested by C05.
        5. Updates committed watermark and last_successful_sync_run_id in sync_state.
        6. Updates sync_runs status to COMPLETED with metrics and termination record.
        7. Clears staging for this run.

        Any failure causes an immediate ROLLBACK.
        """
        self._ensure_initialized()
        conn = self._get_connection()
        t_finished = finished_at or _utcnow_iso()
        m = metrics or {}

        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Fetch and validate sync_run
            run_row = conn.execute(
                "SELECT * FROM sync_runs WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()

            if not run_row:
                raise RepositoryNotFoundError(f"SyncRun '{sync_run_id}' not found.")

            status = run_row["status"]
            if status == SyncRunStatus.COMPLETED.value:
                # Idempotent check
                if run_row["candidate_watermark_cursor"] == candidate_watermark:
                    conn.execute("COMMIT")
                    return self._row_to_sync_run_record(run_row)
                raise RepositoryStateConflictError(
                    f"SyncRun '{sync_run_id}' was already completed with different watermark."
                )
            if status != SyncRunStatus.RUNNING.value:
                raise RepositoryStateConflictError(
                    f"Cannot finalize as success: SyncRun '{sync_run_id}' is in status '{status}'."
                )

            # 2. Optimistic concurrency check
            state_row = conn.execute(
                "SELECT committed_watermark_cursor, last_successful_sync_run_id FROM sync_state WHERE scope_id = ? AND platform = ?",
                (scope_id, platform),
            ).fetchone()

            if state_row:
                current_wm = state_row["committed_watermark_cursor"]
                current_last_run = state_row["last_successful_sync_run_id"]

                if expected_previous_watermark is not None and current_wm != expected_previous_watermark:
                    raise RepositoryStateConflictError(
                        f"Optimistic state conflict: expected watermark '{expected_previous_watermark}', but DB has '{current_wm}'."
                    )
                if expected_last_successful_run_id is not None and current_last_run != expected_last_successful_run_id:
                    raise RepositoryStateConflictError(
                        f"Optimistic state conflict: expected last successful run '{expected_last_successful_run_id}', but DB has '{current_last_run}'."
                    )

            # 3. Apply staged items
            staged_rows = conn.execute(
                "SELECT platform_content_id, content_type, canonical_item_json FROM run_item_staging WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchall()

            for s in staged_rows:
                cid = s["platform_content_id"]
                ctype = s["content_type"]
                item_dict = json.loads(s["canonical_item_json"])

                coll = item_dict.get("collection", {})
                active_val = 1 if coll.get("active", True) else 0
                first_seen = coll.get("first_seen_at", t_finished)
                last_seen = coll.get("last_seen_at", t_finished)
                published = item_dict.get("published_at", t_finished)
                reappeared = coll.get("reappeared_at")
                obs_count = int(coll.get("observed_count", 1))
                pos = coll.get("last_seen_position")

                conn.execute(
                    """
                    INSERT INTO collection_items (
                        scope_id, platform, platform_content_id, content_type,
                        active, first_seen_at, last_seen_at, published_at,
                        reappeared_at, observed_count, last_seen_position,
                        canonical_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (scope_id, platform, platform_content_id) DO UPDATE SET
                        content_type = excluded.content_type,
                        active = excluded.active,
                        last_seen_at = excluded.last_seen_at,
                        published_at = excluded.published_at,
                        reappeared_at = excluded.reappeared_at,
                        observed_count = excluded.observed_count,
                        last_seen_position = excluded.last_seen_position,
                        canonical_json = excluded.canonical_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        scope_id,
                        platform,
                        cid,
                        ctype,
                        active_val,
                        first_seen,
                        last_seen,
                        published,
                        reappeared,
                        obs_count,
                        pos,
                        s["canonical_item_json"],
                        t_finished,
                    ),
                )

            # 4. Apply explicit inactive mutations
            if items_to_mark_inactive:
                for inact_id in items_to_mark_inactive:
                    item_row = conn.execute(
                        "SELECT canonical_json FROM collection_items WHERE scope_id = ? AND platform = ? AND platform_content_id = ?",
                        (scope_id, platform, inact_id),
                    ).fetchone()
                    if item_row:
                        try:
                            item_obj = json.loads(item_row["canonical_json"])
                            if "collection" in item_obj:
                                item_obj["collection"]["active"] = False
                            updated_item_json = _deterministic_json(item_obj)
                        except Exception:
                            updated_item_json = item_row["canonical_json"]

                        conn.execute(
                            """
                            UPDATE collection_items
                            SET active = 0, canonical_json = ?, updated_at = ?
                            WHERE scope_id = ? AND platform = ? AND platform_content_id = ?
                            """,
                            (updated_item_json, t_finished, scope_id, platform, inact_id),
                        )

            # 5. Update committed watermark and last successful sync run in sync_state
            state_row = conn.execute(
                "SELECT history_complete, backfill_checkpoint_cursor FROM sync_state WHERE scope_id = ? AND platform = ?",
                (scope_id, platform),
            ).fetchone()
            curr_history_complete = bool(state_row["history_complete"]) if state_row else False
            curr_backfill_cursor = state_row["backfill_checkpoint_cursor"] if state_row else None

            if history_complete is not None:
                new_history_complete = bool(history_complete)
            else:
                new_history_complete = True if candidate_watermark is not None else curr_history_complete

            if new_history_complete:
                new_backfill_cursor = None
            else:
                new_backfill_cursor = backfill_checkpoint_cursor if backfill_checkpoint_cursor is not None else curr_backfill_cursor

            if candidate_watermark is not None:
                conn.execute(
                    """
                    UPDATE sync_state
                    SET committed_watermark_cursor = ?,
                        incremental_head_watermark_cursor = ?,
                        history_complete = ?,
                        backfill_checkpoint_cursor = ?,
                        last_successful_sync_run_id = ?,
                        updated_at = ?
                    WHERE scope_id = ? AND platform = ?
                    """,
                    (
                        candidate_watermark,
                        candidate_watermark,
                        1 if new_history_complete else 0,
                        new_backfill_cursor,
                        sync_run_id,
                        t_finished,
                        scope_id,
                        platform,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE sync_state
                    SET history_complete = ?,
                        backfill_checkpoint_cursor = ?,
                        last_successful_sync_run_id = ?,
                        updated_at = ?
                    WHERE scope_id = ? AND platform = ?
                    """,
                    (
                        1 if new_history_complete else 0,
                        new_backfill_cursor,
                        sync_run_id,
                        t_finished,
                        scope_id,
                        platform,
                    ),
                )

            # 6. Update sync_runs row
            exec_ctx = json.loads(run_row["execution_context_json"]) if run_row["execution_context_json"] else {}
            canonical_dict = self._build_sync_run_canonical_dict(
                sync_run_id=sync_run_id,
                scope_id=scope_id,
                platform=platform,
                mode=run_row["mode"],
                status=SyncRunStatus.COMPLETED.value,
                started_at=run_row["started_at"],
                finished_at=t_finished,
                execution_context=exec_ctx,
                previous_watermark_cursor=run_row["previous_watermark_cursor"],
                candidate_watermark_cursor=candidate_watermark,
                stop_reason=stop_reason,
                stop_cursor=stop_cursor,
                has_more=has_more,
                metrics=m,
                error=None,
            )
            canonical_json = _deterministic_json(canonical_dict)

            conn.execute(
                """
                UPDATE sync_runs
                SET status = ?,
                    finished_at = ?,
                    candidate_watermark_cursor = ?,
                    stop_reason = ?,
                    stop_cursor = ?,
                    has_more = ?,
                    metrics_json = ?,
                    canonical_json = ?
                WHERE sync_run_id = ?
                """,
                (
                    SyncRunStatus.COMPLETED.value,
                    t_finished,
                    candidate_watermark,
                    stop_reason,
                    stop_cursor,
                    has_more,
                    _deterministic_json(m),
                    canonical_json,
                    sync_run_id,
                ),
            )

            # 7. Persist transactional download outbox items (C09 Transactional Outbox)
            if download_tasks:
                for dt in download_tasks:
                    payload_json = dt.to_deterministic_json()
                    conn.execute(
                        """
                        INSERT INTO download_outbox (
                            outbox_id, task_id, scope_id, platform,
                            platform_content_id, content_type, payload_json,
                            status, created_at, available_at,
                            source_sync_run_id, first_source_sync_run_id, last_seen_sync_run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                        ON CONFLICT (task_id) DO UPDATE SET
                            payload_json = excluded.payload_json,
                            last_seen_sync_run_id = excluded.last_seen_sync_run_id
                        WHERE download_outbox.status = 'PENDING'
                        """,
                        (
                            f"ob_{dt.task_id}",
                            dt.task_id,
                            scope_id,
                            platform,
                            dt.platform_content_id,
                            dt.content_type,
                            payload_json,
                            t_finished,
                            t_finished,
                            sync_run_id,
                            sync_run_id,
                            sync_run_id,
                        ),
                    )

            # 8. Clear staging
            conn.execute(
                "DELETE FROM run_item_staging WHERE sync_run_id = ?",
                (sync_run_id,),
            )

            conn.execute("COMMIT")
            return self.get_sync_run(sync_run_id)  # type: ignore[return-value]
        except Exception as e:
            conn.execute("ROLLBACK")
            if isinstance(e, RepositoryError):
                raise
            raise RepositoryTransactionError(f"Transaction failed during finalize_success: {e}") from e

    def finalize_failure(
        self,
        sync_run_id: str,
        error_code: str,
        error_message: str,
        metrics: dict[str, Any] | None = None,
        finished_at: str | None = None,
        stop_reason: str = "error",
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> SyncRunRecord:
        """Finalizes a failed sync run without mutating committed watermark or items.

        Guarantees:
        - DOES NOT ADVANCE committed watermark.
        - DOES NOT MARK existing items inactive.
        - DOES NOT APPLY staged item mutations.
        - Clears staging for this run.
        - Marks sync_runs as FAILED with sanitized error summary.
        """
        self._ensure_initialized()
        conn = self._get_connection()
        t_finished = finished_at or _utcnow_iso()
        m = metrics or {}
        sanitized_msg = _sanitize_text(error_message)

        conn.execute("BEGIN IMMEDIATE")
        try:
            run_row = conn.execute(
                "SELECT * FROM sync_runs WHERE sync_run_id = ?",
                (sync_run_id,),
            ).fetchone()

            if not run_row:
                raise RepositoryNotFoundError(f"SyncRun '{sync_run_id}' not found.")

            status = run_row["status"]
            if status == SyncRunStatus.FAILED.value:
                # Idempotent return
                conn.execute("COMMIT")
                return self._row_to_sync_run_record(run_row)
            if status == SyncRunStatus.COMPLETED.value:
                raise RepositoryStateConflictError(
                    f"Cannot mark completed run '{sync_run_id}' as failed."
                )

            # Clear staging
            conn.execute(
                "DELETE FROM run_item_staging WHERE sync_run_id = ?",
                (sync_run_id,),
            )

            # Update run record to FAILED
            exec_ctx = json.loads(run_row["execution_context_json"]) if run_row["execution_context_json"] else {}
            err_dict = {
                "code": error_code,
                "message": sanitized_msg,
            }

            canonical_dict = self._build_sync_run_canonical_dict(
                sync_run_id=sync_run_id,
                scope_id=scope_id,
                platform=platform,
                mode=run_row["mode"],
                status=SyncRunStatus.FAILED.value,
                started_at=run_row["started_at"],
                finished_at=t_finished,
                execution_context=exec_ctx,
                previous_watermark_cursor=run_row["previous_watermark_cursor"],
                candidate_watermark_cursor=None,
                stop_reason=stop_reason,
                stop_cursor=None,
                has_more=None,
                metrics=m,
                error=err_dict,
            )
            canonical_json = _deterministic_json(canonical_dict)

            conn.execute(
                """
                UPDATE sync_runs
                SET status = ?,
                    finished_at = ?,
                    error_code = ?,
                    error_message = ?,
                    stop_reason = ?,
                    metrics_json = ?,
                    canonical_json = ?
                WHERE sync_run_id = ?
                """,
                (
                    SyncRunStatus.FAILED.value,
                    t_finished,
                    error_code,
                    sanitized_msg,
                    stop_reason,
                    _deterministic_json(m),
                    canonical_json,
                    sync_run_id,
                ),
            )

            conn.execute("COMMIT")
            return self.get_sync_run(sync_run_id)  # type: ignore[return-value]
        except Exception as e:
            conn.execute("ROLLBACK")
            if isinstance(e, RepositoryError):
                raise
            raise RepositoryTransactionError(f"Transaction failed during finalize_failure: {e}") from e

    # =========================================================================
    # Crash Recovery
    # =========================================================================

    def recover_stale_runs(
        self,
        scope_id: str | None = None,
        service_lock_acquired: bool = False,
    ) -> list[str]:
        """Discovers orphaned RUNNING runs from past crashed processes and marks them ABORTED.

        Safety Guard (Section B):
        - service_lock_acquired MUST be True. If False, raises RepositoryStateConflictError.
          Repository alone does NOT have the authority to abort live runs without holding ServiceLock.
        - If scope_id is provided, only recovers runs belonging to that specific scope.
        - Ensures that orphaned staging is cleaned and committed watermark remains untouched.
        """
        self._ensure_initialized()
        if not service_lock_acquired:
            raise RepositoryStateConflictError(
                "Cannot execute recover_stale_runs() without holding the Collector ServiceLock. "
                "Another active collector process may legitimately own the running session."
            )

        conn = self._get_connection()

        recovered_ids: list[str] = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            if scope_id is not None:
                stale_rows = conn.execute(
                    "SELECT sync_run_id, started_at, mode, scope_id, platform, previous_watermark_cursor, execution_context_json FROM sync_runs WHERE status = ? AND scope_id = ?",
                    (SyncRunStatus.RUNNING.value, scope_id),
                ).fetchall()
            else:
                stale_rows = conn.execute(
                    "SELECT sync_run_id, started_at, mode, scope_id, platform, previous_watermark_cursor, execution_context_json FROM sync_runs WHERE status = ?",
                    (SyncRunStatus.RUNNING.value,),
                ).fetchall()

            now_iso = _utcnow_iso()
            for row in stale_rows:
                run_id = row["sync_run_id"]
                recovered_ids.append(run_id)

                # Clear its staging
                conn.execute(
                    "DELETE FROM run_item_staging WHERE sync_run_id = ?",
                    (run_id,),
                )

                exec_ctx = json.loads(row["execution_context_json"]) if row["execution_context_json"] else {}
                err_dict = {
                    "code": "PROCESS_TERMINATED_ABNORMALLY",
                    "message": "Run aborted during crash recovery.",
                }
                canonical_dict = self._build_sync_run_canonical_dict(
                    sync_run_id=run_id,
                    scope_id=row["scope_id"],
                    platform=row["platform"],
                    mode=row["mode"],
                    status=SyncRunStatus.ABORTED.value,
                    started_at=row["started_at"],
                    finished_at=now_iso,
                    execution_context=exec_ctx,
                    previous_watermark_cursor=row["previous_watermark_cursor"],
                    candidate_watermark_cursor=None,
                    stop_reason="manual_abort",
                    stop_cursor=None,
                    has_more=None,
                    metrics={},
                    error=err_dict,
                )
                canonical_json = _deterministic_json(canonical_dict)

                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?,
                        finished_at = ?,
                        stop_reason = 'manual_abort',
                        error_code = 'PROCESS_TERMINATED_ABNORMALLY',
                        error_message = 'Run aborted during crash recovery.',
                        canonical_json = ?
                    WHERE sync_run_id = ?
                    """,
                    (SyncRunStatus.ABORTED.value, now_iso, canonical_json, run_id),
                )

            conn.execute("COMMIT")
            if recovered_ids:
                logger.warning(f"Recovered {len(recovered_ids)} crashed runs: {recovered_ids}")
            return recovered_ids
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RepositoryTransactionError(f"Crash recovery failed: {e}") from e

    # =========================================================================
    # Query & Retrieval APIs
    # =========================================================================

    def get_item(
        self,
        platform_content_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> dict[str, Any] | None:
        """Retrieves full canonical item dictionary."""
        self._ensure_initialized()
        conn = self._get_connection()

        row = conn.execute(
            "SELECT canonical_json FROM collection_items WHERE scope_id = ? AND platform = ? AND platform_content_id = ?",
            (scope_id, platform, platform_content_id),
        ).fetchone()

        if not row:
            return None
        return json.loads(row["canonical_json"])

    def get_item_record(
        self,
        platform_content_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> CollectionItemRecord | None:
        """Retrieves strongly-typed CollectionItemRecord."""
        self._ensure_initialized()
        conn = self._get_connection()

        row = conn.execute(
            "SELECT * FROM collection_items WHERE scope_id = ? AND platform = ? AND platform_content_id = ?",
            (scope_id, platform, platform_content_id),
        ).fetchone()

        if not row:
            return None

        return CollectionItemRecord(
            scope_id=row["scope_id"],
            platform=row["platform"],
            platform_content_id=row["platform_content_id"],
            content_type=row["content_type"],
            active=bool(row["active"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            published_at=row["published_at"],
            reappeared_at=row["reappeared_at"],
            observed_count=int(row["observed_count"]),
            last_seen_position=row["last_seen_position"],
            canonical_json=row["canonical_json"],
            updated_at=row["updated_at"],
        )

    def list_items(
        self,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Lists canonical items paginated, optionally filtering active items."""
        self._ensure_initialized()
        conn = self._get_connection()

        query = "SELECT canonical_json FROM collection_items WHERE scope_id = ? AND platform = ?"
        params: list[Any] = [scope_id, platform]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [json.loads(r["canonical_json"]) for r in rows]

    def count_items(
        self,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
        active_only: bool = False,
    ) -> int:
        """Returns total count of stored items for scope/platform."""
        self._ensure_initialized()
        conn = self._get_connection()

        query = "SELECT COUNT(*) FROM collection_items WHERE scope_id = ? AND platform = ?"
        params: list[Any] = [scope_id, platform]
        if active_only:
            query += " AND active = 1"

        row = conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def get_observations_for_run(self, sync_run_id: str) -> list[dict[str, Any]]:
        """Returns all observation dictionaries for a given sync run ordered by rank."""
        self._ensure_initialized()
        conn = self._get_connection()

        rows = conn.execute(
            "SELECT canonical_json FROM collection_observations WHERE sync_run_id = ? ORDER BY global_rank_seen ASC",
            (sync_run_id,),
        ).fetchall()

        return [json.loads(r["canonical_json"]) for r in rows]

    def get_staged_items_for_run(self, sync_run_id: str) -> list[dict[str, Any]]:
        """Returns all staged item dictionaries for a given sync run."""
        self._ensure_initialized()
        conn = self._get_connection()

        rows = conn.execute(
            "SELECT canonical_item_json FROM run_item_staging WHERE sync_run_id = ?",
            (sync_run_id,),
        ).fetchall()

        return [json.loads(r["canonical_item_json"]) for r in rows]

    # =========================================================================
    # Skeleton Protocol Compatibility Methods
    # =========================================================================

    def get_watermark(self, platform: str, user_id: str) -> int:
        """Compatibility method for C01 MetadataRepository protocol."""
        state = self.get_sync_state(scope_id=f"{platform}:{user_id}", platform=platform)
        if state and state.committed_watermark_cursor:
            try:
                return int(state.committed_watermark_cursor)
            except ValueError:
                return 0
        return 0

    def update_watermark(self, watermark: Watermark) -> None:
        """Compatibility method for C01 MetadataRepository protocol."""
        self._ensure_initialized()
        conn = self._get_connection()
        scope_id = f"{watermark.platform}:{watermark.user_id}"
        t_now = _utcnow_iso()

        conn.execute(
            """
            INSERT INTO sync_state (
                scope_id, platform, committed_watermark_cursor,
                last_successful_sync_run_id, updated_at
            ) VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT (scope_id, platform) DO UPDATE SET
                committed_watermark_cursor = excluded.committed_watermark_cursor,
                updated_at = excluded.updated_at
            """,
            (scope_id, watermark.platform, str(watermark.watermark_cursor), t_now),
        )

    def record_sync_run(self, sync_run_data: dict[str, Any]) -> None:
        """Compatibility method for C01 MetadataRepository protocol."""
        run_id = sync_run_data.get("run_id", f"run_{_utcnow_iso()}")
        status_raw = sync_run_data.get("status", "completed").lower()
        status_val = "completed" if "success" in status_raw or "complete" in status_raw else "failed"

        conn = self._get_connection()
        started = sync_run_data.get("started_at", _utcnow_iso())
        finished = sync_run_data.get("finished_at", _utcnow_iso())
        mode_val = sync_run_data.get("mode", "incremental")

        canonical_dict = self._build_sync_run_canonical_dict(
            sync_run_id=run_id,
            scope_id=DEFAULT_SCOPE_ID,
            platform=DEFAULT_PLATFORM,
            mode=mode_val,
            status=status_val,
            started_at=started,
            finished_at=finished,
            execution_context={"page_size": 10},
            previous_watermark_cursor=None,
            candidate_watermark_cursor=None,
            stop_reason="watermark_reached" if status_val == "completed" else "error",
            stop_cursor=None,
            has_more=None,
            metrics={},
            error=None,
        )

        conn.execute(
            """
            INSERT INTO sync_runs (
                sync_run_id, scope_id, platform, collector_type,
                mode, status, started_at, finished_at,
                canonical_json
            ) VALUES (?, ?, ?, 'collection', ?, ?, ?, ?, ?)
            ON CONFLICT (sync_run_id) DO UPDATE SET
                status = excluded.status,
                finished_at = excluded.finished_at,
                canonical_json = excluded.canonical_json
            """,
            (
                run_id,
                DEFAULT_SCOPE_ID,
                DEFAULT_PLATFORM,
                mode_val,
                status_val,
                started,
                finished,
                _deterministic_json(canonical_dict),
            ),
        )

    def item_exists(
        self,
        platform: str,
        platform_content_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
    ) -> bool:
        """Compatibility method for C01 MetadataRepository protocol."""
        self._ensure_initialized()
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM collection_items WHERE scope_id = ? AND platform = ? AND platform_content_id = ?",
            (scope_id, platform, platform_content_id),
        ).fetchone()
        return bool(row)

    def save_item(
        self,
        item_data: dict[str, Any],
        scope_id: str = DEFAULT_SCOPE_ID,
    ) -> None:
        """Direct save method for item compatibility."""
        self._ensure_initialized()
        cid = item_data.get("platform_content_id")
        if not cid:
            raise RepositoryInvalidInputError("Missing platform_content_id")

        platform = item_data.get("platform", DEFAULT_PLATFORM)
        ctype = item_data.get("content_type", "video")
        coll = item_data.get("collection", {})
        active = 1 if coll.get("active", True) else 0
        first_seen = coll.get("first_seen_at", _utcnow_iso())
        last_seen = coll.get("last_seen_at", _utcnow_iso())
        published = item_data.get("published_at", _utcnow_iso())
        reappeared = coll.get("reappeared_at")
        obs_count = int(coll.get("observed_count", 1))
        pos = coll.get("last_seen_position")
        item_json = _deterministic_json(item_data)
        t_now = _utcnow_iso()

        conn = self._get_connection()
        conn.execute(
            """
            INSERT INTO collection_items (
                scope_id, platform, platform_content_id, content_type,
                active, first_seen_at, last_seen_at, published_at,
                reappeared_at, observed_count, last_seen_position,
                canonical_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scope_id, platform, platform_content_id) DO UPDATE SET
                content_type = excluded.content_type,
                active = excluded.active,
                last_seen_at = excluded.last_seen_at,
                published_at = excluded.published_at,
                reappeared_at = excluded.reappeared_at,
                observed_count = excluded.observed_count,
                last_seen_position = excluded.last_seen_position,
                canonical_json = excluded.canonical_json,
                updated_at = excluded.updated_at
            """,
            (
                scope_id,
                platform,
                cid,
                ctype,
                active,
                first_seen,
                last_seen,
                published,
                reappeared,
                obs_count,
                pos,
                item_json,
                t_now,
            ),
        )

    def save_observation(
        self,
        observation_data: dict[str, Any],
        scope_id: str = DEFAULT_SCOPE_ID,
    ) -> None:
        """Direct save method for observation compatibility."""
        self.append_observation(observation_data, scope_id=scope_id)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _build_sync_run_canonical_dict(
        self,
        sync_run_id: str,
        scope_id: str,
        platform: str,
        mode: str,
        status: str,
        started_at: str,
        finished_at: str | None,
        execution_context: dict[str, Any],
        previous_watermark_cursor: str | None,
        candidate_watermark_cursor: str | None,
        stop_reason: str | None,
        stop_cursor: str | None,
        has_more: int | None,
        metrics: dict[str, Any],
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Constructs canonical sync-run-v1 schema representation."""
        # Normalized metrics with defaults
        m = {
            "pages_fetched": int(metrics.get("pages_fetched", 0)),
            "items_seen": int(metrics.get("items_seen", 0)),
            "new_items": int(metrics.get("new_items", 0)),
            "reappeared_items": int(metrics.get("reappeared_items", 0)),
            "known_items": int(metrics.get("known_items", 0)),
            "errors_count": int(metrics.get("errors_count", 0)),
        }

        # Determine valid enum stop_reason
        normalized_stop = stop_reason
        if not normalized_stop:
            if status == SyncRunStatus.COMPLETED.value:
                normalized_stop = "watermark_reached"
            elif status == SyncRunStatus.FAILED.value:
                normalized_stop = "error"
            elif status == SyncRunStatus.ABORTED.value:
                normalized_stop = "manual_abort"
            else:
                normalized_stop = "max_pages_reached"

        valid_reasons = {
            "watermark_reached",
            "terminal_has_more_zero",
            "max_pages_reached",
            "error",
            "manual_abort",
        }
        if normalized_stop not in valid_reasons:
            normalized_stop = "error" if status == SyncRunStatus.FAILED.value else "watermark_reached"

        term: dict[str, Any] = {
            "stop_reason": normalized_stop,
            "stop_cursor": stop_cursor,
            "has_more": has_more,
        }

        wm: dict[str, Any] = {
            "previous_watermark_cursor": previous_watermark_cursor,
            "new_watermark_cursor": candidate_watermark_cursor,
        }

        exec_ctx: dict[str, Any] = {
            "page_size": execution_context.get("page_size", 10),
            "agent_id": execution_context.get("agent_id", "agent:strong-model"),
            "environment": execution_context.get("environment", "python-3.12"),
        }

        return {
            "schema_version": "sync-run-v1",
            "sync_run_id": sync_run_id,
            "platform": platform,
            "collector_type": "collection",
            "mode": mode,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "execution_context": exec_ctx,
            "watermark": wm,
            "termination": term,
            "metrics": m,
            "error": error,
        }

    # =========================================================================
    # Staging & Collection Item Queries
    # =========================================================================

    def get_staged_items(self, sync_run_id: str) -> list[StagedItemRecord]:
        """Retrieves all staged item records for an active sync run."""
        self._ensure_initialized()
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT sync_run_id, scope_id, platform, platform_content_id,
                   content_type, canonical_item_json, is_reappearance, staged_at
            FROM run_item_staging
            WHERE sync_run_id = ?
            ORDER BY staged_at ASC, platform_content_id ASC
            """,
            (sync_run_id,),
        ).fetchall()
        return [
            StagedItemRecord(
                sync_run_id=r["sync_run_id"],
                scope_id=r["scope_id"],
                platform=r["platform"],
                platform_content_id=r["platform_content_id"],
                content_type=r["content_type"],
                canonical_item_json=r["canonical_item_json"],
                is_reappearance=bool(r["is_reappearance"]),
                staged_at=r["staged_at"],
            )
            for r in rows
        ]

    def _row_to_collection_item_record(self, row: sqlite3.Row) -> CollectionItemRecord:
        """Helper to map a sqlite3.Row to CollectionItemRecord."""
        return CollectionItemRecord(
            scope_id=row["scope_id"],
            platform=row["platform"],
            platform_content_id=row["platform_content_id"],
            content_type=row["content_type"],
            active=bool(row["active"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            published_at=row["published_at"],
            reappeared_at=row["reappeared_at"],
            observed_count=int(row["observed_count"]),
            last_seen_position=row["last_seen_position"],
            canonical_json=row["canonical_json"],
            updated_at=row["updated_at"],
        )

    def get_collection_item(
        self,
        platform_content_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> CollectionItemRecord | None:
        """Retrieves a single committed collection item by content ID."""
        self._ensure_initialized()
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT scope_id, platform, platform_content_id, content_type,
                   active, first_seen_at, last_seen_at, published_at,
                   reappeared_at, observed_count, last_seen_position,
                   canonical_json, updated_at
            FROM collection_items
            WHERE scope_id = ? AND platform = ? AND platform_content_id = ?
            """,
            (scope_id, platform, platform_content_id),
        ).fetchone()
        if not row:
            return None
        return self._row_to_collection_item_record(row)

    def get_committed_items_for_run(
        self,
        sync_run_id: str,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> list[CollectionItemRecord]:
        """Retrieves all committed collection items observed during the specified sync run."""
        self._ensure_initialized()
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT ci.scope_id, ci.platform, ci.platform_content_id, ci.content_type,
                   ci.active, ci.first_seen_at, ci.last_seen_at, ci.published_at,
                   ci.reappeared_at, ci.observed_count, ci.last_seen_position,
                   ci.canonical_json, ci.updated_at
            FROM collection_items ci
            WHERE ci.scope_id = ? AND ci.platform = ? AND ci.platform_content_id IN (
                SELECT DISTINCT platform_content_id FROM collection_observations WHERE sync_run_id = ?
            )
            ORDER BY ci.last_seen_at DESC, ci.platform_content_id ASC
            """,
            (scope_id, platform, sync_run_id),
        ).fetchall()
        return [self._row_to_collection_item_record(r) for r in rows]

    # =========================================================================
    # Download Outbox Operations (DY-C09)
    # =========================================================================

    def persist_outbox_tasks(
        self,
        tasks: list[DownloadTask],
        sync_run_id: str | None = None,
        scope_id: str = DEFAULT_SCOPE_ID,
        platform: str = DEFAULT_PLATFORM,
    ) -> int:
        """Durable insertion of download outbox tasks outside finalize_success.

        Used for manual repair or independent backfill enqueue operations.
        """
        if not tasks:
            return 0
        self._ensure_initialized()
        conn = self._get_connection()
        now_iso = _utcnow_iso()
        count = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for dt in tasks:
                run_id = sync_run_id or dt.collection_sync_run_id or "manual"
                payload_json = dt.to_deterministic_json()
                conn.execute(
                    """
                    INSERT INTO download_outbox (
                        outbox_id, task_id, scope_id, platform,
                        platform_content_id, content_type, payload_json,
                        status, created_at, available_at,
                        source_sync_run_id, first_source_sync_run_id, last_seen_sync_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                    ON CONFLICT (task_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        last_seen_sync_run_id = excluded.last_seen_sync_run_id
                    WHERE download_outbox.status = 'PENDING'
                    """,
                    (
                        f"ob_{dt.task_id}",
                        dt.task_id,
                        dt.scope_id or scope_id,
                        dt.platform or platform,
                        dt.platform_content_id,
                        dt.content_type,
                        payload_json,
                        dt.created_at or now_iso,
                        dt.created_at or now_iso,
                        run_id,
                        run_id,
                        run_id,
                    ),
                )
                count += 1
            conn.execute("COMMIT")
            return count
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RepositoryIOError(f"Failed to persist outbox tasks: {e}") from e

    def get_pending_outbox_tasks(
        self,
        limit: int = 50,
        scope_id: str | None = None,
        platform: str | None = None,
    ) -> list[OutboxRecord]:
        """Polls available pending download tasks from the outbox table."""
        self._ensure_initialized()
        conn = self._get_connection()
        now_iso = _utcnow_iso()

        query = """
            SELECT outbox_id, task_id, scope_id, platform, platform_content_id,
                   content_type, payload_json, status, created_at, available_at,
                   dispatched_at, attempt_count, last_error, source_sync_run_id,
                   first_source_sync_run_id, last_seen_sync_run_id
            FROM download_outbox
            WHERE status = 'PENDING' AND available_at <= ?
        """
        params: list[Any] = [now_iso]

        if scope_id:
            query += " AND scope_id = ?"
            params.append(scope_id)
        if platform:
            query += " AND platform = ?"
            params.append(platform)

        query += " ORDER BY available_at ASC, created_at ASC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, tuple(params)).fetchall()
        return [
            OutboxRecord(
                outbox_id=r["outbox_id"],
                task_id=r["task_id"],
                scope_id=r["scope_id"],
                platform=r["platform"],
                platform_content_id=r["platform_content_id"],
                content_type=r["content_type"],
                payload_json=r["payload_json"],
                status=r["status"],
                created_at=r["created_at"],
                available_at=r["available_at"],
                source_sync_run_id=r["source_sync_run_id"],
                first_source_sync_run_id=r["first_source_sync_run_id"] or r["source_sync_run_id"],
                last_seen_sync_run_id=r["last_seen_sync_run_id"],
                dispatched_at=r["dispatched_at"],
                attempt_count=r["attempt_count"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def get_outbox_record(self, task_id: str) -> OutboxRecord | None:
        """Retrieves a single outbox record by its deterministic task_id."""
        self._ensure_initialized()
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT outbox_id, task_id, scope_id, platform, platform_content_id,
                   content_type, payload_json, status, created_at, available_at,
                   dispatched_at, attempt_count, last_error, source_sync_run_id,
                   first_source_sync_run_id, last_seen_sync_run_id
            FROM download_outbox
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return OutboxRecord(
            outbox_id=row["outbox_id"],
            task_id=row["task_id"],
            scope_id=row["scope_id"],
            platform=row["platform"],
            platform_content_id=row["platform_content_id"],
            content_type=row["content_type"],
            payload_json=row["payload_json"],
            status=row["status"],
            created_at=row["created_at"],
            available_at=row["available_at"],
            source_sync_run_id=row["source_sync_run_id"],
            first_source_sync_run_id=row["first_source_sync_run_id"] or row["source_sync_run_id"],
            last_seen_sync_run_id=row["last_seen_sync_run_id"],
            dispatched_at=row["dispatched_at"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    def mark_outbox_dispatched(self, task_ids: list[str], dispatched_at: str | None = None) -> int:
        """Transitions outbox tasks to DISPATCHED state."""
        if not task_ids:
            return 0
        self._ensure_initialized()
        conn = self._get_connection()
        t_disp = dispatched_at or _utcnow_iso()
        placeholders = ",".join("?" for _ in task_ids)
        try:
            cur = conn.execute(
                f"""
                UPDATE download_outbox
                SET status = 'DISPATCHED', dispatched_at = ?
                WHERE task_id IN ({placeholders}) AND status = 'PENDING'
                """,
                (t_disp, *task_ids),
            )
            conn.commit()
            return cur.rowcount
        except sqlite3.Error as e:
            raise RepositoryIOError(f"Failed to mark outbox tasks dispatched: {e}") from e

    def mark_outbox_failed(
        self,
        task_id: str,
        error: str,
        retry_delay_seconds: int = 60,
        max_attempts: int = 3,
    ) -> None:
        """Records delivery failure, increments attempt count, and handles retry or terminal FAILED."""
        self._ensure_initialized()
        conn = self._get_connection()
        sanitized_err = _sanitize_text(error)

        row = conn.execute(
            "SELECT attempt_count FROM download_outbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            raise RepositoryNotFoundError(f"Outbox task '{task_id}' not found.")

        new_attempts = row["attempt_count"] + 1
        if new_attempts >= max_attempts:
            new_status = "FAILED"
            new_avail = _utcnow_iso()
        else:
            new_status = "PENDING"
            delay = retry_delay_seconds * (2 ** (new_attempts - 1))
            next_dt = datetime.now(timezone.utc) + timedelta(seconds=delay)
            new_avail = next_dt.isoformat()

        try:
            conn.execute(
                """
                UPDATE download_outbox
                SET attempt_count = ?,
                    last_error = ?,
                    status = ?,
                    available_at = ?
                WHERE task_id = ?
                """,
                (new_attempts, sanitized_err, new_status, new_avail, task_id),
            )
            conn.commit()
        except sqlite3.Error as e:
            raise RepositoryIOError(f"Failed to record outbox failure for '{task_id}': {e}") from e

    # Narrow adapter aliases for C09 / D10 outbox consumption protocol
    poll_pending_outbox = get_pending_outbox_tasks
    mark_delivery_failed = mark_outbox_failed



CollectorRepository = SqliteMetadataRepository

__all__ = [
    "SqliteMetadataRepository",
    "CollectorRepository",
    "DEFAULT_SCOPE_ID",
    "DEFAULT_PLATFORM",
]
