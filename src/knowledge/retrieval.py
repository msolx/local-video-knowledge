"""M5-03 Evidence-Grounded Retrieval API.

Public retrieval contract (``RetrievalQuery -> RetrievalHit[] ->
RetrievalResult``) over the sealed M5-01 canonical store and the M5-02 trigram
FTS5 lexical index.

Design rules (frozen in docs/M5_KNOWLEDGE_STORE_DESIGN.md §12-16 and recorded
in M5_DECISIONS.md):

- Retrieval != answering: hits carry knowledge AND full provenance
  (verbatim canonical unit, evidence refs, source artifact reference). No LLM,
  no RAG, no reranker, no embeddings.
- Canonical hydration: a hit's content always comes from
  ``knowledge_units.canonical_payload_json`` via ``CanonicalKnowledgeUnit.
  from_dict`` — never reassembled from projection columns.
- Evidence expansion is always on: ``evidence_refs`` preserves the canonical
  unit's original evidence order (id + verbatim excerpt + temporal/sequence
  coordinates).
- Filters target structured projection columns / tables (canonical_id,
  unit_type, verification_status, topics, entity_names) and are applied in SQL
  BEFORE ranking / LIMIT so filtered-out rows can never crowd out hits.
- BM25 (FTS path): lower is better. raw_bm25 is exposed as a diagnostic; it is
  never negated into a fake probability. ORDER BY bm25 ASC.
- Short-query fallback (terms < 3 chars, a documented trigram limitation):
  deterministic literal substring scan over ``knowledge_fts_content`` using
  parameterized ``instr()`` (never LIKE wildcards, never a user-controlled
  pattern). Weighted short score (statement 5 / entity_names 2 / topics 2 /
  evidence_excerpts 1) — higher is better.
- Determinism: ties are broken by ``unit_rowid`` ASC. No retrieval cache.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .fts import (
    FTS_COLUMNS,
    FTS_CONTENT_TABLE,
    FTS_FIELD_WEIGHTS,
    FTS_INDEX_TABLE,
    literal_fts_query,
)
from .models import (
    AttributionInfo,
    CanonicalKnowledgeUnit,
    EntityMention,
    EvidenceRef,
    ExtractionLineage,
    UnitType,
    VerificationStatus,
)
from .store import STORE_SCHEMA_VERSION, compute_store_revision, open_store

# ----------------------------------------------------------------------
# Frozen policy constants
# ----------------------------------------------------------------------

MIN_TOP_K = 1
MAX_TOP_K = 100

RETRIEVAL_METHOD_FTS = "lexical_fts5_trigram"
RETRIEVAL_METHOD_SHORT = "lexical_substring_short"
RETRIEVAL_METHOD_MIXED = "lexical_fts5_trigram_with_short_filter"

# Deterministic short-query field weights (mirrors FTS_FIELD_WEIGHTS).
_SHORT_FIELDS = ("statement", "entity_names", "topics", "evidence_excerpts")

# ----------------------------------------------------------------------
# Query normalization & literal planning
# ----------------------------------------------------------------------


def normalize_retrieval_query(query_text: str) -> str:
    """Deterministic lexical normalization: Unicode NFKC, strip, collapse
    whitespace. No stemming, no semantic rewrite, no synonyms, no
    segmentation."""
    if not isinstance(query_text, str):
        raise TypeError("query_text must be a string")
    normalized = unicodedata.normalize("NFKC", query_text)
    return re.sub(r"\s+", " ", normalized.strip())


def plan_literal_terms(normalized_query: str) -> list[str]:
    """Split a normalized query into literal terms by whitespace.

    Every term is treated as literal data only — it never gains FTS operator
    authority. User text is never spliced into SQL or MATCH.
    """
    return [term for term in normalized_query.split(" ") if term]


# ----------------------------------------------------------------------
# RetrievalQuery
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalQuery:
    """Stable v1 retrieval query envelope (frozen contract).

    ``query_text`` is required and must be non-empty after stripping.
    ``top_k`` must be in ``[1, MAX_TOP_K]``.

    Filters (all optional):

    - ``canonical_ids``            -> ingested_assets projection column
    - ``unit_types``               -> UnitType enum validated values
    - ``verification_statuses``    -> VerificationStatus enum validated values
    - ``topics``                   -> exact structured topics table values
    - ``entity_names``             -> exact structured entities table values

    Semantics: values within one filter are OR'd; filter categories are AND'd.
    Empty / None collections are treated as unset (never produce SQL that is
    always false).
    """

    query_text: str
    top_k: int = 10
    canonical_ids: Optional[list[str]] = None
    unit_types: Optional[list[str]] = None
    verification_statuses: Optional[list[str]] = None
    topics: Optional[list[str]] = None
    entity_names: Optional[list[str]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.query_text, str) or not self.query_text.strip():
            raise ValueError("query_text must be a non-empty string")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise ValueError("top_k must be an integer")
        if not (MIN_TOP_K <= self.top_k <= MAX_TOP_K):
            raise ValueError(f"top_k must be in [{MIN_TOP_K}, {MAX_TOP_K}]")

        for attr in ("canonical_ids", "unit_types", "verification_statuses", "topics", "entity_names"):
            value = getattr(self, attr)
            if value is None:
                continue
            cleaned = [v for v in value if v]
            object.__setattr__(self, attr, cleaned if cleaned else None)

        if self.unit_types:
            valid_types = {t.value for t in UnitType}
            for value in self.unit_types:
                if value not in valid_types:
                    raise ValueError(f"invalid unit_type: '{value}'")
        if self.verification_statuses:
            valid_statuses = {s.value for s in VerificationStatus}
            for value in self.verification_statuses:
                if value not in valid_statuses:
                    raise ValueError(f"invalid verification_status: '{value}'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_text": self.query_text,
            "top_k": self.top_k,
            "canonical_ids": list(self.canonical_ids) if self.canonical_ids else None,
            "unit_types": list(self.unit_types) if self.unit_types else None,
            "verification_statuses": list(self.verification_statuses) if self.verification_statuses else None,
            "topics": list(self.topics) if self.topics else None,
            "entity_names": list(self.entity_names) if self.entity_names else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetrievalQuery:
        if not isinstance(d, dict):
            raise TypeError("RetrievalQuery payload must be a dict")
        return cls(
            query_text=str(d["query_text"]),
            top_k=int(d.get("top_k", 10)),
            canonical_ids=[str(v) for v in d["canonical_ids"]] if d.get("canonical_ids") else None,
            unit_types=[str(v) for v in d["unit_types"]] if d.get("unit_types") else None,
            verification_statuses=[str(v) for v in d["verification_statuses"]] if d.get("verification_statuses") else None,
            topics=[str(v) for v in d["topics"]] if d.get("topics") else None,
            entity_names=[str(v) for v in d["entity_names"]] if d.get("entity_names") else None,
        )


# ----------------------------------------------------------------------
# RetrievalHit
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalHit:
    """A retrieval hit carrying the full canonical unit and its provenance.

    ``evidence_refs`` is always fully populated from the canonical payload
    (never truncated, never re-sorted). ``source_artifact`` references the
    M4 ``knowledge_units.json`` the unit came from. ``match_info`` and
    ``ranking_diagnostics`` are deterministic, JSON-safe explanations.
    """

    rank: int
    knowledge_unit_id: str
    canonical_id: str
    unit_type: str
    statement: str
    verification_status: str
    extraction_confidence: float
    entities: list[EntityMention]
    topics: list[str]
    attribution: AttributionInfo
    evidence_refs: list[EvidenceRef]
    extraction_lineage: ExtractionLineage
    source_artifact: dict[str, Any]
    match_info: dict[str, Any]
    ranking_diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "knowledge_unit_id": self.knowledge_unit_id,
            "canonical_id": self.canonical_id,
            "unit_type": self.unit_type,
            "statement": self.statement,
            "verification_status": self.verification_status,
            "extraction_confidence": self.extraction_confidence,
            "entities": [e.to_dict() for e in self.entities],
            "topics": list(self.topics),
            "attribution": self.attribution.to_dict(),
            "evidence_refs": [r.to_dict() for r in self.evidence_refs],
            "extraction_lineage": self.extraction_lineage.to_dict(),
            "source_artifact": dict(self.source_artifact),
            "match_info": dict(self.match_info),
            "ranking_diagnostics": dict(self.ranking_diagnostics),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetrievalHit:
        if not isinstance(d, dict):
            raise TypeError("RetrievalHit payload must be a dict")
        return cls(
            rank=int(d["rank"]),
            knowledge_unit_id=str(d["knowledge_unit_id"]),
            canonical_id=str(d["canonical_id"]),
            unit_type=str(d["unit_type"]),
            statement=str(d["statement"]),
            verification_status=str(d["verification_status"]),
            extraction_confidence=float(d["extraction_confidence"]),
            entities=[EntityMention.from_dict(e) for e in d.get("entities", [])],
            topics=[str(t) for t in d.get("topics", [])],
            attribution=AttributionInfo.from_dict(d.get("attribution", {})),
            evidence_refs=[EvidenceRef.from_dict(r) for r in d.get("evidence_refs", [])],
            extraction_lineage=ExtractionLineage.from_dict(d.get("extraction_lineage", {})),
            source_artifact=dict(d.get("source_artifact", {})),
            match_info=dict(d.get("match_info", {})),
            ranking_diagnostics=dict(d.get("ranking_diagnostics", {})),
        )


# ----------------------------------------------------------------------
# RetrievalResult
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalResult:
    """Stable retrieval result envelope. Never contains an LLM answer, a
    generated answer, or a truth score."""

    query: RetrievalQuery
    retrieval_method: str
    store_schema_version: str
    store_revision: Optional[str]
    result_count: int
    hits: list[RetrievalHit] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "retrieval_method": self.retrieval_method,
            "store_schema_version": self.store_schema_version,
            "store_revision": self.store_revision,
            "result_count": self.result_count,
            "hits": [h.to_dict() for h in self.hits],
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetrievalResult:
        if not isinstance(d, dict):
            raise TypeError("RetrievalResult payload must be a dict")
        return cls(
            query=RetrievalQuery.from_dict(d["query"]),
            retrieval_method=str(d["retrieval_method"]),
            store_schema_version=str(d["store_schema_version"]),
            store_revision=d.get("store_revision"),
            result_count=int(d["result_count"]),
            hits=[RetrievalHit.from_dict(h) for h in d.get("hits", [])],
            diagnostics=dict(d.get("diagnostics", {})),
        )


# ----------------------------------------------------------------------
# Retrieval backend abstraction (extension point; M5 v1 ships FTS5 only)
# ----------------------------------------------------------------------


class RetrievalBackend(ABC):
    """Extension point for future dense / hybrid backends (design §14.5).

    M5 v1 implements only ``FTS5RetrievalBackend``. Dense / hybrid backends are
    explicitly not implemented in M5.
    """

    @abstractmethod
    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        raise NotImplementedError


class FTS5RetrievalBackend(RetrievalBackend):
    """Lexical FTS5 backend over a concrete store DB file."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        return retrieve(self._db_path, query)


# ----------------------------------------------------------------------
# Structured filter SQL (applied before ranking / LIMIT)
# ----------------------------------------------------------------------


def _build_filter_sql(query: RetrievalQuery, params: list[Any]) -> str:
    """Append AND filter clauses to the current WHERE clause.

    Filters run against structured projection columns/tables only — never FTS
    MATCH text. Same-category values are OR'd; categories are AND'd.
    """
    clauses: list[str] = []
    if query.canonical_ids:
        placeholders = ",".join("?" * len(query.canonical_ids))
        clauses.append(f"k.canonical_id IN ({placeholders})")
        params.extend(query.canonical_ids)
    if query.unit_types:
        placeholders = ",".join("?" * len(query.unit_types))
        clauses.append(f"k.unit_type IN ({placeholders})")
        params.extend(query.unit_types)
    if query.verification_statuses:
        placeholders = ",".join("?" * len(query.verification_statuses))
        clauses.append(f"k.verification_status IN ({placeholders})")
        params.extend(query.verification_statuses)
    if query.topics:
        placeholders = ",".join("?" * len(query.topics))
        clauses.append(
            f"EXISTS (SELECT 1 FROM topics t "
            f"WHERE t.knowledge_unit_id = k.knowledge_unit_id AND t.topic IN ({placeholders}))"
        )
        params.extend(query.topics)
    if query.entity_names:
        placeholders = ",".join("?" * len(query.entity_names))
        clauses.append(
            f"EXISTS (SELECT 1 FROM entities e "
            f"WHERE e.knowledge_unit_id = k.knowledge_unit_id AND e.entity_name IN ({placeholders}))"
        )
        params.extend(query.entity_names)
    return (" AND " + " AND ".join(clauses)) if clauses else ""


# ----------------------------------------------------------------------
# Query planner -> SQL paths
# ----------------------------------------------------------------------


def _fts_match_sql(long_terms: list[str]) -> str:
    """Construct an FTS MATCH expression with literal AND semantics.

    Every term is wrapped by ``literal_fts_query`` (quoted phrase, embedded
    quotes doubled) so no user text gains FTS operator authority. Terms are
    ANDed together: ``"Vulkan" AND "27B"``.
    """
    return " AND ".join(literal_fts_query(term) for term in long_terms)


def _short_score_sql(short_terms: list[str], params: list[Any]) -> str:
    """Deterministic weighted short score: sum over terms of field weights for
    every content field the term matches. Higher is better."""
    parts: list[str] = []
    for term in short_terms:
        lower_term = term.lower()
        field_parts: list[str] = []
        for col in _SHORT_FIELDS:
            weight = FTS_FIELD_WEIGHTS[col]
            field_parts.append(f"(instr(lower(c.{col}), ?)>0)*{weight:g}")
            params.append(lower_term)
        parts.append("(" + " + ".join(field_parts) + ")")
    return " + ".join(parts)


def _short_condition_sql(short_terms: list[str], params: list[Any]) -> str:
    """Per-term literal substring conditions over all content fields, AND'd
    across terms. Each term must match at least one field."""
    parts: list[str] = []
    for term in short_terms:
        lower_term = term.lower()
        field_parts = [
            f"instr(lower(c.{col}), ?)>0" for col in _SHORT_FIELDS
        ]
        parts.append("(" + " OR ".join(field_parts) + ")")
        params.extend([lower_term] * len(_SHORT_FIELDS))
    return " AND ".join(parts)


def _fts_retrieve(
    conn: sqlite3.Connection,
    long_terms: list[str],
    query: RetrievalQuery,
) -> list[tuple[int, float]]:
    """All-long-terms path: trigram FTS with literal AND, filters before LIMIT."""
    field_weights = [FTS_FIELD_WEIGHTS[col] for col in FTS_COLUMNS]
    params: list[Any] = list(field_weights)
    match_sql = _fts_match_sql(long_terms)
    params.append(match_sql)
    filter_sql = _build_filter_sql(query, params)
    params.append(query.top_k)
    sql = f"""
        SELECT k.unit_rowid AS unit_rowid,
               bm25({FTS_INDEX_TABLE}, ?, ?, ?, ?) AS bm25_score
        FROM {FTS_INDEX_TABLE}
        JOIN knowledge_units k ON k.unit_rowid = {FTS_INDEX_TABLE}.rowid
        WHERE {FTS_INDEX_TABLE} MATCH ?{filter_sql}
        ORDER BY bm25_score, k.unit_rowid
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [(row["unit_rowid"], float(row["bm25_score"])) for row in rows]


def _short_retrieve(
    conn: sqlite3.Connection,
    short_terms: list[str],
    query: RetrievalQuery,
) -> list[tuple[int, float]]:
    """All-short path: deterministic literal substring over the materialized
    FTS content table. Weighted short score, higher is better."""
    score_params: list[Any] = []
    condition_params: list[Any] = []
    score_sql = _short_score_sql(short_terms, score_params)
    condition_sql = _short_condition_sql(short_terms, condition_params)
    params: list[Any] = score_params + condition_params
    filter_sql = _build_filter_sql(query, params)
    params.append(query.top_k)
    sql = f"""
        SELECT k.unit_rowid AS unit_rowid, ({score_sql}) AS short_score
        FROM {FTS_CONTENT_TABLE} c
        JOIN knowledge_units k ON k.unit_rowid = c.unit_rowid
        WHERE {condition_sql}{filter_sql}
        ORDER BY short_score DESC, k.unit_rowid
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [(row["unit_rowid"], float(row["short_score"])) for row in rows]


def _mixed_retrieve(
    conn: sqlite3.Connection,
    long_terms: list[str],
    short_terms: list[str],
    query: RetrievalQuery,
) -> list[tuple[int, float]]:
    """Mixed path: FTS constrains the long terms; structured substring
    conditions additionally require every short term to match content. AND
    semantics. Ranking stays raw BM25 (lower is better)."""
    field_weights = [FTS_FIELD_WEIGHTS[col] for col in FTS_COLUMNS]
    params: list[Any] = list(field_weights)
    match_sql = _fts_match_sql(long_terms)
    params.append(match_sql)
    condition_params: list[Any] = []
    condition_sql = _short_condition_sql(short_terms, condition_params)
    params.extend(condition_params)
    filter_sql = _build_filter_sql(query, params)
    params.append(query.top_k)
    sql = f"""
        SELECT k.unit_rowid AS unit_rowid,
               bm25({FTS_INDEX_TABLE}, ?, ?, ?, ?) AS bm25_score
        FROM {FTS_INDEX_TABLE}
        JOIN knowledge_units k ON k.unit_rowid = {FTS_INDEX_TABLE}.rowid
        JOIN {FTS_CONTENT_TABLE} c ON c.unit_rowid = k.unit_rowid
        WHERE {FTS_INDEX_TABLE} MATCH ?
          AND {condition_sql}{filter_sql}
        ORDER BY bm25_score, k.unit_rowid
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [(row["unit_rowid"], float(row["bm25_score"])) for row in rows]


# ----------------------------------------------------------------------
# Match info (deterministic field-content explanation)
# ----------------------------------------------------------------------


def _compute_match_info(
    terms: list[str],
    content_row: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Determine matched_fields / matched_terms by deterministic literal
    field-content checks (no model, no fuzzy). Case-insensitive for ASCII
    via ``.lower()`` (CJK is identity)."""
    values = {col: str(content_row[col] or "") for col in _SHORT_FIELDS}
    matched_fields: list[str] = []
    matched_terms: list[str] = []
    for term in terms:
        lower_term = term.lower()
        if any(lower_term in values[col].lower() for col in _SHORT_FIELDS):
            matched_terms.append(term)
    for col in _SHORT_FIELDS:
        lower_value = values[col].lower()
        if any(term.lower() in lower_value for term in terms):
            matched_fields.append(col)
    return matched_fields, matched_terms


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------


def retrieve(db_path: Path | str, query: RetrievalQuery | dict[str, Any]) -> RetrievalResult:
    """Stable public retrieval entrypoint.

    ``query`` may be a ``RetrievalQuery`` or a JSON-safe dict (auto-coerced via
    ``RetrievalQuery.from_dict``). Opens the store read-only for the duration
    of the call; callers never touch the sqlite Connection directly.
    """
    if isinstance(query, dict):
        query = RetrievalQuery.from_dict(query)
    if not isinstance(query, RetrievalQuery):
        raise TypeError("query must be a RetrievalQuery (or a dict thereof)")

    normalized = normalize_retrieval_query(query.query_text)
    if not normalized:
        raise ValueError("query_text is empty after normalization")
    terms = plan_literal_terms(normalized)
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) <= 2]

    conn = open_store(Path(db_path))
    try:
        revision = compute_store_revision(Path(db_path), conn)
        if long_terms and not short_terms:
            method = RETRIEVAL_METHOD_FTS
            rows = _fts_retrieve(conn, long_terms, query)
        elif short_terms and not long_terms:
            method = RETRIEVAL_METHOD_SHORT
            rows = _short_retrieve(conn, short_terms, query)
        else:
            method = RETRIEVAL_METHOD_MIXED
            rows = _mixed_retrieve(conn, long_terms, short_terms, query)

        asset_cache: dict[str, dict[str, Any]] = {}
        hits = []
        for rank, (unit_rowid, score) in enumerate(rows, start=1):
            hits.append(_build_hit(conn, rank, unit_rowid, score, query, method, long_terms, short_terms, asset_cache))

        return RetrievalResult(
            query=query,
            retrieval_method=method,
            store_schema_version=STORE_SCHEMA_VERSION,
            store_revision=revision,
            result_count=len(hits),
            hits=hits,
            diagnostics={
                "normalized_query": normalized,
                "terms": terms,
                "long_terms": long_terms,
                "short_terms": short_terms,
                "short_query_fallback": bool(short_terms),
            },
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Hit hydration (canonical payload -> full provenance)
# ----------------------------------------------------------------------


def _load_content_row(conn: sqlite3.Connection, unit_rowid: int) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT * FROM {FTS_CONTENT_TABLE} WHERE unit_rowid = ?",
        (unit_rowid,),
    ).fetchone()
    return dict(row)


def _asset_metadata(conn: sqlite3.Connection, canonical_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if canonical_id in cache:
        return cache[canonical_id]
    row = conn.execute(
        "SELECT source_artifact_path, source_artifact_fingerprint "
        "FROM ingested_assets WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    meta = {
        "path": row["source_artifact_path"] if row is not None else None,
        "fingerprint": row["source_artifact_fingerprint"] if row is not None else None,
    }
    cache[canonical_id] = meta
    return meta


def _build_hit(
    conn: sqlite3.Connection,
    rank: int,
    unit_rowid: int,
    score: float,
    query: RetrievalQuery,
    method: str,
    long_terms: list[str],
    short_terms: list[str],
    _asset_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> RetrievalHit:
    row = conn.execute(
        "SELECT canonical_payload_json FROM knowledge_units WHERE unit_rowid = ?",
        (unit_rowid,),
    ).fetchone()
    payload = json.loads(row["canonical_payload_json"])
    unit = CanonicalKnowledgeUnit.from_dict(payload)

    content_row = _load_content_row(conn, unit_rowid)
    terms = long_terms + short_terms
    matched_fields, matched_terms = _compute_match_info(terms, content_row)

    if method == RETRIEVAL_METHOD_FTS:
        ranking_diagnostics = {
            "method": method,
            "raw_bm25": float(score),
            "field_weights": dict(FTS_FIELD_WEIGHTS),
            "rank": rank,
        }
    elif method == RETRIEVAL_METHOD_SHORT:
        ranking_diagnostics = {
            "method": method,
            "weighted_substring_score": float(score),
            "matched_fields": matched_fields,
            "rank": rank,
        }
    else:
        ranking_diagnostics = {
            "method": method,
            "long_terms": list(long_terms),
            "short_terms": list(short_terms),
            "raw_bm25": float(score),
            "field_weights": dict(FTS_FIELD_WEIGHTS),
            "rank": rank,
        }

    cache = _asset_cache if _asset_cache is not None else {}
    source_artifact = _asset_metadata(conn, unit.canonical_id, cache)

    return RetrievalHit(
        rank=rank,
        knowledge_unit_id=unit.knowledge_unit_id,
        canonical_id=unit.canonical_id,
        unit_type=unit.unit_type.value,
        statement=unit.statement,
        verification_status=unit.verification_status.value,
        extraction_confidence=unit.extraction_confidence,
        entities=list(unit.entities),
        topics=list(unit.topics),
        attribution=unit.attribution,
        evidence_refs=list(unit.evidence_refs),
        extraction_lineage=unit.extraction_lineage,
        source_artifact=source_artifact,
        match_info={
            "matched_on": matched_fields,
            "matched_terms": matched_terms,
        },
        ranking_diagnostics=ranking_diagnostics,
    )