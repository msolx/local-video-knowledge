"""M5-03/M5-04 Evidence-Grounded Retrieval API.

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
  never negated into a fake probability.
- Short-query fallback (terms < 3 chars, a documented trigram limitation):
  deterministic literal substring scan over ``knowledge_fts_content`` using
  parameterized ``instr()`` (never LIKE wildcards, never a user-controlled
  pattern). Weighted short score (statement 5 / entity_names 2 / topics 2 /
  evidence_excerpts 1) — higher is better.
- Determinism: ties are broken by ``unit_rowid`` ASC. No retrieval cache.

M5-04 additions (ranking policy ``lexical-ranking-v1``, see
M5_DECISIONS.md Decision 30-36):

- ``QueryPlan`` describes how a query is parsed and which retrieval path runs,
  deterministically and JSON-safe. It never exposes SQL as a contract.
- ``RetrievalResult.diagnostics`` is a stable, JSON-safe structure:
  normalized/long/short terms, retrieval path, applied filters, candidate
  count before LIMIT, result count, top_k, fallback flag, ranking policy
  version and documented limitations.
- A conservative lexicographic ranking policy. The FTS paths place
  field-priority signals before ``raw_bm25`` (a hit that matches
  statement/entity/topic outranks an evidence-only hit; raw_bm25 then decides
  within the same field profile; ``unit_rowid`` is the final tie-break). The
  short path keeps ``weighted_substring_score`` primary (higher is better)
  and uses exact-phrase + term-coverage as deterministic tie-breaks.
- Field-match signals (``statement_match`` / ``entity_match`` /
  ``topic_match`` / ``evidence_match``), exact-phrase flags,
  ``matched_term_count`` / ``total_term_count`` / ``term_coverage`` and
  ``evidence_only_match`` are recorded per hit in ``match_info``.
- Retrieval invariant: under AND semantics every accepted hit must match every
  required term (``term_coverage == 1.0``). A violation raises
  ``RetrievalInvariantError``.
- No learned reranker, no LLM scoring, no embedding similarity, no semantic
  similarity, no ``extraction_confidence``/``verification_status`` ranking
  boost.
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

# M5-04: compact, contract-level retrieval-path names used by QueryPlan and
# result diagnostics. Mapped 1:1 to the sealed M5-03 method names.
RETRIEVAL_PATH_FTS = "fts_trigram"
RETRIEVAL_PATH_SHORT = "substring_short"
RETRIEVAL_PATH_MIXED = "fts_trigram_with_short_filter"

_METHOD_TO_PATH = {
    RETRIEVAL_METHOD_FTS: RETRIEVAL_PATH_FTS,
    RETRIEVAL_METHOD_SHORT: RETRIEVAL_PATH_SHORT,
    RETRIEVAL_METHOD_MIXED: RETRIEVAL_PATH_MIXED,
}

# Frozen deterministic ranking policy version. Any future change to the
# ranking tuple must bump this and be recorded in M5_DECISIONS.md.
RANKING_POLICY_VERSION = "lexical-ranking-v1"

# Deterministic short-query field weights (mirrors FTS_FIELD_WEIGHTS).
_SHORT_FIELDS = ("statement", "entity_names", "topics", "evidence_excerpts")


class RetrievalInvariantError(RuntimeError):
    """Raised when an accepted hit violates the term-coverage invariant."""


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
# QueryPlan
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class QueryPlan:
    """Deterministic, JSON-safe description of how a query is executed.

    M5-04 read-only plan. It answers: how was the query parsed, which
    retrieval path runs, which filters apply and with what ``top_k``. It is a
    diagnostic/planning record only — never executable SQL.
    """

    original_query: str
    normalized_query: str
    terms: list[str]
    long_terms: list[str]
    short_terms: list[str]
    retrieval_path: str
    filters: dict[str, list[str]]
    top_k: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "normalized_query": self.normalized_query,
            "terms": list(self.terms),
            "long_terms": list(self.long_terms),
            "short_terms": list(self.short_terms),
            "retrieval_path": self.retrieval_path,
            "filters": {k: list(v) for k, v in self.filters.items()},
            "top_k": self.top_k,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QueryPlan:
        if not isinstance(d, dict):
            raise TypeError("QueryPlan payload must be a dict")
        return cls(
            original_query=str(d["original_query"]),
            normalized_query=str(d["normalized_query"]),
            terms=[str(t) for t in d["terms"]],
            long_terms=[str(t) for t in d["long_terms"]],
            short_terms=[str(t) for t in d["short_terms"]],
            retrieval_path=str(d["retrieval_path"]),
            filters={str(k): [str(v) for v in val] for k, val in d.get("filters", {}).items()},
            top_k=int(d["top_k"]),
        )


_FILTER_NAMES = ("canonical_ids", "unit_types", "verification_statuses", "topics", "entity_names")


def build_query_plan(query: RetrievalQuery) -> QueryPlan:
    """Build the deterministic plan for a validated query.

    Long terms are those with >= 3 codepoints (FTS trigram); short terms have
    1-2 codepoints (deterministic substring fallback). Retrieval path follows
    the M5-03 planner: only-long -> FTS, only-short -> substring, both ->
    mixed. Filters are recorded exactly as provided by the caller (no
    auto-inferred filters are ever added).
    """
    normalized = normalize_retrieval_query(query.query_text)
    terms = plan_literal_terms(normalized)
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) <= 2]
    if long_terms and not short_terms:
        path = RETRIEVAL_PATH_FTS
    elif short_terms and not long_terms:
        path = RETRIEVAL_PATH_SHORT
    else:
        path = RETRIEVAL_PATH_MIXED
    filters = {name: list(getattr(query, name)) for name in _FILTER_NAMES if getattr(query, name)}
    return QueryPlan(
        original_query=query.query_text,
        normalized_query=normalized,
        terms=terms,
        long_terms=long_terms,
        short_terms=short_terms,
        retrieval_path=path,
        filters=filters,
        top_k=query.top_k,
    )


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


def _applied_filters(query: RetrievalQuery) -> dict[str, list[str]]:
    """Deterministic record of the filters actually applied (never inferred)."""
    return {name: list(getattr(query, name)) for name in _FILTER_NAMES if getattr(query, name)}


# ----------------------------------------------------------------------
# Query planner -> SQL candidate queries (no LIMIT; ranking in Python)
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


_CANDIDATE_COLS = (
    "k.unit_rowid AS unit_rowid",
    "c.statement AS statement",
    "c.entity_names AS entity_names",
    "c.topics AS topics",
    "c.evidence_excerpts AS evidence_excerpts",
)


def _fts_candidates(
    conn: sqlite3.Connection,
    long_terms: list[str],
    query: RetrievalQuery,
) -> list[dict[str, Any]]:
    """All candidates on the all-long path: trigram FTS with literal AND and
    structured filters, NO LIMIT. Returns one dict per row with the content
    projection fields and the raw bm25 score."""
    field_weights = [FTS_FIELD_WEIGHTS[col] for col in FTS_COLUMNS]
    params: list[Any] = list(field_weights)
    match_sql = _fts_match_sql(long_terms)
    params.append(match_sql)
    filter_sql = _build_filter_sql(query, params)
    sql = f"""
        SELECT {", ".join(_CANDIDATE_COLS)},
               bm25({FTS_INDEX_TABLE}, ?, ?, ?, ?) AS bm25_score
        FROM {FTS_INDEX_TABLE}
        JOIN knowledge_units k ON k.unit_rowid = {FTS_INDEX_TABLE}.rowid
        JOIN {FTS_CONTENT_TABLE} c ON c.unit_rowid = k.unit_rowid
        WHERE {FTS_INDEX_TABLE} MATCH ?{filter_sql}
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _short_candidates(
    conn: sqlite3.Connection,
    short_terms: list[str],
    query: RetrievalQuery,
) -> list[dict[str, Any]]:
    """All candidates on the all-short path: deterministic literal substring
    over the materialized FTS content table, NO LIMIT."""
    score_params: list[Any] = []
    condition_params: list[Any] = []
    score_sql = _short_score_sql(short_terms, score_params)
    condition_sql = _short_condition_sql(short_terms, condition_params)
    params: list[Any] = score_params + condition_params
    filter_sql = _build_filter_sql(query, params)
    sql = f"""
        SELECT {", ".join(_CANDIDATE_COLS)},
               ({score_sql}) AS short_score
        FROM {FTS_CONTENT_TABLE} c
        JOIN knowledge_units k ON k.unit_rowid = c.unit_rowid
        WHERE {condition_sql}{filter_sql}
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _mixed_candidates(
    conn: sqlite3.Connection,
    long_terms: list[str],
    short_terms: list[str],
    query: RetrievalQuery,
) -> list[dict[str, Any]]:
    """All candidates on the mixed path: FTS constrains the long terms and
    structured substring conditions require every short term to match content.
    AND semantics, NO LIMIT."""
    field_weights = [FTS_FIELD_WEIGHTS[col] for col in FTS_COLUMNS]
    params: list[Any] = list(field_weights)
    match_sql = _fts_match_sql(long_terms)
    params.append(match_sql)
    condition_params: list[Any] = []
    condition_sql = _short_condition_sql(short_terms, condition_params)
    params.extend(condition_params)
    filter_sql = _build_filter_sql(query, params)
    sql = f"""
        SELECT {", ".join(_CANDIDATE_COLS)},
               bm25({FTS_INDEX_TABLE}, ?, ?, ?, ?) AS bm25_score
        FROM {FTS_INDEX_TABLE}
        JOIN knowledge_units k ON k.unit_rowid = {FTS_INDEX_TABLE}.rowid
        JOIN {FTS_CONTENT_TABLE} c ON c.unit_rowid = k.unit_rowid
        WHERE {FTS_INDEX_TABLE} MATCH ?
          AND {condition_sql}{filter_sql}
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------------
# Field-match signals (deterministic literal field-content explanation)
# ----------------------------------------------------------------------


def _compute_field_match_stats(
    terms: list[str],
    normalized_query: str,
    content_row: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic literal field-match stats for a candidate.

    Returns (no model, no fuzzy):

    - ``matched_on`` / ``matched_terms`` (M5-03 compatible)
    - per-field boolean flags ``statement_match`` / ``entity_match`` /
      ``topic_match`` / ``evidence_match``
    - exact-phrase flags for every content field (full normalized query as a
      contiguous literal substring, ASCII case-insensitive via ``lower()``)
    - ``matched_term_count`` / ``total_term_count`` / ``term_coverage``
    - ``evidence_only_match`` (no statement/entity/topic match, but evidence)
    """
    values = {col: str(content_row.get(col) or "") for col in _SHORT_FIELDS}
    lower_values = {col: values[col].lower() for col in _SHORT_FIELDS}
    lower_terms = [term.lower() for term in terms]

    matched_terms = [
        term for term, lterm in zip(terms, lower_terms)
        if any(lterm in lower_values[col] for col in _SHORT_FIELDS)
    ]
    matched_fields = [
        col for col in _SHORT_FIELDS
        if any(lterm in lower_values[col] for lterm in lower_terms)
    ]

    field_flags = {
        "statement_match": "statement" in matched_fields,
        "entity_match": "entity_names" in matched_fields,
        "topic_match": "topics" in matched_fields,
        "evidence_match": "evidence_excerpts" in matched_fields,
    }

    query_lower = normalized_query.lower()
    exact_flags = {
        "exact_statement_phrase": bool(query_lower) and query_lower in lower_values["statement"],
        "exact_entity_phrase": bool(query_lower) and query_lower in lower_values["entity_names"],
        "exact_topic_phrase": bool(query_lower) and query_lower in lower_values["topics"],
        "exact_evidence_phrase": bool(query_lower) and query_lower in lower_values["evidence_excerpts"],
    }

    total_term_count = len(terms)
    matched_term_count = len(matched_terms)
    term_coverage = (matched_term_count / total_term_count) if total_term_count else 1.0

    evidence_only_match = bool(
        field_flags["evidence_match"]
        and not (field_flags["statement_match"] or field_flags["entity_match"] or field_flags["topic_match"])
    )

    return {
        "matched_on": matched_fields,
        "matched_terms": matched_terms,
        **field_flags,
        **exact_flags,
        "matched_term_count": matched_term_count,
        "total_term_count": total_term_count,
        "term_coverage": term_coverage,
        "evidence_only_match": evidence_only_match,
    }


# ----------------------------------------------------------------------
# Deterministic ranking policy (lexical-ranking-v1)
# ----------------------------------------------------------------------


def _fts_ranking_key(stats: dict[str, Any], raw_bm25: float, unit_rowid: int) -> tuple:
    """Lexicographic key for the FTS / mixed paths (ascending sort).

    Order (explainable, conservative):
      1. evidence-only tier: hits matching statement/entity/topic rank before
         evidence-only hits (an evidence-only accidental match never outranks
         a real field match).
      2. exact statement phrase
      3. statement_match
      4. entity_match
      5. topic_match
      6. term_coverage (>= 1.0 invariant; present for completeness)
      7. raw_bm25 (LOWER IS BETTER — BM25 semantics preserved)
      8. unit_rowid (stable final tie-break)
    """
    evidence_tier = 1 if stats["evidence_only_match"] else 0
    return (
        evidence_tier,
        -1 if stats["exact_statement_phrase"] else 0,
        -1 if stats["statement_match"] else 0,
        -1 if stats["entity_match"] else 0,
        -1 if stats["topic_match"] else 0,
        -1.0 * stats["term_coverage"],
        float(raw_bm25),
        int(unit_rowid),
    )


def _short_ranking_key(stats: dict[str, Any], short_score: float, unit_rowid: int) -> tuple:
    """Lexicographic key for the short path (ascending sort).

    The weighted substring score stays PRIMARY (higher is better — statement
    5 / entity 2 / topic 2 / evidence 1 already encode field priority); exact
    phrase and term coverage are deterministic tie-breaks; unit_rowid is the
    stable final tie-break.
    """
    return (
        -1.0 * float(short_score),
        -1 if stats["exact_statement_phrase"] else 0,
        -1.0 * stats["term_coverage"],
        int(unit_rowid),
    )


def _ranking_components(method: str, stats: dict[str, Any], score: float, unit_rowid: int) -> dict[str, Any]:
    """JSON-safe breakdown of the ranking key for diagnostics."""
    if method == RETRIEVAL_METHOD_SHORT:
        return {
            "weighted_substring_score": float(score),
            "exact_statement_phrase": bool(stats["exact_statement_phrase"]),
            "term_coverage": float(stats["term_coverage"]),
            "unit_rowid": int(unit_rowid),
        }
    return {
        "evidence_only_tier": 1 if stats["evidence_only_match"] else 0,
        "exact_statement_phrase": bool(stats["exact_statement_phrase"]),
        "statement_match": bool(stats["statement_match"]),
        "entity_match": bool(stats["entity_match"]),
        "topic_match": bool(stats["topic_match"]),
        "term_coverage": float(stats["term_coverage"]),
        "raw_bm25": float(score),
        "unit_rowid": int(unit_rowid),
    }


def _why_this_hit(stats: dict[str, Any]) -> str:
    """Deterministic, templated human-readable explanation (never an LLM)."""
    parts = []
    for field, key in (
        ("statement", "statement_match"),
        ("entities", "entity_match"),
        ("topics", "topic_match"),
        ("evidence", "evidence_match"),
    ):
        if stats[key]:
            parts.append(field)
    if not parts:
        return "no matched field (invariant violation)"
    return "matched query terms in " + " and ".join(parts)


def check_retrieval_invariant(result: RetrievalResult) -> list[str]:
    """Return the list of term-coverage invariant violations in a result.

    Under AND semantics every accepted hit must match every required term
    (``term_coverage == 1.0``). An empty list means the invariant holds.
    """
    violations: list[str] = []
    for hit in result.hits:
        coverage = hit.match_info.get("term_coverage")
        if coverage != 1.0:
            violations.append(
                f"hit {hit.rank} ({hit.knowledge_unit_id}): term_coverage={coverage!r} "
                f"(matched={hit.match_info.get('matched_terms')!r}, "
                f"total={hit.match_info.get('total_term_count')!r})"
            )
    return violations


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------


def _build_limitations(method: str) -> list[str]:
    """Documented deterministic limitations for the chosen path."""
    if method == RETRIEVAL_METHOD_SHORT:
        return [
            "trigram cannot match terms shorter than 3 characters; "
            "deterministic literal substring fallback used"
        ]
    if method == RETRIEVAL_METHOD_MIXED:
        return [
            "short terms constrained via deterministic literal substring "
            "(trigram <3-character limitation)"
        ]
    return []


def retrieve(db_path: Path | str, query: RetrievalQuery | dict[str, Any]) -> RetrievalResult:
    """Stable public retrieval entrypoint.

    ``query`` may be a ``RetrievalQuery`` or a JSON-safe dict (auto-coerced via
    ``RetrievalQuery.from_dict``). Opens the store read-only for the duration
    of the call; callers never touch the sqlite Connection directly.

    M5-04: the query plan is deterministic; candidate SQL runs WITHOUT LIMIT,
    ranking applies the ``lexical-ranking-v1`` lexicographic policy in Python,
    then ``top_k`` is sliced. ``candidate_count_before_limit`` reports the
    filtered candidate count; ``result_count <= top_k``.
    """
    if isinstance(query, dict):
        query = RetrievalQuery.from_dict(query)
    if not isinstance(query, RetrievalQuery):
        raise TypeError("query must be a RetrievalQuery (or a dict thereof)")

    plan = build_query_plan(query)
    method = {
        RETRIEVAL_PATH_FTS: RETRIEVAL_METHOD_FTS,
        RETRIEVAL_PATH_SHORT: RETRIEVAL_METHOD_SHORT,
        RETRIEVAL_PATH_MIXED: RETRIEVAL_METHOD_MIXED,
    }[plan.retrieval_path]

    conn = open_store(Path(db_path))
    try:
        revision = compute_store_revision(Path(db_path), conn)
        if method == RETRIEVAL_METHOD_FTS:
            candidates = _fts_candidates(conn, plan.long_terms, query)
        elif method == RETRIEVAL_METHOD_SHORT:
            candidates = _short_candidates(conn, plan.short_terms, query)
        else:
            candidates = _mixed_candidates(conn, plan.long_terms, plan.short_terms, query)

        candidate_count = len(candidates)

        ranked: list[tuple[tuple, dict[str, Any], dict[str, Any]]] = []
        for cand in candidates:
            unit_rowid = int(cand["unit_rowid"])
            stats = _compute_field_match_stats(plan.terms, plan.normalized_query, cand)
            if method == RETRIEVAL_METHOD_SHORT:
                key = _short_ranking_key(stats, float(cand["short_score"]), unit_rowid)
            else:
                key = _fts_ranking_key(stats, float(cand["bm25_score"]), unit_rowid)
            ranked.append((key, cand, stats))
        ranked.sort(key=lambda item: item[0])

        top = ranked[: query.top_k]

        asset_cache: dict[str, dict[str, Any]] = {}
        hits: list[RetrievalHit] = []
        for rank, (key, cand, stats) in enumerate(top, start=1):
            unit_rowid = int(cand["unit_rowid"])
            if method == RETRIEVAL_METHOD_SHORT:
                score = float(cand["short_score"])
                ranking_diagnostics = {
                    "method": method,
                    "ranking_policy_version": RANKING_POLICY_VERSION,
                    "rank": rank,
                    "weighted_substring_score": score,
                    "score_direction": "higher_is_better",
                    "matched_fields": list(stats["matched_on"]),
                    "ranking_components": _ranking_components(method, stats, score, unit_rowid),
                    "why_this_hit": _why_this_hit(stats),
                }
            else:
                score = float(cand["bm25_score"])
                ranking_diagnostics = {
                    "method": method,
                    "ranking_policy_version": RANKING_POLICY_VERSION,
                    "rank": rank,
                    "raw_bm25": score,
                    "bm25_direction": "lower_is_better",
                    "field_weights": dict(FTS_FIELD_WEIGHTS),
                    "ranking_components": _ranking_components(method, stats, score, unit_rowid),
                    "why_this_hit": _why_this_hit(stats),
                }
                if method == RETRIEVAL_METHOD_MIXED:
                    ranking_diagnostics["long_terms"] = list(plan.long_terms)
                    ranking_diagnostics["short_terms"] = list(plan.short_terms)
                    ranking_diagnostics["short_term_matches"] = [
                        term for term in plan.short_terms if term.lower() in {
                            str(cand.get(col) or "").lower() for col in _SHORT_FIELDS
                        }
                    ]
            hits.append(
                _build_hit(
                    conn,
                    rank,
                    unit_rowid,
                    query,
                    method,
                    plan.long_terms,
                    plan.short_terms,
                    stats,
                    ranking_diagnostics,
                    asset_cache,
                )
            )

        result = RetrievalResult(
            query=query,
            retrieval_method=method,
            store_schema_version=STORE_SCHEMA_VERSION,
            store_revision=revision,
            result_count=len(hits),
            hits=hits,
            diagnostics={},
        )
        invariant_violations = check_retrieval_invariant(result)
        if invariant_violations:
            raise RetrievalInvariantError(
                "accepted hit with term_coverage < 1.0 (required term missing); "
                f"violations={invariant_violations}"
            )

        diagnostics: dict[str, Any] = {
            "query_plan": plan.to_dict(),
            "normalized_query": plan.normalized_query,
            "terms": list(plan.terms),
            "long_terms": list(plan.long_terms),
            "short_terms": list(plan.short_terms),
            "retrieval_path": plan.retrieval_path,
            "filters_applied": _applied_filters(query),
            "candidate_count_before_limit": candidate_count,
            "result_count": len(hits),
            "top_k": query.top_k,
            "short_query_fallback": bool(plan.short_terms),
            "ranking_policy_version": RANKING_POLICY_VERSION,
            "limitations": _build_limitations(method),
        }

        return RetrievalResult(
            query=query,
            retrieval_method=method,
            store_schema_version=STORE_SCHEMA_VERSION,
            store_revision=revision,
            result_count=len(hits),
            hits=hits,
            diagnostics=diagnostics,
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Hit hydration (canonical payload -> full provenance)
# ----------------------------------------------------------------------


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
    query: RetrievalQuery,
    method: str,
    long_terms: list[str],
    short_terms: list[str],
    stats: dict[str, Any],
    ranking_diagnostics: dict[str, Any],
    _asset_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> RetrievalHit:
    row = conn.execute(
        "SELECT canonical_payload_json FROM knowledge_units WHERE unit_rowid = ?",
        (unit_rowid,),
    ).fetchone()
    payload = json.loads(row["canonical_payload_json"])
    unit = CanonicalKnowledgeUnit.from_dict(payload)

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
            "matched_on": list(stats["matched_on"]),
            "matched_terms": list(stats["matched_terms"]),
            "term_coverage": float(stats["term_coverage"]),
            "matched_term_count": int(stats["matched_term_count"]),
            "total_term_count": int(stats["total_term_count"]),
            "statement_match": bool(stats["statement_match"]),
            "entity_match": bool(stats["entity_match"]),
            "topic_match": bool(stats["topic_match"]),
            "evidence_match": bool(stats["evidence_match"]),
            "exact_statement_phrase": bool(stats["exact_statement_phrase"]),
            "exact_entity_phrase": bool(stats["exact_entity_phrase"]),
            "exact_topic_phrase": bool(stats["exact_topic_phrase"]),
            "exact_evidence_phrase": bool(stats["exact_evidence_phrase"]),
            "evidence_only_match": bool(stats["evidence_only_match"]),
        },
        ranking_diagnostics=ranking_diagnostics,
    )