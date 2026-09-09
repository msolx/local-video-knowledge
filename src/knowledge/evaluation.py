"""M5-05 Retrieval Evaluation Harness (pure deterministic, fully offline).

Consumes the sealed M5-03/M5-04 retrieval API (``retrieve`` over an M5-01/M5-02
store) and a tracked golden-query fixture to answer: did the query find the
knowledge it should, in a reasonable position, with accurate filters and
complete provenance/evidence, on the expected planner path, deterministically?

This module never mutates the store, never calls an LLM, never touches the
network, and never rewrites the retrieval heuristics.  If a golden evaluation
exposes a genuine retrieval bug, this module reports it -- it does not patch
``retrieval.py``.

Relevance semantics (see evaluation/m5/c10_golden_queries.json):
- ``exhaustive``: the whole corpus was reviewed; ``expected_relevant_ku_ids``
  is the complete relevant set, so Precision@K / Recall@K / F1@K are valid.
- ``partial``: only clearly-required units are marked; only Hit@K, MRR and
  required-hit success are valid.  Precision/Recall are never reported for a
  partial query.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.knowledge.retrieval import (
    MAX_TOP_K,
    MIN_TOP_K,
    RETRIEVAL_PATH_FTS,
    RETRIEVAL_PATH_MIXED,
    RETRIEVAL_PATH_SHORT,
    RetrievalQuery,
    retrieve,
)
from src.knowledge.store import compute_store_revision

EVALUATION_POLICY_VERSION = "m5-evaluation-policy-v1"
DEFAULT_GOLDEN_PATH = Path("evaluation/m5/c10_golden_queries.json")

_ALLOWED_FILTER_KEYS = ("canonical_ids", "unit_types", "verification_statuses", "topics", "entity_names")
_ALLOWED_PATHS = (RETRIEVAL_PATH_FTS, RETRIEVAL_PATH_SHORT, RETRIEVAL_PATH_MIXED)
_ALLOWED_JUDGMENTS = ("exhaustive", "partial")
_KU_ID_RE = re.compile(r"^ku_[a-f0-9]{16}$")


# ----------------------------------------------------------------------
# Golden fixture model
# ----------------------------------------------------------------------


def _sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_asset_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compute_corpus_fingerprint(asset_paths: dict[str, Path | str]) -> str:
    """Deterministic corpus fingerprint: sha256 of canonical
    [{canonical_id, sha256}] sorted by canonical_id."""
    entries = [
        {"canonical_id": cid, "sha256": compute_asset_sha256(asset_paths[cid])}
        for cid in sorted(asset_paths)
    ]
    return _sha256_json(entries)


@dataclass(frozen=True)
class GoldenQuery:
    """A single golden query with explicit relevance judgment."""

    query_id: str
    query_text: str
    top_k: int
    filters: dict[str, list[str]]
    expected_retrieval_path: str
    relevance_judgment: str
    expected_relevant_ku_ids: list[str]
    required_ku_ids: list[str] = field(default_factory=list)
    forbidden_ku_ids: list[str] = field(default_factory=list)
    expected_canonical_ids: list[str] = field(default_factory=list)
    forbidden_canonical_ids: list[str] = field(default_factory=list)
    max_first_relevant_rank: Optional[int] = None
    zero_result_expected: bool = False
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.query_id.strip():
            raise ValueError("golden query_id must be non-empty")
        if not isinstance(self.query_text, str) or not self.query_text.strip():
            raise ValueError(f"{self.query_id}: query_text must be a non-empty string")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise ValueError(f"{self.query_id}: top_k must be an integer")
        if not (MIN_TOP_K <= self.top_k <= MAX_TOP_K):
            raise ValueError(f"{self.query_id}: top_k must be in [{MIN_TOP_K}, {MAX_TOP_K}]")
        for key in self.filters:
            if key not in _ALLOWED_FILTER_KEYS:
                raise ValueError(f"{self.query_id}: unknown filter key '{key}'")
        if self.expected_retrieval_path not in _ALLOWED_PATHS:
            raise ValueError(
                f"{self.query_id}: expected_retrieval_path must be one of {_ALLOWED_PATHS}"
            )
        if self.relevance_judgment not in _ALLOWED_JUDGMENTS:
            raise ValueError(
                f"{self.query_id}: relevance_judgment must be one of {_ALLOWED_JUDGMENTS}"
            )
        if self.zero_result_expected:
            if self.expected_relevant_ku_ids:
                raise ValueError(
                    f"{self.query_id}: zero_result_expected requires an empty expected_relevant_ku_ids"
                )
            if self.required_ku_ids:
                raise ValueError(f"{self.query_id}: zero_result_expected requires empty required_ku_ids")
            if self.max_first_relevant_rank is not None:
                raise ValueError(
                    f"{self.query_id}: zero_result_expected forbids max_first_relevant_rank"
                )
        if self.max_first_relevant_rank is not None:
            if isinstance(self.max_first_relevant_rank, bool) or not isinstance(self.max_first_relevant_rank, int):
                raise ValueError(f"{self.query_id}: max_first_relevant_rank must be an int or null")
            if self.max_first_relevant_rank < 1:
                raise ValueError(f"{self.query_id}: max_first_relevant_rank must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query_text": self.query_text,
            "top_k": self.top_k,
            "filters": {k: list(v) for k, v in sorted(self.filters.items())},
            "expected_retrieval_path": self.expected_retrieval_path,
            "relevance_judgment": self.relevance_judgment,
            "expected_relevant_ku_ids": list(self.expected_relevant_ku_ids),
            "required_ku_ids": list(self.required_ku_ids),
            "forbidden_ku_ids": list(self.forbidden_ku_ids),
            "expected_canonical_ids": list(self.expected_canonical_ids),
            "forbidden_canonical_ids": list(self.forbidden_canonical_ids),
            "max_first_relevant_rank": self.max_first_relevant_rank,
            "zero_result_expected": self.zero_result_expected,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldenQuery:
        if not isinstance(d, dict):
            raise TypeError("GoldenQuery payload must be a dict")
        raw_filters = d.get("filters") or {}
        filters = {
            key: [str(v) for v in (raw_filters.get(key) or [])]
            for key in _ALLOWED_FILTER_KEYS
            if raw_filters.get(key)
        }
        return cls(
            query_id=str(d["query_id"]),
            query_text=str(d["query_text"]),
            top_k=int(d["top_k"]),
            filters=filters,
            expected_retrieval_path=str(d["expected_retrieval_path"]),
            relevance_judgment=str(d["relevance_judgment"]),
            expected_relevant_ku_ids=[str(v) for v in d.get("expected_relevant_ku_ids", [])],
            required_ku_ids=[str(v) for v in d.get("required_ku_ids", [])],
            forbidden_ku_ids=[str(v) for v in d.get("forbidden_ku_ids", [])],
            expected_canonical_ids=[str(v) for v in d.get("expected_canonical_ids", [])],
            forbidden_canonical_ids=[str(v) for v in d.get("forbidden_canonical_ids", [])],
            max_first_relevant_rank=(
                int(d["max_first_relevant_rank"]) if d.get("max_first_relevant_rank") is not None else None
            ),
            zero_result_expected=bool(d.get("zero_result_expected", False)),
            notes=d.get("notes"),
        )


@dataclass(frozen=True)
class GoldenSuite:
    """Top-level golden fixture with corpus binding."""

    suite_version: str
    evaluation_policy_version: str
    corpus_version: str
    corpus_fingerprint: str
    corpus_assets: list[dict[str, Any]]
    store_config: dict[str, str]
    relevance_semantics: dict[str, str]
    queries: list[GoldenQuery]

    def __post_init__(self) -> None:
        if not self.suite_version.strip():
            raise ValueError("suite_version must be non-empty")
        if not self.corpus_fingerprint.strip():
            raise ValueError("corpus_fingerprint must be non-empty")
        ids = [q.query_id for q in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("golden query_id values must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_version": self.suite_version,
            "evaluation_policy_version": self.evaluation_policy_version,
            "corpus_version": self.corpus_version,
            "corpus_fingerprint": self.corpus_fingerprint,
            "corpus_assets": [dict(a) for a in self.corpus_assets],
            "store_config": dict(self.store_config),
            "relevance_semantics": dict(self.relevance_semantics),
            "queries": [q.to_dict() for q in self.queries],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldenSuite:
        if not isinstance(d, dict):
            raise TypeError("GoldenSuite payload must be a dict")
        return cls(
            suite_version=str(d["suite_version"]),
            evaluation_policy_version=str(d["evaluation_policy_version"]),
            corpus_version=str(d.get("corpus_version", "")),
            corpus_fingerprint=str(d["corpus_fingerprint"]),
            corpus_assets=[dict(a) for a in d.get("corpus_assets", [])],
            store_config={str(k): str(v) for k, v in (d.get("store_config") or {}).items()},
            relevance_semantics={str(k): str(v) for k, v in (d.get("relevance_semantics") or {}).items()},
            queries=[GoldenQuery.from_dict(q) for q in d.get("queries", [])],
        )


def load_golden_suite(path: Path | str) -> GoldenSuite:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"golden fixture not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"golden fixture is not valid JSON: {p}: {exc}") from exc
    return GoldenSuite.from_dict(raw)


def load_golden_queries(path: Path | str) -> list[GoldenQuery]:
    return list(load_golden_suite(path).queries)


# ----------------------------------------------------------------------
# Per-query evaluation
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class QueryEvaluation:
    """Result of evaluating one golden query."""

    query_id: str
    passed: bool
    hit_at_k: bool
    mrr: float
    first_relevant_rank: Optional[int]
    precision_at_k: Optional[float]
    recall_at_k: Optional[float]
    f1_at_k: Optional[float]
    filter_correct: bool
    retrieval_path_correct: bool
    evidence_complete: bool
    provenance_complete: bool
    term_coverage_valid: bool
    required_hits_found: list[str]
    required_hits_missing: list[str]
    forbidden_hits_found: list[str]
    expected_canonical_ok: bool
    forbidden_canonical_hits: list[str]
    zero_result_ok: bool
    actual_top_k_ids: list[str]
    actual_retrieval_path: str
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "passed": self.passed,
            "hit_at_k": self.hit_at_k,
            "mrr": self.mrr,
            "first_relevant_rank": self.first_relevant_rank,
            "precision_at_k": self.precision_at_k,
            "recall_at_k": self.recall_at_k,
            "f1_at_k": self.f1_at_k,
            "filter_correct": self.filter_correct,
            "retrieval_path_correct": self.retrieval_path_correct,
            "evidence_complete": self.evidence_complete,
            "provenance_complete": self.provenance_complete,
            "term_coverage_valid": self.term_coverage_valid,
            "required_hits_found": list(self.required_hits_found),
            "required_hits_missing": list(self.required_hits_missing),
            "forbidden_hits_found": list(self.forbidden_hits_found),
            "expected_canonical_ok": self.expected_canonical_ok,
            "forbidden_canonical_hits": list(self.forbidden_canonical_hits),
            "zero_result_ok": self.zero_result_ok,
            "actual_top_k_ids": list(self.actual_top_k_ids),
            "actual_retrieval_path": self.actual_retrieval_path,
            "failure_reasons": list(self.failure_reasons),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QueryEvaluation:
        return cls(
            query_id=str(d["query_id"]),
            passed=bool(d["passed"]),
            hit_at_k=bool(d["hit_at_k"]),
            mrr=float(d["mrr"]),
            first_relevant_rank=(
                int(d["first_relevant_rank"]) if d.get("first_relevant_rank") is not None else None
            ),
            precision_at_k=float(d["precision_at_k"]) if d.get("precision_at_k") is not None else None,
            recall_at_k=float(d["recall_at_k"]) if d.get("recall_at_k") is not None else None,
            f1_at_k=float(d["f1_at_k"]) if d.get("f1_at_k") is not None else None,
            filter_correct=bool(d["filter_correct"]),
            retrieval_path_correct=bool(d["retrieval_path_correct"]),
            evidence_complete=bool(d["evidence_complete"]),
            provenance_complete=bool(d["provenance_complete"]),
            term_coverage_valid=bool(d["term_coverage_valid"]),
            required_hits_found=[str(v) for v in d.get("required_hits_found", [])],
            required_hits_missing=[str(v) for v in d.get("required_hits_missing", [])],
            forbidden_hits_found=[str(v) for v in d.get("forbidden_hits_found", [])],
            expected_canonical_ok=bool(d["expected_canonical_ok"]),
            forbidden_canonical_hits=[str(v) for v in d.get("forbidden_canonical_hits", [])],
            zero_result_ok=bool(d["zero_result_ok"]),
            actual_top_k_ids=[str(v) for v in d.get("actual_top_k_ids", [])],
            actual_retrieval_path=str(d["actual_retrieval_path"]),
            failure_reasons=[str(v) for v in d.get("failure_reasons", [])],
        )


def _golden_to_query(golden: GoldenQuery) -> RetrievalQuery:
    filters = golden.filters or {}
    return RetrievalQuery(
        query_text=golden.query_text,
        top_k=golden.top_k,
        canonical_ids=[str(v) for v in filters["canonical_ids"]] if filters.get("canonical_ids") else None,
        unit_types=[str(v) for v in filters["unit_types"]] if filters.get("unit_types") else None,
        verification_statuses=[str(v) for v in filters["verification_statuses"]]
        if filters.get("verification_statuses")
        else None,
        topics=[str(v) for v in filters["topics"]] if filters.get("topics") else None,
        entity_names=[str(v) for v in filters["entity_names"]] if filters.get("entity_names") else None,
    )


def _hit_evidence_complete(hit: Any) -> bool:
    refs = getattr(hit, "evidence_refs", None) or []
    if not refs:
        return False
    for ref in refs:
        if not getattr(ref, "evidence_id", "") or not getattr(ref, "source_excerpt", ""):
            return False
        if getattr(ref, "temporal_range", None) is None and getattr(ref, "sequence_range", None) is None:
            return False
    return True


def _hit_provenance_complete(hit: Any) -> bool:
    src = dict(getattr(hit, "source_artifact", None) or {})
    if not src.get("path") or not src.get("fingerprint"):
        return False
    ku_id = getattr(hit, "knowledge_unit_id", "")
    if not _KU_ID_RE.match(ku_id):
        return False
    if not getattr(hit, "canonical_id", ""):
        return False
    return True


def _hit_has_forbidden_ku(hit: Any, forbidden: set[str]) -> bool:
    return getattr(hit, "knowledge_unit_id", "") in forbidden


def evaluate_query(db_path: Path | str, golden: GoldenQuery) -> QueryEvaluation:
    """Evaluate a single golden query against a populated store."""
    query = _golden_to_query(golden)
    result = retrieve(db_path, query)
    hits = result.hits
    actual_ids = [h.knowledge_unit_id for h in hits]
    actual_ids_set = set(actual_ids)
    actual_path = str(result.diagnostics.get("retrieval_path", ""))

    relevant = set(golden.expected_relevant_ku_ids)
    required = set(golden.required_ku_ids)
    forbidden = set(golden.forbidden_ku_ids)
    expected_canonical = set(golden.expected_canonical_ids)
    forbidden_canonical = set(golden.forbidden_canonical_ids)

    reasons: list[str] = []

    # --- relevance metrics -------------------------------------------------
    first_relevant_rank: Optional[int] = None
    for rank, ku_id in enumerate(actual_ids, start=1):
        if ku_id in relevant:
            first_relevant_rank = rank
            break
    hit_at_k = first_relevant_rank is not None
    mrr = (1.0 / first_relevant_rank) if first_relevant_rank is not None else 0.0

    precision_at_k: Optional[float] = None
    recall_at_k: Optional[float] = None
    f1_at_k: Optional[float] = None
    if golden.relevance_judgment == "exhaustive" and relevant:
        retrieved_relevant = actual_ids_set & relevant
        precision_at_k = len(retrieved_relevant) / golden.top_k
        recall_at_k = len(retrieved_relevant) / len(relevant)
        if precision_at_k + recall_at_k > 0:
            f1_at_k = 2 * precision_at_k * recall_at_k / (precision_at_k + recall_at_k)
        else:
            f1_at_k = 0.0

    # --- golden requirement checks -----------------------------------------
    required_found = sorted(required & actual_ids_set)
    required_missing = sorted(required - actual_ids_set)
    forbidden_hits = sorted(ku for ku in actual_ids if ku in forbidden)
    forbidden_canonical_hits = sorted(
        {h.canonical_id for h in hits if h.canonical_id in forbidden_canonical}
    )

    expected_canonical_ok = True
    if expected_canonical:
        expected_canonical_ok = len(hits) > 0 and {h.canonical_id for h in hits} <= expected_canonical

    zero_result_ok = (not golden.zero_result_expected) or len(hits) == 0

    if required_missing:
        reasons.append(f"required hits missing: {required_missing}")
    if forbidden_hits:
        reasons.append(f"forbidden hits returned: {forbidden_hits}")
    if forbidden_canonical_hits:
        reasons.append(f"forbidden canonical hits returned: {forbidden_canonical_hits}")
    if not expected_canonical_ok:
        reasons.append("expected canonical set violated")
    if golden.zero_result_expected and len(hits) != 0:
        reasons.append(f"expected zero results but got {len(hits)}")
    if not golden.zero_result_expected and not hit_at_k:
        reasons.append("no expected relevant unit in top_k")

    # --- structured filter correctness -------------------------------------
    filter_correct = True
    if golden.filters:
        expected_assets = set(golden.filters.get("canonical_ids") or [])
        expected_types = set(golden.filters.get("unit_types") or [])
        expected_statuses = set(golden.filters.get("verification_statuses") or [])
        expected_topics = set(golden.filters.get("topics") or [])
        expected_entities = set(golden.filters.get("entity_names") or [])
        for hit in hits:
            if expected_assets and hit.canonical_id not in expected_assets:
                filter_correct = False
                reasons.append(f"hit {hit.knowledge_unit_id} violates canonical_id filter")
                break
            if expected_types and hit.unit_type not in expected_types:
                filter_correct = False
                reasons.append(f"hit {hit.knowledge_unit_id} violates unit_type filter")
                break
            if expected_statuses and hit.verification_status not in expected_statuses:
                filter_correct = False
                reasons.append(f"hit {hit.knowledge_unit_id} violates verification_status filter")
                break
            if expected_topics and not (set(hit.topics) & expected_topics):
                filter_correct = False
                reasons.append(f"hit {hit.knowledge_unit_id} violates topics filter")
                break
            if expected_entities and not {e.entity_name for e in hit.entities} & expected_entities:
                filter_correct = False
                reasons.append(f"hit {hit.knowledge_unit_id} violates entity_names filter")
                break

    # --- retrieval path -----------------------------------------------------
    retrieval_path_correct = actual_path == golden.expected_retrieval_path
    if not retrieval_path_correct:
        reasons.append(
            f"retrieval path mismatch: expected {golden.expected_retrieval_path}, got {actual_path}"
        )

    # --- structural completeness ---------------------------------------------
    evidence_complete = all(_hit_evidence_complete(h) for h in hits)
    provenance_complete = all(_hit_provenance_complete(h) for h in hits)
    term_coverage_valid = all(
        float(h.match_info.get("term_coverage", 0.0)) == 1.0 for h in hits
    )
    if not evidence_complete:
        reasons.append("evidence completeness violation")
    if not provenance_complete:
        reasons.append("provenance completeness violation")
    if not term_coverage_valid:
        reasons.append("term_coverage invariant violation")

    # --- rank bound ----------------------------------------------------------
    if golden.max_first_relevant_rank is not None:
        if first_relevant_rank is None or first_relevant_rank > golden.max_first_relevant_rank:
            reasons.append(
                f"first relevant rank {first_relevant_rank} exceeds "
                f"max_first_relevant_rank={golden.max_first_relevant_rank}"
            )

    passed = not reasons and (len(hits) > 0 or golden.zero_result_expected)

    return QueryEvaluation(
        query_id=golden.query_id,
        passed=passed,
        hit_at_k=hit_at_k,
        mrr=mrr,
        first_relevant_rank=first_relevant_rank,
        precision_at_k=precision_at_k,
        recall_at_k=recall_at_k,
        f1_at_k=f1_at_k,
        filter_correct=filter_correct,
        retrieval_path_correct=retrieval_path_correct,
        evidence_complete=evidence_complete,
        provenance_complete=provenance_complete,
        term_coverage_valid=term_coverage_valid,
        required_hits_found=required_found,
        required_hits_missing=required_missing,
        forbidden_hits_found=forbidden_hits,
        expected_canonical_ok=expected_canonical_ok,
        forbidden_canonical_hits=forbidden_canonical_hits,
        zero_result_ok=zero_result_ok,
        actual_top_k_ids=actual_ids,
        actual_retrieval_path=actual_path,
        failure_reasons=reasons,
    )


# ----------------------------------------------------------------------
# Suite evaluation + aggregates
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationSummary:
    """Aggregate evaluation results over a golden suite."""

    suite_version: str
    evaluation_policy_version: str
    ranking_policy_version: str
    corpus_fingerprint: str
    corpus_fingerprint_ok: bool
    store_revision: str
    query_count: int
    exhaustive_query_count: int
    partial_query_count: int
    passed_query_count: int
    failed_query_count: int
    mean_hit_at_k: float
    mean_mrr: float
    mean_precision_at_k: Optional[float]
    mean_recall_at_k: Optional[float]
    mean_f1_at_k: Optional[float]
    filter_accuracy: Optional[float]
    retrieval_path_accuracy: float
    evidence_completeness_rate: float
    provenance_completeness_rate: float
    term_coverage_valid_rate: float
    per_query: list[dict[str, Any]]
    structural_failures: list[dict[str, Any]]
    generated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_version": self.suite_version,
            "evaluation_policy_version": self.evaluation_policy_version,
            "ranking_policy_version": self.ranking_policy_version,
            "corpus_fingerprint": self.corpus_fingerprint,
            "corpus_fingerprint_ok": self.corpus_fingerprint_ok,
            "store_revision": self.store_revision,
            "query_count": self.query_count,
            "exhaustive_query_count": self.exhaustive_query_count,
            "partial_query_count": self.partial_query_count,
            "passed_query_count": self.passed_query_count,
            "failed_query_count": self.failed_query_count,
            "mean_hit_at_k": self.mean_hit_at_k,
            "mean_mrr": self.mean_mrr,
            "mean_precision_at_k": self.mean_precision_at_k,
            "mean_recall_at_k": self.mean_recall_at_k,
            "mean_f1_at_k": self.mean_f1_at_k,
            "filter_accuracy": self.filter_accuracy,
            "retrieval_path_accuracy": self.retrieval_path_accuracy,
            "evidence_completeness_rate": self.evidence_completeness_rate,
            "provenance_completeness_rate": self.provenance_completeness_rate,
            "term_coverage_valid_rate": self.term_coverage_valid_rate,
            "per_query": [dict(q) for q in self.per_query],
            "structural_failures": [dict(f) for f in self.structural_failures],
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvaluationSummary:
        return cls(
            suite_version=str(d["suite_version"]),
            evaluation_policy_version=str(d["evaluation_policy_version"]),
            ranking_policy_version=str(d["ranking_policy_version"]),
            corpus_fingerprint=str(d["corpus_fingerprint"]),
            corpus_fingerprint_ok=bool(d["corpus_fingerprint_ok"]),
            store_revision=str(d["store_revision"]),
            query_count=int(d["query_count"]),
            exhaustive_query_count=int(d["exhaustive_query_count"]),
            partial_query_count=int(d["partial_query_count"]),
            passed_query_count=int(d["passed_query_count"]),
            failed_query_count=int(d["failed_query_count"]),
            mean_hit_at_k=float(d["mean_hit_at_k"]),
            mean_mrr=float(d["mean_mrr"]),
            mean_precision_at_k=float(d["mean_precision_at_k"]) if d.get("mean_precision_at_k") is not None else None,
            mean_recall_at_k=float(d["mean_recall_at_k"]) if d.get("mean_recall_at_k") is not None else None,
            mean_f1_at_k=float(d["mean_f1_at_k"]) if d.get("mean_f1_at_k") is not None else None,
            filter_accuracy=float(d["filter_accuracy"]) if d.get("filter_accuracy") is not None else None,
            retrieval_path_accuracy=float(d["retrieval_path_accuracy"]),
            evidence_completeness_rate=float(d["evidence_completeness_rate"]),
            provenance_completeness_rate=float(d["provenance_completeness_rate"]),
            term_coverage_valid_rate=float(d["term_coverage_valid_rate"]),
            per_query=[dict(q) for q in d.get("per_query", [])],
            structural_failures=[dict(f) for f in d.get("structural_failures", [])],
            generated_at=d.get("generated_at"),
        )


def evaluate_suite(
    db_path: Path | str,
    suite: GoldenSuite,
    *,
    store_revision: Optional[str] = None,
) -> EvaluationSummary:
    """Evaluate every golden query against a populated store.

    ``db_path`` must already contain the corpus (the runner ingests it first;
    tests build a disposable store). The suite's ``corpus_fingerprint`` is
    validated against the actual artifact SHAs recorded in the fixture; a
    mismatch marks every query failed as stale.
    """
    asset_paths = {
        a["canonical_id"]: a["path"] for a in suite.corpus_assets
    }
    computed_fp = compute_corpus_fingerprint(asset_paths)
    corpus_ok = computed_fp == suite.corpus_fingerprint

    if store_revision is None:
        store_revision = compute_store_revision(db_path)

    evaluations = []
    for golden in suite.queries:
        if not corpus_ok:
            ev = QueryEvaluation(
                query_id=golden.query_id,
                passed=False,
                hit_at_k=False,
                mrr=0.0,
                first_relevant_rank=None,
                precision_at_k=None,
                recall_at_k=None,
                f1_at_k=None,
                filter_correct=True,
                retrieval_path_correct=False,
                evidence_complete=True,
                provenance_complete=True,
                term_coverage_valid=True,
                required_hits_found=[],
                required_hits_missing=list(golden.required_ku_ids),
                forbidden_hits_found=[],
                expected_canonical_ok=False,
                forbidden_canonical_hits=[],
                zero_result_ok=False,
                actual_top_k_ids=[],
                actual_retrieval_path=golden.expected_retrieval_path,
                failure_reasons=["stale corpus fingerprint: golden bound to a different artifact version"],
            )
        else:
            ev = evaluate_query(db_path, golden)
        evaluations.append(ev)

    per_query = [ev.to_dict() for ev in evaluations]
    failed = [ev for ev in evaluations if not ev.passed]
    passed = [ev for ev in evaluations if ev.passed]
    exhaustive = [ev for ev in evaluations if ev.query_id in {q.query_id for q in suite.queries if q.relevance_judgment == "exhaustive"}]
    partial = [ev for ev in evaluations if ev.query_id not in {q.query_id for q in suite.queries if q.relevance_judgment == "exhaustive"}]
    filtered_queries = [ev for ev in evaluations if ev.query_id in {q.query_id for q in suite.queries if q.filters}]

    mean_hit = (sum(1 for ev in evaluations if ev.hit_at_k) / len(evaluations)) if evaluations else 0.0
    mean_mrr = (sum(ev.mrr for ev in evaluations) / len(evaluations)) if evaluations else 0.0

    def _mean(values: list[Optional[float]]) -> Optional[float]:
        present = [v for v in values if v is not None]
        return (sum(present) / len(present)) if present else None

    mean_precision = _mean([ev.precision_at_k for ev in exhaustive])
    mean_recall = _mean([ev.recall_at_k for ev in exhaustive])
    mean_f1 = _mean([ev.f1_at_k for ev in exhaustive])

    filter_accuracy = (
        (sum(1 for ev in filtered_queries if ev.filter_correct) / len(filtered_queries))
        if filtered_queries
        else None
    )
    path_accuracy = (sum(1 for ev in evaluations if ev.retrieval_path_correct) / len(evaluations)) if evaluations else 0.0
    evidence_rate = (sum(1 for ev in evaluations if ev.evidence_complete) / len(evaluations)) if evaluations else 0.0
    provenance_rate = (sum(1 for ev in evaluations if ev.provenance_complete) / len(evaluations)) if evaluations else 0.0
    coverage_rate = (sum(1 for ev in evaluations if ev.term_coverage_valid) / len(evaluations)) if evaluations else 0.0

    structural_failures = [
        {"query_id": ev.query_id, "failure_reasons": list(ev.failure_reasons)}
        for ev in failed
    ]

    return EvaluationSummary(
        suite_version=suite.suite_version,
        evaluation_policy_version=suite.evaluation_policy_version,
        ranking_policy_version=suite.store_config.get("ranking_policy_version", "lexical-ranking-v1"),
        corpus_fingerprint=suite.corpus_fingerprint,
        corpus_fingerprint_ok=corpus_ok,
        store_revision=store_revision,
        query_count=len(evaluations),
        exhaustive_query_count=len(exhaustive),
        partial_query_count=len(partial),
        passed_query_count=len(passed),
        failed_query_count=len(failed),
        mean_hit_at_k=mean_hit,
        mean_mrr=mean_mrr,
        mean_precision_at_k=mean_precision,
        mean_recall_at_k=mean_recall,
        mean_f1_at_k=mean_f1,
        filter_accuracy=filter_accuracy,
        retrieval_path_accuracy=path_accuracy,
        evidence_completeness_rate=evidence_rate,
        provenance_completeness_rate=provenance_rate,
        term_coverage_valid_rate=coverage_rate,
        per_query=per_query,
        structural_failures=structural_failures,
        generated_at=None,
    )


def write_evaluation_report(summary: EvaluationSummary, path: Path | str, *, generated_at: str) -> None:
    """Write the machine-readable evaluation report (report files are generated
    artifacts; ``generated_at`` is recorded but never compared for determinism)."""
    report = summary.to_dict()
    report["generated_at"] = generated_at
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")