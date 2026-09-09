"""M5-01 Canonical Knowledge Store & Idempotent Ingestion (SQLite).

Implements the sealed `knowledge-store-v1` schema:

  - store_meta         schema/policy metadata + deterministic store revision
  - ingested_assets    which knowledge_units.json each asset was built from
  - knowledge_units    unit projection + verbatim canonical payload
  - evidence_refs      per-unit evidence refs (canonical ordinal preserved)
  - entities           per-unit entity mentions (canonical ordinal preserved)
  - topics             per-unit topics (canonical ordinal preserved)

Invariants (M5-00 Decisions 1, 5-9):

  - M4 knowledge_units.json is the canonical Source of Truth; the store is a
    derived, rebuildable projection.
  - One asset ingest = one `BEGIN IMMEDIATE ... COMMIT` transaction. Any
    failure rolls back; a half-ingested asset never exists.
  - Same canonical_id + same source fingerprint => NO-OP / cache hit.
    Changed fingerprint => deterministic replace inside one transaction.
  - projection columns are always derived from canonical_payload_json; the
    store never keeps two competing truths.
  - remove_asset deletes only derived rows (FK cascade); never source files.
  - Schema is versioned via PRAGMA user_version=1; incompatible schema fails
    explicitly (create/validate/rebuild only, no silent migration).

M5-01 does NOT implement FTS5, search, RetrievalQuery/Hit/Result, ranking or
filters; those belong to M5-02+.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    KNOWLEDGE_SCHEMA_VERSION,
    CanonicalKnowledgeUnit,
    CanonicalKnowledgeUnitsDocument,
)
from ..storage import utc_now

STORE_SCHEMA_VERSION = "knowledge-store-v1"
STORE_SCHEMA_POLICY_VERSION = "knowledge-store-policy-v1"
STORE_USER_VERSION = 1
DEFAULT_STORE_PATH = Path("data/knowledge/knowledge_store.sqlite3")

# Canonical final artifact filename used by rebuild discovery.
FINAL_ARTIFACT_FILENAME = "knowledge_units.json"

_STORE_META_SCHEMA_VERSION_KEY = "schema_version"
_STORE_META_POLICY_VERSION_KEY = "schema_policy_version"
_STORE_META_CREATED_AT_KEY = "created_at"
_STORE_META_REBUILT_AT_KEY = "rebuilt_at"
_STORE_META_REVISION_KEY = "store_revision"


class StoreError(Exception):
    """Base class for knowledge store errors."""


class StoreSchemaError(StoreError):
    """Raised when the on-disk schema is missing or incompatible."""


class StoreIngestError(StoreError):
    """Raised when an ingest cannot be performed (invalid document, etc.)."""


class StoreValidationError(StoreError):
    """Raised when store validation detects an invariant violation."""


@dataclass(frozen=True)
class IngestResult:
    """Result of an asset ingestion.

    ``status`` is one of:
      - ``inserted``: asset newly persisted.
      - ``replaced``: existing asset replaced with a new artifact fingerprint.
      - ``unchanged``: idempotency hit; no rows touched (cache hit).
    """

    status: str
    canonical_id: str
    source_artifact_fingerprint: str
    unit_count: int
    ingested_at: str
    replaced_from_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class StoreValidationResult:
    """Structured result of ``validate_store``."""

    valid: bool
    schema_version: str
    schema_policy_version: str
    store_revision: str
    asset_count: int
    unit_count: int
    checks: dict[str, Any]
    violations: list[str]


# ----------------------------------------------------------------------
# Canonical serialization helpers (project-wide convention)
# ----------------------------------------------------------------------

def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_source_artifact_fingerprint(document: dict[str, Any]) -> str:
    """Content fingerprint of a canonical knowledge_units.json document.

    Uses the project SHA-256 canonical-JSON convention (same rule as M4
    fingerprint helpers); never fabricated.
    """
    if not isinstance(document, dict):
        raise StoreIngestError("knowledge document must be a dict")
    return _sha256_json(document)


def _payload_json(unit: CanonicalKnowledgeUnit) -> str:
    """Deterministic canonical-JSON serialization of a unit payload."""
    return json.dumps(
        unit.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


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
        "store_meta",
        "ingested_assets",
        "knowledge_units",
        "evidence_refs",
        "entities",
        "topics",
    }
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    present = {row["name"] for row in rows}
    return required.issubset(present)


# ----------------------------------------------------------------------
# Schema creation / open / validation
# ----------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_assets (
    canonical_id TEXT PRIMARY KEY,
    knowledge_schema_version TEXT NOT NULL,
    source_artifact_path TEXT NOT NULL,
    source_artifact_fingerprint TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    unit_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_units (
    unit_rowid INTEGER PRIMARY KEY,
    knowledge_unit_id TEXT NOT NULL UNIQUE,
    canonical_id TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    extraction_confidence REAL NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    FOREIGN KEY (canonical_id) REFERENCES ingested_assets(canonical_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_knowledge_units_canonical
    ON knowledge_units(canonical_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_units_type
    ON knowledge_units(unit_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_units_verification
    ON knowledge_units(verification_status);

CREATE TABLE IF NOT EXISTS evidence_refs (
    ref_rowid INTEGER PRIMARY KEY,
    knowledge_unit_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    source_excerpt TEXT NOT NULL,
    temporal_start REAL,
    temporal_end REAL,
    temporal_duration REAL,
    sequence_index INTEGER,
    UNIQUE(knowledge_unit_id, ordinal),
    FOREIGN KEY (knowledge_unit_id) REFERENCES knowledge_units(knowledge_unit_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS entities (
    entity_rowid INTEGER PRIMARY KEY,
    knowledge_unit_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    entity_name TEXT NOT NULL,
    category TEXT NOT NULL,
    UNIQUE(knowledge_unit_id, ordinal),
    FOREIGN KEY (knowledge_unit_id) REFERENCES knowledge_units(knowledge_unit_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS topics (
    topic_rowid INTEGER PRIMARY KEY,
    knowledge_unit_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    topic TEXT NOT NULL,
    UNIQUE(knowledge_unit_id, ordinal),
    FOREIGN KEY (knowledge_unit_id) REFERENCES knowledge_units(knowledge_unit_id) ON DELETE CASCADE
);
"""


def _any_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return row is not None


def create_store(db_path: Path) -> None:
    """Create the knowledge-store-v1 schema on an empty/non-existent DB file.

    Raises StoreSchemaError if the target file already contains an
    incompatible schema or unknown tables.
    """
    db_path = Path(db_path)
    exists = db_path.exists()
    conn = _connect(db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables_present = _any_tables_exist(conn)
        if exists and user_version != 0 and user_version != STORE_USER_VERSION:
            raise StoreSchemaError(
                f"incompatible schema: PRAGMA user_version={user_version}, "
                f"expected {STORE_USER_VERSION} (knowledge-store-v1)"
            )
        if user_version == 0 and exists and tables_present:
            raise StoreSchemaError(
                "file already contains tables but no knowledge-store-v1 "
                "user_version; refusing to guess"
            )
        conn.executescript(_SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {STORE_USER_VERSION}")
        now = utc_now()
        conn.executemany(
            "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
            [
                (_STORE_META_SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION),
                (_STORE_META_POLICY_VERSION_KEY, STORE_SCHEMA_POLICY_VERSION),
                (_STORE_META_CREATED_AT_KEY, now),
                (_STORE_META_REBUILT_AT_KEY, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def open_store(db_path: Path) -> sqlite3.Connection:
    """Open an existing knowledge-store-v1 DB, initializing if it is empty.

    - schema/version correct  -> connection returned
    - empty/non-existent file -> initialized via create_store
    - incompatible version    -> StoreSchemaError (no silent migration)
    """
    db_path = Path(db_path)
    if not db_path.exists():
        create_store(db_path)
    conn = _connect(db_path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version == 0:
            conn.close()
            create_store(db_path)
            conn = _connect(db_path)
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version != STORE_USER_VERSION:
            conn.close()
            raise StoreSchemaError(
                f"incompatible schema: PRAGMA user_version={user_version}, "
                f"expected {STORE_USER_VERSION} (knowledge-store-v1)"
            )
        if not _required_tables_exist(conn):
            conn.close()
            raise StoreSchemaError(
                "user_version matches but required tables are missing; "
                "store is corrupt or incompatible"
            )
        return conn
    except Exception:
        conn.close()
        raise


def _read_store_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM store_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


# ----------------------------------------------------------------------
# Document loading & domain validation
# ----------------------------------------------------------------------

def _load_document(knowledge_units_path: Path) -> tuple[dict[str, Any], CanonicalKnowledgeUnitsDocument]:
    """Read + parse + domain-validate a canonical knowledge_units.json.

    Returns (raw_artifact_dict, parsed_document). Invalid documents raise
    StoreIngestError without touching the database.
    """
    knowledge_units_path = Path(knowledge_units_path)
    if not knowledge_units_path.is_file():
        raise StoreIngestError(
            f"knowledge units artifact not found: {knowledge_units_path}"
        )
    try:
        artifact = json.loads(knowledge_units_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreIngestError(
            f"malformed JSON in {knowledge_units_path}: {exc}"
        ) from exc
    if not isinstance(artifact, dict):
        raise StoreIngestError("knowledge document must be a JSON object")
    if artifact.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
        raise StoreIngestError(
            f"unsupported schema_version: {artifact.get('schema_version')!r}; "
            f"expected {KNOWLEDGE_SCHEMA_VERSION!r}"
        )
    try:
        document = CanonicalKnowledgeUnitsDocument.from_dict(artifact)
    except (ValueError, TypeError, KeyError) as exc:
        raise StoreIngestError(f"invalid canonical document: {exc}") from exc
    return artifact, document


# ----------------------------------------------------------------------
# Projection derivation (single source of truth)
# ----------------------------------------------------------------------

def _projection_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically derive projection fields from a canonical payload.

    Projections are always derived from canonical_payload_json so divergence
    is impossible by construction.
    """
    return {
        "knowledge_unit_id": payload["knowledge_unit_id"],
        "canonical_id": payload["canonical_id"],
        "unit_type": payload["unit_type"],
        "statement": payload["statement"],
        "verification_status": payload["verification_status"],
        "extraction_confidence": payload["extraction_confidence"],
    }


def _insert_unit_rows(
    conn: sqlite3.Connection,
    canonical_id: str,
    unit: CanonicalKnowledgeUnit,
) -> None:
    """Insert one unit's projection + payload + child rows (ordinals preserved)."""
    payload = unit.to_dict()
    projection = _projection_from_payload(payload)
    conn.execute(
        """
        INSERT INTO knowledge_units (
            knowledge_unit_id, canonical_id, unit_type, statement,
            verification_status, extraction_confidence, canonical_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            projection["knowledge_unit_id"],
            projection["canonical_id"],
            projection["unit_type"],
            projection["statement"],
            projection["verification_status"],
            projection["extraction_confidence"],
            _payload_json(unit),
        ),
    )
    for ordinal, ref in enumerate(payload["evidence_refs"]):
        temporal = ref.get("temporal_range")
        sequence = ref.get("sequence_range")
        conn.execute(
            """
            INSERT INTO evidence_refs (
                knowledge_unit_id, ordinal, evidence_id, source_excerpt,
                temporal_start, temporal_end, temporal_duration, sequence_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["knowledge_unit_id"],
                ordinal,
                ref["evidence_id"],
                ref.get("source_excerpt", ""),
                temporal["start"] if temporal else None,
                temporal["end"] if temporal else None,
                temporal["duration"] if temporal else None,
                sequence["sequence_index"] if sequence else None,
            ),
        )
    for ordinal, entity in enumerate(payload["entities"]):
        conn.execute(
            """
            INSERT INTO entities (knowledge_unit_id, ordinal, entity_name, category)
            VALUES (?, ?, ?, ?)
            """,
            (
                payload["knowledge_unit_id"],
                ordinal,
                entity["entity_name"],
                entity["category"],
            ),
        )
    for ordinal, topic in enumerate(payload["topics"]):
        conn.execute(
            """
            INSERT INTO topics (knowledge_unit_id, ordinal, topic)
            VALUES (?, ?, ?)
            """,
            (payload["knowledge_unit_id"], ordinal, topic),
        )


def _delete_asset_rows(conn: sqlite3.Connection, canonical_id: str) -> None:
    """Delete an asset's derived rows (cascades via FK to child tables)."""
    conn.execute(
        "DELETE FROM ingested_assets WHERE canonical_id = ?", (canonical_id,)
    )


def _persist_asset(
    conn: sqlite3.Connection,
    canonical_id: str,
    document: CanonicalKnowledgeUnitsDocument,
    *,
    source_artifact_path: str,
    source_artifact_fingerprint: str,
    ingested_at: str,
) -> None:
    """Persist one asset's full projection inside an open transaction."""
    conn.execute(
        """
        INSERT INTO ingested_assets (
            canonical_id, knowledge_schema_version, source_artifact_path,
            source_artifact_fingerprint, ingested_at, unit_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            canonical_id,
            KNOWLEDGE_SCHEMA_VERSION,
            source_artifact_path,
            source_artifact_fingerprint,
            ingested_at,
            len(document.units),
        ),
    )
    for unit in document.units:
        _insert_unit_rows(conn, canonical_id, unit)


def _ingest_document(
    conn: sqlite3.Connection,
    db_path: Path,
    artifact: dict[str, Any],
    document: CanonicalKnowledgeUnitsDocument,
    *,
    source_artifact_path: Path,
) -> IngestResult:
    """Atomic ingestion core: idempotent NO-OP or deterministic replace."""
    canonical_id = document.canonical_id
    fingerprint = compute_source_artifact_fingerprint(artifact)
    source_path_str = str(source_artifact_path)

    existing = conn.execute(
        "SELECT source_artifact_fingerprint, ingested_at FROM ingested_assets "
        "WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    if existing is not None and existing["source_artifact_fingerprint"] == fingerprint:
        # NO-OP / cache hit: no delete, no re-insert, ingested_at preserved.
        return IngestResult(
            status="unchanged",
            canonical_id=canonical_id,
            source_artifact_fingerprint=fingerprint,
            unit_count=len(document.units),
            ingested_at=existing["ingested_at"],
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        previous_fingerprint = None
        if existing is not None:
            previous_fingerprint = existing["source_artifact_fingerprint"]
            _delete_asset_rows(conn, canonical_id)
        _persist_asset(
            conn,
            canonical_id,
            document,
            source_artifact_path=source_path_str,
            source_artifact_fingerprint=fingerprint,
            ingested_at=utc_now(),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if existing is None:
        status = "inserted"
    else:
        status = "replaced"
    ingested_row = conn.execute(
        "SELECT ingested_at FROM ingested_assets WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    return IngestResult(
        status=status,
        canonical_id=canonical_id,
        source_artifact_fingerprint=fingerprint,
        unit_count=len(document.units),
        ingested_at=ingested_row["ingested_at"],
        replaced_from_fingerprint=previous_fingerprint,
    )


# ----------------------------------------------------------------------
# Public ingestion API
# ----------------------------------------------------------------------

def ingest_knowledge_document(
    db_path: Path,
    knowledge_units_path: Path,
) -> IngestResult:
    """Ingest one canonical knowledge_units.json into the store atomically.

    Reads + parses + domain-validates the M4 artifact, then persists the asset
    projection in a single transaction. Idempotent (unchanged) on identical
    fingerprint; deterministic replace on changed fingerprint.
    """
    artifact, document = _load_document(knowledge_units_path)
    db_path = Path(db_path)
    conn = open_store(db_path)
    try:
        return _ingest_document(
            conn, db_path, artifact, document, source_artifact_path=knowledge_units_path
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Read API (minimal, deterministic; NOT retrieval)
# ----------------------------------------------------------------------

def _payload_from_row(row: sqlite3.Row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return json.loads(row["canonical_payload_json"])


def get_unit(db_path: Path, knowledge_unit_id: str) -> Optional[dict[str, Any]]:
    """Return the canonical unit payload for a knowledge_unit_id, or None."""
    conn = open_store(db_path)
    try:
        row = conn.execute(
            "SELECT canonical_payload_json FROM knowledge_units "
            "WHERE knowledge_unit_id = ?",
            (knowledge_unit_id,),
        ).fetchone()
        return _payload_from_row(row)
    finally:
        conn.close()


def list_units_for_asset(db_path: Path, canonical_id: str) -> list[dict[str, Any]]:
    """Return canonical unit payloads for an asset, in artifact order."""
    conn = open_store(db_path)
    try:
        rows = conn.execute(
            "SELECT canonical_payload_json FROM knowledge_units "
            "WHERE canonical_id = ? ORDER BY unit_rowid",
            (canonical_id,),
        ).fetchall()
        return [json.loads(row["canonical_payload_json"]) for row in rows]
    finally:
        conn.close()


def get_ingested_asset(db_path: Path, canonical_id: str) -> Optional[dict[str, Any]]:
    """Return ingested_assets metadata row for a canonical_id, or None."""
    conn = open_store(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ingested_assets WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_ingested_assets(db_path: Path) -> list[dict[str, Any]]:
    """Return all ingested_assets metadata rows (deterministic order)."""
    conn = open_store(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM ingested_assets ORDER BY canonical_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Removal
# ----------------------------------------------------------------------

def remove_asset(db_path: Path, canonical_id: str) -> dict[str, Any]:
    """Remove an asset's derived store rows only (FK cascade).

    Never touches knowledge_units.json, data/processed, M3 evidence, or media.
    A missing asset is a deterministic no-op returning removed=False.
    """
    conn = open_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT unit_count FROM ingested_assets WHERE canonical_id = ?",
                (canonical_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return {"removed": False, "canonical_id": canonical_id, "unit_count": 0}
            conn.execute(
                "DELETE FROM ingested_assets WHERE canonical_id = ?",
                (canonical_id,),
            )
            conn.commit()
            return {
                "removed": True,
                "canonical_id": canonical_id,
                "unit_count": int(row["unit_count"]),
            }
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Store revision (deterministic, source-state only)
# ----------------------------------------------------------------------

def _compute_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT canonical_id, source_artifact_fingerprint "
        "FROM ingested_assets ORDER BY canonical_id"
    ).fetchall()
    pairs = [(row["canonical_id"], row["source_artifact_fingerprint"]) for row in rows]
    return _sha256_json(pairs)


def compute_store_revision(db_path: Path, conn: Optional[sqlite3.Connection] = None) -> str:
    """Deterministic content revision of the store.

    SHA-256 over sorted (canonical_id, source_artifact_fingerprint) pairs.
    Depends only on ingested source state — never ingested_at, rowids, or
    current time. Empty store => SHA-256 of the empty list.
    """
    if conn is not None:
        return _compute_revision(conn)
    conn = open_store(db_path)
    try:
        return _compute_revision(conn)
    finally:
        conn.close()


def _set_store_revision(conn: sqlite3.Connection, revision: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
        (_STORE_META_REVISION_KEY, revision),
    )


# ----------------------------------------------------------------------
# Store validation
# ----------------------------------------------------------------------

def _check_projection_consistency(
    conn: sqlite3.Connection, violations: list[str], counters: dict[str, int]
) -> None:
    rows = conn.execute(
        "SELECT canonical_payload_json, unit_type, statement, "
        "verification_status, extraction_confidence "
        "FROM knowledge_units"
    ).fetchall()
    for row in rows:
        counters["units"] += 1
        try:
            payload = json.loads(row["canonical_payload_json"])
        except json.JSONDecodeError as exc:
            counters["projection_violations"] += 1
            violations.append(f"unit payload is not valid JSON: {exc}")
            continue
        expected = _projection_from_payload(payload)
        if (
            expected["unit_type"] != row["unit_type"]
            or expected["statement"] != row["statement"]
            or expected["verification_status"] != row["verification_status"]
            or abs(expected["extraction_confidence"] - row["extraction_confidence"]) > 1e-9
        ):
            counters["projection_violations"] += 1
            violations.append(
                f"projection divergence for unit {payload.get('knowledge_unit_id')}"
            )


def _check_child_ordinals(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    id_column: str,
    violations: list[str],
    counters: dict[str, int],
) -> None:
    rows = conn.execute(
        f"SELECT {id_column}, ordinal, COUNT(*) AS c FROM {table} "
        f"GROUP BY {id_column}, ordinal HAVING COUNT(*) > 1"
    ).fetchall()
    for row in rows:
        violations.append(
            f"duplicate ordinal in {table}: {key_column}={row[id_column]} "
            f"ordinal={row['ordinal']}"
        )
        counters["duplicate_ordinals"] += 1
    # Contiguity: ordinals must be exactly 0..n-1 per parent.
    parents = conn.execute(
        f"SELECT {id_column} FROM {table} GROUP BY {id_column}"
    ).fetchall()
    for parent in parents:
        parent_id = parent[id_column]
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {id_column} = ?", (parent_id,)
        ).fetchone()[0]
        expected = set(range(count))
        actual = {
            row[0]
            for row in conn.execute(
                f"SELECT ordinal FROM {table} WHERE {id_column} = ?", (parent_id,)
            ).fetchall()
        }
        if actual != expected:
            violations.append(
                f"non-contiguous ordinals in {table} for {key_column}={parent_id}"
            )
            counters["invalid_ordinals"] += 1


def validate_store(db_path: Path) -> StoreValidationResult:
    """Validate the store's structural and application invariants.

    Checks: schema version, foreign key integrity, asset unit_count, orphan
    rows, projection-vs-payload consistency, KU canonical_id vs owning asset,
    and child ordinal consistency.
    """
    conn = open_store(db_path)
    try:
        violations: list[str] = []
        counters: dict[str, int] = {
            "units": 0,
            "assets": 0,
            "evidence_refs": 0,
            "entities": 0,
            "topics": 0,
            "projection_violations": 0,
            "orphan_rows": 0,
            "asset_unit_count_mismatches": 0,
            "ku_asset_mismatches": 0,
            "duplicate_ordinals": 0,
            "invalid_ordinals": 0,
        }
        counters["assets"] = conn.execute(
            "SELECT COUNT(*) FROM ingested_assets"
        ).fetchone()[0]
        counters["evidence_refs"] = conn.execute(
            "SELECT COUNT(*) FROM evidence_refs"
        ).fetchone()[0]
        counters["entities"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        counters["topics"] = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]

        # Schema version
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version != STORE_USER_VERSION:
            violations.append(
                f"user_version={user_version}, expected {STORE_USER_VERSION}"
            )
        meta_version = _read_store_meta(conn, _STORE_META_SCHEMA_VERSION_KEY)
        if meta_version != STORE_SCHEMA_VERSION:
            violations.append(
                f"store_meta schema_version={meta_version!r}, "
                f"expected {STORE_SCHEMA_VERSION!r}"
            )

        # Foreign key integrity
        fk_check = conn.execute("PRAGMA foreign_key_check").fetchall()
        for row in fk_check:
            counters["orphan_rows"] += 1
            violations.append(f"foreign key violation: {dict(row)}")

        # Asset unit_count invariant
        asset_rows = conn.execute(
            """
            SELECT a.canonical_id, a.unit_count,
                   (SELECT COUNT(*) FROM knowledge_units ku
                    WHERE ku.canonical_id = a.canonical_id) AS actual_units
            FROM ingested_assets a
            """
        ).fetchall()
        for asset in asset_rows:
            if asset["actual_units"] != asset["unit_count"]:
                counters["asset_unit_count_mismatches"] += 1
                violations.append(
                    f"asset {asset['canonical_id']}: declared unit_count "
                    f"{asset['unit_count']} != actual {asset['actual_units']}"
                )

        # KU canonical_id vs owning asset
        ku_rows = conn.execute(
            """
            SELECT ku.knowledge_unit_id, ku.canonical_id
            FROM knowledge_units ku
            LEFT JOIN ingested_assets a ON a.canonical_id = ku.canonical_id
            WHERE a.canonical_id IS NULL
            """
        ).fetchall()
        for row in ku_rows:
            counters["orphan_rows"] += 1
            violations.append(
                f"unit {row['knowledge_unit_id']} references missing asset "
                f"{row['canonical_id']}"
            )

        # Projection consistency
        _check_projection_consistency(conn, violations, counters)

        # Child ordinal consistency
        _check_child_ordinals(
            conn, "evidence_refs", "knowledge_unit_id", "knowledge_unit_id",
            violations, counters,
        )
        _check_child_ordinals(
            conn, "entities", "knowledge_unit_id", "knowledge_unit_id",
            violations, counters,
        )
        _check_child_ordinals(
            conn, "topics", "knowledge_unit_id", "knowledge_unit_id",
            violations, counters,
        )

        # Store revision (deterministic; reuse this connection, no nesting)
        revision = _compute_revision(conn)

        valid = len(violations) == 0
        return StoreValidationResult(
            valid=valid,
            schema_version=STORE_SCHEMA_VERSION,
            schema_policy_version=STORE_SCHEMA_POLICY_VERSION,
            store_revision=revision,
            asset_count=counters["assets"],
            unit_count=counters["units"],
            checks=counters,
            violations=violations,
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Rebuild
# ----------------------------------------------------------------------

def discover_final_artifacts(processed_root: Path) -> list[Path]:
    """Discover canonical M4 final artifacts.

    Only the exact pattern data/processed/*/knowledge/knowledge_units.json is
    accepted. Intermediate artifacts (knowledge_candidates.json,
    merged_knowledge_candidates.json, enriched_knowledge_candidates.json) are
    never treated as final sources.
    """
    processed_root = Path(processed_root)
    artifacts: list[Path] = []
    if not processed_root.is_dir():
        return artifacts
    for asset_dir in sorted(p for p in processed_root.iterdir() if p.is_dir()):
        candidate = asset_dir / "knowledge" / FINAL_ARTIFACT_FILENAME
        if candidate.is_file():
            artifacts.append(candidate)
    return artifacts


def rebuild_store(db_path: Path, processed_root: Path) -> StoreValidationResult:
    """Rebuild the store from all discovered canonical M4 final artifacts.

    Safe rebuild strategy:
      A. fully rebuild into a temporary SQLite DB (same directory for atomic
         replace),
      B. validate the temporary store,
      C. on success, atomically replace the official DB.

    Fail-fast: any invalid artifact aborts the rebuild and leaves the existing
    store untouched. No source artifact is ever modified.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts = discover_final_artifacts(processed_root)
    if not artifacts:
        raise StoreIngestError(
            f"no canonical final artifacts found under {processed_root}"
        )

    fd, temp_path_str = tempfile.mkstemp(
        prefix=f".{db_path.name}.", suffix=".tmp", dir=str(db_path.parent)
    )
    os.close(fd)
    os.unlink(temp_path_str)
    temp_path = Path(temp_path_str)
    try:
        create_store(temp_path)
        ingested: list[str] = []
        for artifact_path in artifacts:
            result = ingest_knowledge_document(temp_path, artifact_path)
            if result.status == "inserted":
                ingested.append(result.canonical_id)
            # replaced/unchanged should never occur on a fresh temp store
        validation = validate_store(temp_path)
        if not validation.valid:
            raise StoreValidationError(
                "rebuild produced an invalid store: "
                + "; ".join(validation.violations[:10])
            )
        # Close any lingering handle then atomically replace the official DB.
        try:
            conn = open_store(db_path)
            conn.close()
        except (StoreSchemaError, sqlite3.Error):
            pass
        os.replace(temp_path, db_path)
        # Refresh store_meta rebuilt_at + revision.
        conn = open_store(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_META_REBUILT_AT_KEY, utc_now()),
            )
            _set_store_revision(conn, _compute_revision(conn))
            conn.commit()
        finally:
            conn.close()
        return validate_store(db_path)
    finally:
        temp_path.unlink(missing_ok=True)