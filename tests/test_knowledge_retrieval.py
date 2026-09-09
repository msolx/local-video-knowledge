"""M5-03 Evidence-Grounded Retrieval API tests.

Covers the public RetrievalQuery -> RetrievalHit[] -> RetrievalResult contract
over the sealed M5-01 store + M5-02 trigram FTS index: query validation and
normalization, long-term FTS path, short-query (1-2 char) deterministic
substring fallback, mixed long+short path, structured filters applied before
ranking/LIMIT, canonical hydration, mandatory evidence expansion, source
artifact provenance, match info, BM25/short score semantics, deterministic
ordering, JSON serialization, and real C10 golden queries. Deterministic and
offline; no LLM/runtime, no network.
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
from src.knowledge.store import (
    compute_source_artifact_fingerprint,
    create_store,
    get_ingested_asset,
    ingest_knowledge_document,
)
from src.knowledge.retrieval import (
    MAX_TOP_K,
    RETRIEVAL_METHOD_FTS,
    RETRIEVAL_METHOD_MIXED,
    RETRIEVAL_METHOD_SHORT,
    RetrievalBackend,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    FTS5RetrievalBackend,
    normalize_retrieval_query,
    plan_literal_terms,
    retrieve,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"


# ----------------------------------------------------------------------
# Fixtures (mirrors tests/test_knowledge_store.py / test_knowledge_fts.py)
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
# 1-6. Query validation
# ----------------------------------------------------------------------

def test_valid_retrieval_query() -> None:
    q = RetrievalQuery(query_text="Vulkan 27B", top_k=5)
    assert q.query_text == "Vulkan 27B"
    assert q.top_k == 5
    assert q.canonical_ids is None


def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="   ")


def test_invalid_top_k() -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="x", top_k=0)
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="x", top_k=-3)


def test_top_k_cap() -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="x", top_k=MAX_TOP_K + 1)
    assert RetrievalQuery(query_text="x", top_k=MAX_TOP_K).top_k == MAX_TOP_K


def test_invalid_unit_type() -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="x", unit_types=["bogus_type"])


def test_invalid_verification_status() -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query_text="x", verification_statuses=["probably_true"])


# ----------------------------------------------------------------------
# Query normalization / planner
# ----------------------------------------------------------------------

def test_query_normalization_nfkc_strip_collapse() -> None:
    assert normalize_retrieval_query("  Vulkan\n  27B  ") == "Vulkan 27B"
    assert normalize_retrieval_query("\u3000全角\u3000") == "全角"


def test_query_plan_literal_terms() -> None:
    assert plan_literal_terms("Vulkan 27B") == ["Vulkan", "27B"]
    assert plan_literal_terms("  Vulkan  模型 ") == ["Vulkan", "模型"]


# ----------------------------------------------------------------------
# 7-13. Path selection & long-term FTS
# ----------------------------------------------------------------------

def test_long_terms_use_fts(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "Vulkan 后端加速推理引擎"), _unit("asset_a", "今天天气很好")],
    )
    res = _query(db_path, "Vulkan")
    assert res.retrieval_method == RETRIEVAL_METHOD_FTS
    assert res.result_count == 1
    assert res.hits[0].statement == "Vulkan 后端加速推理引擎"


def test_multiple_long_terms_are_anded(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端运行大模型"),
            _unit("asset_a", "Vulkan 图形接口说明", topics=["27B 参数量"]),
            _unit("asset_a", "27B 模型需要很多显存"),
            _unit("asset_a", "今天的午餐很丰盛"),
        ],
    )
    res = _query(db_path, "Vulkan 27B")
    assert res.retrieval_method == RETRIEVAL_METHOD_FTS
    ku_ids = [h.knowledge_unit_id for h in res.hits]
    assert len(ku_ids) == 1
    assert res.hits[0].statement == "Vulkan 图形接口说明"


def test_two_char_chinese_uses_short_fallback(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "本地大模型推理很快")])
    res = _query(db_path, "模型")
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count == 1


def test_one_char_uses_short_fallback(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "本地大模型推理很快")])
    res = _query(db_path, "模")
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count >= 1


def test_short_fallback_respects_top_k(db_path: Path) -> None:
    units = [_unit("asset_a", f"第{i}条本地大模型知识 {i}") for i in range(20)]
    _ingest(db_path, "asset_a", units)
    res = _query(db_path, "模型", top_k=5)
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count == 5
    assert [h.rank for h in res.hits] == [1, 2, 3, 4, 5]


def test_mixed_long_plus_short_requires_both(db_path: Path) -> None:
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
    assert res.hits[0].ranking_diagnostics["long_terms"] == ["Vulkan"]
    assert res.hits[0].ranking_diagnostics["short_terms"] == ["模型"]


def test_literal_fts_operators_harmless(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "Vulkan 后端加速推理引擎"), _unit("asset_a", "27B 模型需要很多显存")],
    )
    res = _query(db_path, "Vulkan NEAR 27B")
    assert res.result_count == 0
    res2 = _query(db_path, 'Vulkan OR "27B"')
    assert res2.result_count == 0


# ----------------------------------------------------------------------
# 14-22. Structured filters (before ranking / LIMIT)
# ----------------------------------------------------------------------

def test_canonical_id_filter(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    _ingest(db_path, "asset_b", [_unit("asset_b", "Vulkan 图形接口说明")])
    res = _query(db_path, "Vulkan", canonical_ids=["asset_a"])
    assert res.result_count == 1
    assert res.hits[0].canonical_id == "asset_a"


def test_multiple_canonical_ids_or(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    _ingest(db_path, "asset_b", [_unit("asset_b", "Vulkan 图形接口说明")])
    res = _query(db_path, "Vulkan", canonical_ids=["asset_a", "asset_b"])
    assert res.result_count == 2


def test_unit_type_filter(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", unit_type="claim"),
            _unit("asset_a", "我认为 Vulkan 不错", unit_type="opinion"),
        ],
    )
    res = _query(db_path, "Vulkan", unit_types=["claim"])
    assert res.result_count == 1
    assert res.hits[0].unit_type == "claim"


def test_multiple_unit_types_or(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", unit_type="claim"),
            _unit("asset_a", "我认为 Vulkan 不错", unit_type="opinion"),
        ],
    )
    res = _query(db_path, "Vulkan", unit_types=["claim", "opinion"])
    assert res.result_count == 2


def test_verification_filter(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", verification="not_checked"),
            _unit("asset_a", "Vulkan 是开放的图形标准", verification="verified"),
        ],
    )
    res = _query(db_path, "Vulkan", verification_statuses=["verified"])
    assert res.result_count == 1
    assert res.hits[0].verification_status == "verified"


def test_topic_structured_filter(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", topics=["GPU推理优化"]),
            _unit("asset_a", "Vulkan 图形接口说明", topics=["图形编程"]),
        ],
    )
    res = _query(db_path, "Vulkan", topics=["GPU推理优化"])
    assert res.result_count == 1
    assert res.hits[0].topics == ["GPU推理优化"]


def test_entity_structured_filter(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", entities=[{"entity_name": "Vulkan", "category": "technology"}]),
            _unit("asset_a", "图形接口说明", entities=[{"entity_name": "RDNA", "category": "technology"}]),
        ],
    )
    res = _query(db_path, "Vulkan", entity_names=["Vulkan"])
    assert res.result_count == 1
    assert res.hits[0].entities[0].entity_name == "Vulkan"


def test_cross_category_filters_and(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit(
                "asset_a",
                "Vulkan 后端加速推理引擎",
                unit_type="claim",
                topics=["GPU推理优化"],
            ),
            _unit("asset_a", "Vulkan 图形接口说明", unit_type="opinion", topics=["图形编程"]),
        ],
    )
    res = _query(db_path, "Vulkan", unit_types=["claim"], topics=["GPU推理优化"])
    assert res.result_count == 1
    assert res.hits[0].statement == "Vulkan 后端加速推理引擎"


def test_filters_before_limit(db_path: Path) -> None:
    a_units = [_unit("asset_a", f"Vulkan 后端加速 {i} 次") for i in range(20)]
    b_units = [_unit("asset_b", f"Vulkan 后端加速 {i} 次") for i in range(20)]
    _ingest(db_path, "asset_a", a_units)
    _ingest(db_path, "asset_b", b_units)
    res = _query(db_path, "Vulkan", top_k=5, canonical_ids=["asset_a"])
    assert res.result_count == 5
    assert all(h.canonical_id == "asset_a" for h in res.hits)


# ----------------------------------------------------------------------
# 23-30. Canonical hydration & evidence expansion
# ----------------------------------------------------------------------

def test_full_canonical_hydration(db_path: Path) -> None:
    units = [
        _unit(
            "asset_a",
            "Vulkan 后端加速推理引擎",
            confidence=0.87,
            entities=[{"entity_name": "Vulkan", "category": "technology"}],
            topics=["GPU推理优化"],
        )
    ]
    _ingest(db_path, "asset_a", units)
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.knowledge_unit_id == units[0]["knowledge_unit_id"]
    assert hit.unit_type == "claim"
    assert hit.extraction_confidence == 0.87
    assert [e.entity_name for e in hit.entities] == ["Vulkan"]
    assert hit.topics == ["GPU推理优化"]


def test_evidence_refs_populated(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert len(hit.evidence_refs) == 1
    assert hit.evidence_refs[0].evidence_id == "ev_seg_000001"
    assert hit.evidence_refs[0].source_excerpt == "测试证据内容"


def test_evidence_ordering_preserved(db_path: Path) -> None:
    refs = [
        EvidenceRef("ev_002", "第二条证据", temporal_range=TemporalRange(5.0, 6.0, 1.0)),
        EvidenceRef("ev_001", "第一条证据", temporal_range=TemporalRange(1.0, 2.0, 1.0)),
        EvidenceRef("ev_003", "第三条证据", temporal_range=TemporalRange(9.0, 10.0, 1.0)),
    ]
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎", evidence=refs)])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert [r.evidence_id for r in hit.evidence_refs] == ["ev_002", "ev_001", "ev_003"]


def test_both_coordinate_types_preserved(db_path: Path) -> None:
    refs = [
        EvidenceRef("ev_seg_000001", "时间坐标证据", temporal_range=TemporalRange(1.0, 2.0, 1.0)),
        EvidenceRef("ve_img_001", "序列坐标证据", sequence_range=SequenceRange(3)),
    ]
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎", evidence=refs)])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.evidence_refs[0].temporal_range.start == 1.0
    assert hit.evidence_refs[0].temporal_range.end == 2.0
    assert hit.evidence_refs[1].sequence_range.sequence_index == 3
    assert hit.evidence_refs[1].temporal_range is None


def test_attribution_populated(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.attribution.source_actor_name == "测试作者"
    assert hit.attribution.attribution_status == AttributionStatus.UNVERIFIED_SPEAKER


def test_lineage_populated(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.extraction_lineage.extraction_run_id == "run_test"
    assert hit.extraction_lineage.input_chunk_ids == ["chk_000001"]


def test_source_artifact_path(db_path: Path) -> None:
    path = _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert hit.source_artifact["path"] == str(path)
    assert hit.source_artifact["fingerprint"] == compute_source_artifact_fingerprint(
        json.loads(path.read_text(encoding="utf-8"))
    )


# ----------------------------------------------------------------------
# 31-37. Score semantics & determinism
# ----------------------------------------------------------------------

def test_store_revision_present(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    assert res.store_revision is not None
    assert res.store_revision == res.store_revision


def test_matched_fields_and_terms(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "Vulkan 后端加速推理引擎", topics=["GPU推理优化"])],
    )
    res = _query(db_path, "Vulkan")
    hit = res.hits[0]
    assert "statement" in hit.match_info["matched_on"]
    assert "Vulkan" in hit.match_info["matched_terms"]


def test_raw_bm25_exposed(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "Vulkan")
    assert "raw_bm25" in res.hits[0].ranking_diagnostics
    assert isinstance(res.hits[0].ranking_diagnostics["raw_bm25"], float)


def test_bm25_lower_is_better(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", topics=["GPU推理优化"]),
            _unit("asset_a", "图形接口说明", evidence=[
                EvidenceRef("ev_seg_000001", "Vulkan 支持跨平台图形", temporal_range=TemporalRange(1.0, 2.0, 1.0))
            ]),
        ],
    )
    res = _query(db_path, "Vulkan")
    assert res.result_count == 2
    scores = [h.ranking_diagnostics["raw_bm25"] for h in res.hits]
    assert scores == sorted(scores)
    assert res.hits[0].statement == "Vulkan 后端加速推理引擎"


def test_short_score_higher_is_better(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "本地大模型推理很快", topics=["GPU推理优化"]),
            _unit("asset_a", "今天天气不错", topics=["GPU推理优化"]),
        ],
    )
    res = _query(db_path, "模型")
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    scores = [h.ranking_diagnostics["weighted_substring_score"] for h in res.hits]
    assert scores == sorted(scores, reverse=True)
    assert res.hits[0].statement == "本地大模型推理很快"


def test_deterministic_ties_unit_rowid_asc(db_path: Path) -> None:
    refs_a = [EvidenceRef("ev_a", "同一句证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))]
    refs_b = [EvidenceRef("ev_b", "同一句证据", temporal_range=TemporalRange(1.0, 2.0, 1.0))]
    _ingest(
        db_path,
        "asset_a",
        [
            _unit("asset_a", "Vulkan 后端加速推理引擎", evidence=refs_a),
            _unit("asset_a", "Vulkan 后端加速推理引擎", evidence=refs_b),
        ],
    )
    res1 = _query(db_path, "Vulkan")
    res2 = _query(db_path, "Vulkan")
    assert [h.knowledge_unit_id for h in res1.hits] == [h.knowledge_unit_id for h in res2.hits]
    assert [h.rank for h in res1.hits] == [1, 2]


# ----------------------------------------------------------------------
# 38-42. Edge behavior & serialization
# ----------------------------------------------------------------------

def test_zero_result_is_legal(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "量子纠缠")
    assert res.result_count == 0
    assert res.hits == []


def test_no_llm_fallback_on_empty(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    res = _query(db_path, "量子纠缠")
    assert res.retrieval_method in (RETRIEVAL_METHOD_FTS, RETRIEVAL_METHOD_SHORT)


def test_chinese_unicode_query(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "本地大模型推理很快")])
    res = _query(db_path, "大模型")
    assert res.result_count == 1


def test_sql_injection_harmless(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "Vulkan 后端加速推理引擎"), _unit("asset_a", "今天天气不错")],
    )
    res = _query(db_path, "' OR 1=1 --")
    assert res.result_count == 0
    res2 = _query(db_path, "Vulkan; DROP TABLE knowledge_units;")
    assert res2.result_count >= 0


def test_json_round_trip(db_path: Path) -> None:
    _ingest(
        db_path,
        "asset_a",
        [_unit("asset_a", "Vulkan 后端加速推理引擎", topics=["GPU推理优化"])],
    )
    res = _query(db_path, "Vulkan")
    d = res.to_dict()
    res2 = RetrievalResult.from_dict(d)
    assert res2.to_dict() == d
    q = RetrievalQuery(query_text="Vulkan 27B", top_k=7, canonical_ids=["asset_a"])
    assert RetrievalQuery.from_dict(q.to_dict()) == q
    hit = res.hits[0]
    assert RetrievalHit.from_dict(hit.to_dict()) == hit


def test_backend_abstraction(db_path: Path) -> None:
    _ingest(db_path, "asset_a", [_unit("asset_a", "Vulkan 后端加速推理引擎")])
    backend = FTS5RetrievalBackend(db_path)
    assert isinstance(backend, RetrievalBackend)
    res = backend.retrieve(RetrievalQuery(query_text="Vulkan"))
    assert res.result_count == 1


# ----------------------------------------------------------------------
# 43-55. Real C10 golden queries
# ----------------------------------------------------------------------

def test_real_c10_vulkan(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan", top_k=20)
    assert res.retrieval_method == RETRIEVAL_METHOD_FTS
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_rdna(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "RDNA", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_thinking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Thinking", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_27b(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "27B", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_logitech(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "logitech", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == ALBUM_ASSET for h in res.hits)


def test_real_c10_agon(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "AGON", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == ALBUM_ASSET for h in res.hits)


def test_real_c10_smiley(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "SMILEY", top_k=20)
    assert res.result_count >= 1
    assert all(h.canonical_id == ALBUM_ASSET for h in res.hits)


def test_real_c10_model_two_char(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "模型", top_k=20)
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_speed_two_char(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "速度", top_k=20)
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_inference_two_char_fallback(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "推理", top_k=20)
    assert res.retrieval_method == RETRIEVAL_METHOD_SHORT
    assert res.result_count >= 0


def test_real_c10_mixed_long_short(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan 模型", top_k=20)
    assert res.retrieval_method == RETRIEVAL_METHOD_MIXED
    assert res.result_count >= 1
    assert all(h.canonical_id == VIDEO_ASSET for h in res.hits)


def test_real_c10_evidence_expansion(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "Vulkan", top_k=20)
    for hit in res.hits:
        assert len(hit.evidence_refs) >= 1
        assert all(r.source_excerpt for r in hit.evidence_refs)
        assert all(r.evidence_id for r in hit.evidence_refs)


def test_real_c10_structured_filters(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    res = _query(db_path, "logitech", top_k=20, canonical_ids=[VIDEO_ASSET])
    assert res.result_count == 0
    res = _query(db_path, "logitech", top_k=20, canonical_ids=[ALBUM_ASSET])
    assert res.result_count >= 1
    assert all(h.canonical_id == ALBUM_ASSET for h in res.hits)
    res = _query(db_path, "Vulkan", top_k=20, unit_types=["claim"])
    assert res.result_count >= 1
    assert all(h.unit_type == "claim" for h in res.hits)
    res = _query(db_path, "Vulkan", top_k=20, verification_statuses=["not_checked"])
    assert res.result_count >= 1
    assert all(h.verification_status == "not_checked" for h in res.hits)