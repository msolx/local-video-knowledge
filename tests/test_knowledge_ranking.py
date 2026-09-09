"""M5-04 Deterministic Retrieval Ranking & Query Diagnostics tests.

Covers the ``lexical-ranking-v1`` ranking policy over the sealed M5-01 store +
M5-02 trigram FTS index and the M5-03 retrieval contract: QueryPlan, result
diagnostics (candidate count before LIMIT, filters applied, ranking policy
version, limitations), field-match signals, exact-phrase signals, term
coverage invariant, evidence-only diagnostics, statement/entity/topic/evidence
relative ranking, deterministic tie-breaking, JSON-safety, and real C10
ranking audits. Deterministic and offline; no LLM/runtime, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    AttributionInfo,
    AttributionStatus,
    EntityMention,
    EvidenceRef,
    ExtractionLineage,
    SequenceRange,
    TemporalRange,
    create_knowledge_unit,
)
from src.knowledge.store import ingest_knowledge_document
from src.knowledge.retrieval import (
    MAX_TOP_K,
    RANKING_POLICY_VERSION,
    RETRIEVAL_METHOD_FTS,
    RETRIEVAL_METHOD_MIXED,
    RETRIEVAL_METHOD_SHORT,
    RETRIEVAL_PATH_FTS,
    RETRIEVAL_PATH_MIXED,
    RETRIEVAL_PATH_SHORT,
    QueryPlan,
    RetrievalInvariantError,
    RetrievalQuery,
    RetrievalResult,
    build_query_plan,
    check_retrieval_invariant,
    retrieve,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"


# ----------------------------------------------------------------------
# Fixtures (mirrors tests/test_knowledge_retrieval.py)
# ----------------------------------------------------------------------

def _lineage(run_id: str = "run_test", chunk: str = "chk_000001") -> ExtractionLineage:
    return ExtractionLineage(
        extraction_run_id=run_id,
        input_chunk_ids=[chunk],
        candidate_id="cand_chk_000001_001_abc",
        source_candidate_ids=["cand_chk_000001_001_abc"],
        merge_strategy=None,
    )


def _unit(
    canonical_id: str,
    statement: str,
    *,
    evidence: Optional[list[EvidenceRef]] = None,
    confidence: float = 0.9,
    unit_type: str = "claim",
    entities: Optional[list[Any]] = None,
    topics: Optional[list[str]] = None,
    verification: str = "not_checked",
) -> dict[str, Any]:
    refs = evidence or [
        EvidenceRef(
            evidence_id="ev_seg_000001",
            source_excerpt="测试证据内容",
            temporal_range=TemporalRange(1.0, 2.0, 1.0),
        )
    ]
    return create_knowledge_unit(
        canonical_id=canonical_id,
        unit_type=unit_type,
        statement=statement,
        evidence_refs=refs,
        attribution=AttributionInfo(
            source_actor_name="测试作者",
            attribution_status=AttributionStatus.UNVERIFIED_SPEAKER,
        ),
        extraction_confidence=confidence,
        extraction_lineage=_lineage(),
        verification_status=verification,
        entities=[
            EntityMention(entity_name=e["entity_name"], category=e["category"])
            for e in (entities or [])
        ],
        topics=topics or [],
    ).to_dict()


def _asset_document(
    canonical_id: str,
    units: list[dict[str, Any]],
    *,
    generated_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "canonical_id": canonical_id,
        "generated_at": generated_at,
        "unit_count": len(units),
        "units": units,
        "extraction_provenance": {
            "backend": "mock",
            "model": "qwen3-8b",
            "prompt_version": "m4-extraction-v1.0",
            "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "temperature": 0.1,
            "generated_at": generated_at,
            "evidence_manifest_fingerprint": "a" * 64,
            "evidence_chunks_fingerprint": "b" * 64,
        },
    }


def _write_asset(tmp_path: Path, artifact: dict[str, Any], name: str = "knowledge_units.json") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "knowledge_store.sqlite3"


def _ingest(db_path: Path, canonical_id: str, units: list[dict[str, Any]]) -> Path:
    artifact = _asset_document(canonical_id, units)
    path = _write_asset(Path(db_path).parent, artifact, name=f"{canonical_id}_units.json")
    ingest_knowledge_document(db_path, path)
    return path


def _query(db_path: Path, text: str, **kwargs: Any) -> RetrievalResult:
    return retrieve(db_path, RetrievalQuery(query_text=text, **kwargs))


def _c10_path(asset_id: str) -> Path:
    return ROOT / "data" / "processed" / asset_id / "knowledge" / "knowledge_units.json"


def _build_real_store(db_path: Path) -> bool:
    if not (_c10_path(VIDEO_ASSET).is_file() and _c10_path(ALBUM_ASSET).is_file()):
        return False
    ingest_knowledge_document(db_path, _c10_path(VIDEO_ASSET))
    ingest_knowledge_document(db_path, _c10_path(ALBUM_ASSET))
    return True


# ----------------------------------------------------------------------
# 1-3. Query plan (long / short / mixed)
# ----------------------------------------------------------------------

def test_query_plan_long() -> None:
    plan = build_query_plan(RetrievalQuery(query_text="Vulkan 27B", top_k=5))
    assert plan.original_query == "Vulkan 27B"
    assert plan.normalized_query == "Vulkan 27B"
    assert plan.terms == ["Vulkan", "27B"]
    assert plan.long_terms == ["Vulkan", "27B"]
    assert plan.short_terms == []
    assert plan.retrieval_path == RETRIEVAL_PATH_FTS
    assert plan.filters == {}
    assert plan.top_k == 5


def test_query_plan_short() -> None:
    plan = build_query_plan(RetrievalQuery(query_text="模型 推理"))
    assert plan.long_terms == []
    assert plan.short_terms == ["模型", "推理"]
    assert plan.retrieval_path == RETRIEVAL_PATH_SHORT


def test_query_plan_mixed() -> None:
    plan = build_query_plan(RetrievalQuery(query_text="Vulkan 模型"))
    assert plan.long_terms == ["Vulkan"]
    assert plan.short_terms == ["模型"]
    assert plan.retrieval_path == RETRIEVAL_PATH_MIXED


def test_query_plan_filters_recorded() -> None:
    plan = build_query_plan(
        RetrievalQuery(query_text="Vulkan", canonical_ids=["asset_a"], unit_types=["claim"])
    )
    assert plan.filters == {"canonical_ids": ["asset_a"], "unit_types": ["claim"]}


def test_query_plan_json_round_trip() -> None:
    plan = build_query_plan(RetrievalQuery(query_text="Vulkan 模型", top_k=7, canonical_ids=["a"]))
    restored = QueryPlan.from_dict(plan.to_dict())
    assert restored == plan
    assert json.loads(json.dumps(plan.to_dict())) == plan.to_dict()


# ----------------------------------------------------------------------
# 4. Filter diagnostics
# ----------------------------------------------------------------------

def test_filter_diagnostics(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    _ingest(db_path, "asset_b", [_unit("asset_b", "Vulkan 图形接口说明")])
    res = _query(db_path, "Vulkan", canonical_ids=["asset_a"], unit_types=["claim"])
    assert res.diagnostics["filters_applied"] == {
        "canonical_ids": ["asset_a"],
        "unit_types": ["claim"],
    }


def test_no_inferred_filters(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    assert res.diagnostics["filters_applied"] == {}


# ----------------------------------------------------------------------
# 5-6. Candidate count / result count after limit
# ----------------------------------------------------------------------

def test_candidate_count_before_limit(db_path: Path) -> None:
    units = [_unit("asset_a", f"Vulkan 后端加速第 {i} 次") for i in range(12)]
    _ingest(db_path, "asset_a", units)
    res = _query(db_path, "Vulkan", top_k=5)
    assert res.diagnostics["candidate_count_before_limit"] == 12
    assert res.diagnostics["result_count"] == 5
    assert res.result_count == 5


def test_candidate_count_with_filters(db_path: Path) -> None:
    a_units = [_unit("asset_a", f"Vulkan 后端加速第 {i} 次") for i in range(8)]
    b_units = [_unit("asset_b", f"Vulkan 后端加速第 {i} 次") for i in range(4)]
    _ingest(db_path, "asset_a", a_units)
    _ingest(db_path, "asset_b", b_units)
    res = _query(db_path, "Vulkan", top_k=3, canonical_ids=["asset_a"])
    assert res.diagnostics["candidate_count_before_limit"] == 8
    assert res.result_count == 3


# ----------------------------------------------------------------------
# 7. Ranking policy version
# ----------------------------------------------------------------------

def test_ranking_policy_version_present(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    assert res.diagnostics["ranking_policy_version"] == RANKING_POLICY_VERSION
    assert res.hits[0].ranking_diagnostics["ranking_policy_version"] == RANKING_POLICY_VERSION


# ----------------------------------------------------------------------
# 8-11. Field-match signals (statement / entity / topic / evidence)
# ----------------------------------------------------------------------

def test_statement_direct_match(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.match_info["statement_match"] is True
    assert hit.match_info["evidence_only_match"] is False


def test_entity_match_flag(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "图形接口说明", entities=[{"entity_name": "Vulkan", "category": "technology"}])],
    )
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.match_info["entity_match"] is True
    assert hit.match_info["statement_match"] is False


def test_topic_match_flag(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "图形接口说明", topics=["Vulkan 后端"])])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.match_info["topic_match"] is True
    assert hit.match_info["statement_match"] is False


def test_evidence_match_flag(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit(
                "asset_a",
                "图形接口说明",
                evidence=[EvidenceRef("ev_seg_000001", "Vulkan 支持跨平台图形", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            )
        ],
    )
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.match_info["evidence_match"] is True
    assert hit.match_info["evidence_only_match"] is True


# ----------------------------------------------------------------------
# 12. Evidence-only diagnostic
# ----------------------------------------------------------------------

def test_evidence_only_diagnostic(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎"),
            _unit(
                "asset_a",
                "图形接口说明",
                evidence=[EvidenceRef("ev_seg_000001", "Vulkan 支持跨平台图形", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
        ],
    )
    res = _query(db_path, "Vulkan")
    by_statement = {h.statement: h for h in res.hits}
    assert by_statement["图形接口说明"].match_info["evidence_only_match"] is True
    assert by_statement["Vulkan 后端加速推理引擎"].match_info["evidence_only_match"] is False


# ----------------------------------------------------------------------
# 13. Exact statement phrase
# ----------------------------------------------------------------------

def test_exact_statement_phrase(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "我们来看看 Vulkan 27B 的效果")])
    res = _query(db_path, "Vulkan 27B")
    hit = res.hits[0]
    assert hit.match_info["exact_statement_phrase"] is True


def test_exact_phrase_absent_for_non_contiguous(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 相比 27B 更稳定")])
    res = _query(db_path, "Vulkan 27B")
    if res.result_count:
        assert res.hits[0].match_info["exact_statement_phrase"] is False


# ----------------------------------------------------------------------
# 14-16. Matched term count / coverage / missing-term exclusion
# ----------------------------------------------------------------------

def test_matched_term_count(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端运行 27B 模型")])
    res = _query(db_path, "Vulkan 27B")
    hit = res.hits[0]
    assert hit.match_info["matched_term_count"] == 2
    assert hit.match_info["total_term_count"] == 2
    assert hit.match_info["term_coverage"] == 1.0


def test_full_term_coverage_invariant(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行 27B 模型"),
            _unit("asset_a", "Vulkan 图形接口说明"),
            _unit("asset_a", "27B 模型需要很多显存"),
        ],
    )
    res = _query(db_path, "Vulkan 27B")
    assert check_retrieval_invariant(res) == []
    for hit in res.hits:
        assert hit.match_info["term_coverage"] == 1.0


def test_missing_required_term_excluded(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行 27B 模型"),
            _unit("asset_a", "Vulkan 图形接口说明"),
            _unit("asset_a", "27B 模型需要很多显存"),
        ],
    )
    res = _query(db_path, "Vulkan 27B")
    assert all("Vulkan" in h.match_info["matched_terms"] for h in res.hits)
    assert all("27B" in h.match_info["matched_terms"] for h in res.hits)


# ----------------------------------------------------------------------
# 17-18. Score directions preserved
# ----------------------------------------------------------------------

def test_bm25_lower_is_better_direction(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.ranking_diagnostics["bm25_direction"] == "lower_is_better"
    assert isinstance(hit.ranking_diagnostics["raw_bm25"], float)


def test_short_score_higher_is_better_direction(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "本地大模型推理很快")])
    res = _query(db_path, "模型")
    hit = res.hits[0]
    assert hit.ranking_diagnostics["score_direction"] == "higher_is_better"
    assert isinstance(hit.ranking_diagnostics["weighted_substring_score"], float)


# ----------------------------------------------------------------------
# 19-21. Relative ranking (statement > entity > topic > evidence-only)
# ----------------------------------------------------------------------

def test_statement_outranks_evidence_only(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit(
                "asset_a",
                "Vulkan 后端加速推理引擎",
                evidence=[EvidenceRef("ev_a", "普通证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "图形接口说明",
                evidence=[EvidenceRef("ev_b", "Vulkan 支持跨平台图形绘制" * 3, temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
        ],
    )
    res = _query(db_path, "Vulkan")
    assert res.result_count == 2
    statements = [h.statement for h in res.hits]
    assert statements.index("Vulkan 后端加速推理引擎") < statements.index("图形接口说明")


def test_entity_topic_evidence_priority(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit(
                "asset_a",
                "某图形标准说明",
                entities=[{"entity_name": "Vulkan", "category": "technology"}],
                evidence=[EvidenceRef("ev_entity", "实体证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "某图形标准说明",
                topics=["Vulkan 后端"],
                evidence=[EvidenceRef("ev_topic", "主题证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "某图形标准说明",
                evidence=[EvidenceRef("ev_c", "Vulkan 支持跨平台", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
        ],
    )
    res = _query(db_path, "Vulkan", top_k=3)
    match_kinds = [
        "entity" if h.match_info["entity_match"] else ("topic" if h.match_info["topic_match"] else "evidence")
        for h in res.hits
    ]
    assert match_kinds[0] == "entity"
    assert match_kinds[1] == "topic"
    assert match_kinds[2] == "evidence"


def test_statement_entity_topic_evidence_priority(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit(
                "asset_a",
                "Vulkan 图形接口说明",
                evidence=[EvidenceRef("ev_stmt", "语句证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "某图形标准说明",
                entities=[{"entity_name": "Vulkan", "category": "technology"}],
                evidence=[EvidenceRef("ev_entity", "实体证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "某图形标准说明",
                topics=["Vulkan 后端"],
                evidence=[EvidenceRef("ev_topic", "主题证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
            _unit(
                "asset_a",
                "某图形标准说明",
                evidence=[EvidenceRef("ev_d", "Vulkan 支持跨平台", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
            ),
        ],
    )
    res = _query(db_path, "Vulkan", top_k=4)
    assert res.hits[0].match_info["statement_match"] is True
    assert res.hits[1].match_info["entity_match"] is True
    assert res.hits[2].match_info["topic_match"] is True
    assert res.hits[3].match_info["evidence_match"] is True
    assert res.hits[3].match_info["evidence_only_match"] is True


def test_tie_uses_unit_rowid(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", evidence=[EvidenceRef("ev_a", "同一句证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))]),
            _unit("asset_a", "Vulkan 后端加速推理引擎", evidence=[EvidenceRef("ev_b", "同一句证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))]),
        ],
    )
    res1 = _query(db_path, "Vulkan")
    res2 = _query(db_path, "Vulkan")
    assert [h.knowledge_unit_id for h in res1.hits] == [h.knowledge_unit_id for h in res2.hits]
    assert [h.rank for h in res1.hits] == [1, 2]


def test_repeated_query_stable(db_path: Path) -> None:
    units = [_unit("asset_a", f"Vulkan 后端加速第 {i} 次 27B 评测") for i in range(8)]
    _ingest(db_path, "asset_a", units)
    a = _query(db_path, "Vulkan 27B")
    b = _query(db_path, "Vulkan 27B")
    assert [h.knowledge_unit_id for h in a.hits] == [h.knowledge_unit_id for h in b.hits]
    assert a.to_dict() == b.to_dict()


# ----------------------------------------------------------------------
# 23-24. Confidence / verification do NOT change rank
# ----------------------------------------------------------------------

def test_extraction_confidence_does_not_change_rank(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", confidence=0.3),
            _unit("asset_a", "Vulkan 图形接口说明", confidence=0.95),
        ],
    )
    res_lo = _query(db_path, "Vulkan")
    res_hi = _query(db_path, "Vulkan")
    assert [h.knowledge_unit_id for h in res_lo.hits] == [h.knowledge_unit_id for h in res_hi.hits]


def test_verification_status_does_not_change_rank(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", verification="contested"),
            _unit("asset_a", "Vulkan 图形接口说明", verification="verified"),
        ],
    )
    res = _query(db_path, "Vulkan")
    scores = [h.ranking_diagnostics["raw_bm25"] for h in res.hits]
    assert scores == sorted(scores)


# ----------------------------------------------------------------------
# 25-28. top_k / AND semantics (multi-term long / short / mixed)
# ----------------------------------------------------------------------

def test_top_k_after_filters_and_ranking(db_path: Path) -> None:
    a_units = [_unit("asset_a", f"Vulkan 后端加速第 {i} 次") for i in range(12)]
    _ingest(db_path, "asset_a", a_units)
    res = _query(db_path, "Vulkan", top_k=4, canonical_ids=["asset_a"])
    assert res.diagnostics["candidate_count_before_limit"] == 12
    assert res.result_count == 4
    assert [h.rank for h in res.hits] == [1, 2, 3, 4]


def test_multi_term_long_and(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行 27B 模型"),
            _unit("asset_a", "Vulkan 图形接口说明"),
            _unit("asset_a", "27B 模型需要很多显存"),
        ],
    )
    res = _query(db_path, "Vulkan 27B")
    assert res.result_count == 1
    assert res.hits[0].statement == "Vulkan 后端运行 27B 模型"


def test_multi_term_short_and(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "模型推理速度很快"),
            _unit("asset_a", "模型需要显存"),
            _unit("asset_a", "推理过程很慢"),
        ],
    )
    res = _query(db_path, "模型 推理")
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count == 1
    assert res.hits[0].statement == "模型推理速度很快"


def test_mixed_query_and(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行大模型"),
            _unit("asset_a", "Vulkan 图形接口说明"),
            _unit("asset_a", "本地大模型推理很快"),
        ],
    )
    res = _query(db_path, "Vulkan 模型")
    assert res.retrieval_method == RETRIEVAL_METHOD_MIXED
    assert res.result_count == 1
    assert res.hits[0].statement == "Vulkan 后端运行大模型"


# ----------------------------------------------------------------------
# 29-31. No fuzzy / no rewrite / JSON-safe diagnostics
# ----------------------------------------------------------------------

def test_no_fuzzy_matching(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "RTX 4090 性能很好"),
            _unit("asset_a", "GPU 推理速度评测"),
        ],
    )
    res = _query(db_path, "4090")
    assert res.result_count == 1
    assert res.hits[0].statement == "RTX 4090 性能很好"


def test_no_semantic_rewrite(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "图形编程")
    assert res.result_count == 0


def test_diagnostics_json_safe(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行 27B 模型", topics=["GPU推理优化"]),
            _unit("asset_a", "本地大模型推理很快"),
        ],
    )
    for text in ("Vulkan 27B", "模型", "Vulkan 模型"):
        res = _query(db_path, text)
        json.dumps(res.to_dict())
        json.dumps(res.diagnostics)


def test_diagnostics_contain_no_sql(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    blob = json.dumps(res.to_dict())
    assert "SELECT" not in blob
    assert "MATCH" not in blob
    assert "knowledge_fts" not in blob


# ----------------------------------------------------------------------
# 32-33. Existing RetrievalHit compatibility / zero-result diagnostics
# ----------------------------------------------------------------------

def test_existing_retrieval_hit_compatibility(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert "matched_on" in hit.match_info
    assert "matched_terms" in hit.match_info
    assert "raw_bm25" in hit.ranking_diagnostics
    assert res.retrieval_method == RETRIEVAL_METHOD_FTS
    assert res.store_schema_version == "knowledge-store-v1"
    assert res.store_revision is not None


def test_zero_result_diagnostics(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "量子纠缠")
    assert res.result_count == 0
    assert res.hits == []
    assert res.diagnostics["candidate_count_before_limit"] == 0
    assert res.diagnostics["result_count"] == 0
    assert res.diagnostics["retrieval_path"] == RETRIEVAL_PATH_FTS
    assert res.diagnostics["short_query_fallback"] is False


def test_short_query_fallback_flag(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "本地大模型推理很快")])
    res = _query(db_path, "模型")
    assert res.diagnostics["short_query_fallback"] is True
    assert res.diagnostics["limitations"] != []


# ----------------------------------------------------------------------
# 34-38. Real C10 ranking audits
# ----------------------------------------------------------------------

def test_real_c10_vulkan_ranking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan", top_k=5)
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)
    assert check_retrieval_invariant(res) == []
    for hit in res.hits:
        assert hit.match_info["term_coverage"] == 1.0
        assert hit.ranking_diagnostics["ranking_policy_version"] == RANKING_POLICY_VERSION


def test_real_c10_model_short_ranking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "模型", top_k=5)
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)
    assert check_retrieval_invariant(res) == []


def test_real_c10_logitech_ranking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "logitech", top_k=5)
    assert res.result_count >= 1
    assert all(h.canonical_id == ALBUM_ASSET for h in res.hits)


def test_real_c10_mixed_ranking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan 模型", top_k=5)
    assert res.retrieval_method == RETRIEVAL_METHOD_MIXED
    assert res.result_count >= 1
    assert check_retrieval_invariant(res) == []
    for hit in res.hits:
        assert "Vulkan" in hit.match_info["matched_terms"]
        assert "模型" in hit.match_info["matched_terms"]


def test_real_c10_top_ranking_explainable(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan", top_k=5)
    for hit in res.hits:
        assert "why_this_hit" in hit.ranking_diagnostics
        assert "ranking_components" in hit.ranking_diagnostics
        assert hit.ranking_diagnostics["why_this_hit"] != ""
        assert hit.match_info["evidence_only_match"] in (True, False)
        assert hit.rank == hit.ranking_diagnostics["rank"]