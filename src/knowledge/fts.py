"""M5-02 SQLite FTS5 Lexical / Metadata Indexing (trigram).

Builds a deterministic lexical index over the M5-01 canonical store:

  knowledge_fts_content      materialized FTS content projection (derived)
  knowledge_fts              external-content FTS5 index over the projection

Design decisions (M5-02 probe, recorded in M5_DECISIONS.md):

  - Tokenizer: FTS5 `trigram`. unicode61 was rejected because probe showed it
    tokenizes each contiguous CJK(+Latin) run as a single token, so Chinese
    words and Latin tokens embedded in mixed text (Vulkan / 27B / RDNA /
    Thinking) cannot be matched. trigram matches every >=3 character query.
  - Short-query limitation: trigram cannot match queries shorter than 3
    characters. This is a documented known lexical limitation; M5-03/M5-04
    add a deterministic short-query fallback. No LIKE fallback here.
  - Field weights (frozen): statement 5.0, entity_names 2.0, topics 2.0,
    evidence_excerpts 1.0. BM25 is a lexical ranking signal only, never a
    relevance probability.
  - The FTS index is a derived projection, never the source of truth. The
    canonical store (knowledge_units + payload) remains authoritative.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

# ----------------------------------------------------------------------
# Frozen policy constants
# ----------------------------------------------------------------------

# Version of the FTS schema/tokenizer/weighting policy. Stored in store_meta
# so a future schema change is detectable without a migration engine.
FTS_POLICY_VERSION = "m5-fts-trigram-v1"

FTS_TOKENIZER = "trigram"

# Column order in knowledge_fts (must match the CREATE VIRTUAL TABLE).
FTS_COLUMNS = ("statement", "entity_names", "topics", "evidence_excerpts")

# Frozen bm25() column weights (statement highest, evidence lowest).
FTS_FIELD_WEIGHTS = {"statement": 5.0, "entity_names": 2.0, "topics": 2.0, "evidence_excerpts": 1.0}

FTS_CONTENT_TABLE = "knowledge_fts_content"
FTS_INDEX_TABLE = "knowledge_fts"

# Separator used when materializing child rows into content fields. A single
# space is tokenizer-friendly and deterministic.
FTS_FIELD_SEPARATOR = " "


# ----------------------------------------------------------------------
# FTS schema DDL (appended to the store schema by store.create_store)
# ----------------------------------------------------------------------

# Must be executed AFTER knowledge_units is created (FK reference).
FTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_fts_content (
    unit_rowid INTEGER PRIMARY KEY,
    statement TEXT NOT NULL,
    entity_names TEXT NOT NULL,
    topics TEXT NOT NULL,
    evidence_excerpts TEXT NOT NULL,
    FOREIGN KEY (unit_rowid) REFERENCES knowledge_units(unit_rowid) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    statement,
    entity_names,
    topics,
    evidence_excerpts,
    content='knowledge_fts_content',
    content_rowid='unit_rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge_fts_content BEGIN
  INSERT INTO knowledge_fts(rowid, statement, entity_names, topics, evidence_excerpts)
  VALUES (new.unit_rowid, new.statement, new.entity_names, new.topics, new.evidence_excerpts);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge_fts_content BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, statement, entity_names, topics, evidence_excerpts)
  VALUES ('delete', old.unit_rowid, old.statement, old.entity_names, old.topics, old.evidence_excerpts);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE ON knowledge_fts_content BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, statement, entity_names, topics, evidence_excerpts)
  VALUES ('delete', old.unit_rowid, old.statement, old.entity_names, old.topics, old.evidence_excerpts);
  INSERT INTO knowledge_fts(rowid, statement, entity_names, topics, evidence_excerpts)
  VALUES (new.unit_rowid, new.statement, new.entity_names, new.topics, new.evidence_excerpts);
END;
"""


# ----------------------------------------------------------------------
# Deterministic content materialization
# ----------------------------------------------------------------------

def build_fts_content_values(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    """Derive the FTS content row for a canonical unit payload.

    Deterministic: statement verbatim; entity_names / topics / evidence_excerpts
    joined by ``FTS_FIELD_SEPARATOR`` in canonical ordinal order. Never re-sorts,
    never summarizes, never includes attribution / verification / confidence.
    """
    statement = payload.get("statement", "")
    entity_names = FTS_FIELD_SEPARATOR.join(
        entity["entity_name"] for entity in payload.get("entities", [])
    )
    topics = FTS_FIELD_SEPARATOR.join(payload.get("topics", []))
    evidence_excerpts = FTS_FIELD_SEPARATOR.join(
        ref.get("source_excerpt", "") for ref in payload.get("evidence_refs", [])
    )
    return statement, entity_names, topics, evidence_excerpts


# ----------------------------------------------------------------------
# Literal query safety
# ----------------------------------------------------------------------

def literal_fts_query(query_text: str) -> str:
    """Turn plain user text into a literal FTS5 phrase query.

    The entire user input is wrapped in double quotes and embedded quotes are
    doubled (FTS5 phrase escaping). This disables all FTS5 query-language
    syntax (AND/OR/NEAR/^/*/parens/colon/hyphen semantics) so ordinary user
    text -- including quotes, hyphens, parens, asterisks, colons and CJK
    punctuation -- is always treated literally and never spliced into SQL.
    """
    return '"' + str(query_text).replace('"', '""') + '"'


# ----------------------------------------------------------------------
# Low-level lexical query (internal test helper, NOT the M5-03 Retrieval API)
# ----------------------------------------------------------------------

def lexical_search_rows(
    conn: sqlite3.Connection,
    query_text: str,
    limit: int,
    *,
    weights: Optional[dict[str, float]] = None,
) -> list[tuple[int, float]]:
    """Low-level lexical search over the FTS index.

    Returns ``[(unit_rowid, bm25_score), ...]`` ordered by best score first.
    ``unit_rowid`` maps 1:1 to ``knowledge_units.unit_rowid``.

    This is an internal helper to exercise the index; the public Retrieval API
    (RetrievalQuery / RetrievalHit / RetrievalResult) is M5-03 scope.
    """
    field_weights = weights or FTS_FIELD_WEIGHTS
    w = [field_weights[col] for col in FTS_COLUMNS]
    params = [w[0], w[1], w[2], w[3], literal_fts_query(query_text), limit]
    rows = conn.execute(
        f"""
        SELECT rowid AS unit_rowid, bm25({FTS_INDEX_TABLE}, ?, ?, ?, ?) AS bm25_score
        FROM {FTS_INDEX_TABLE}
        WHERE {FTS_INDEX_TABLE} MATCH ?
        ORDER BY bm25_score
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [(row["unit_rowid"], row["bm25_score"]) for row in rows]


def fts_index_count(conn: sqlite3.Connection) -> int:
    """Number of rows currently present in the FTS index."""
    return conn.execute(f"SELECT COUNT(*) FROM {FTS_INDEX_TABLE}").fetchone()[0]


def fts_integrity_check(conn: sqlite3.Connection) -> None:
    """Run the FTS5 integrity check; raises sqlite3.OperationalError on failure."""
    conn.execute(f"INSERT INTO {FTS_INDEX_TABLE}({FTS_INDEX_TABLE}) VALUES('integrity-check')")