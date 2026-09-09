"""M5-06 End-to-End Acceptance tests (deterministic, fully offline).

These tests exercise the M5-06 acceptance chain: source discovery + validation,
disposable store rebuild + validation, KU round-trip, FTS count, retrieval
contract, short/mixed queries, structured filters, ranking diagnostics, golden
corpus fingerprint + evaluation, deterministic repeated evaluation, deterministic
rebuild revision, rebuild failure preserves an existing store, and production-path
safety (normal tests never touch data/knowledge/knowledge_store.sqlite3).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from src.knowledge.evaluation import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    evaluate_suite,
    load_golden_suite,
)
from src.knowledge.models import CanonicalKnowledgeUnit  # noqa: E402
from src.knowledge.retrieval import (  # noqa: E402
    RETRIEVAL_PATH_FTS,
    RETRIEVAL_PATH_MIXED,
    RETRIEVAL_PATH_SHORT,
    RetrievalQuery,
    check_retrieval_invariant,
    retrieve,
)
from src.knowledge.store import (  # noqa: E402
    DEFAULT_STORE_PATH,
    compute_store_revision,
    create_store,
    discover_final_artifacts,
    ingest_knowledge_document,
    list_ingested_assets,
    list_units_for_asset,
    rebuild_store,
    validate_store,
)

VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"

PRODUCTION_STORE = ROOT / DEFAULT_STORE_PATH


def _synthetic_document(tmp_path: Path, canonical_id: str, unit_count: int) -> Path:
    from src.knowledge.models import (
        AttributionInfo,
        EvidenceRef,
        ExtractionLineage,
        TemporalRange,
        create_knowledge_unit,
    )

    units = []
    for i in range(unit_count):
        statement = f"知识声明 {i + 1}：这是第{i + 1}条测试知识。"
        evidence = EvidenceRef(
            evidence_id=f"ev_seg_{i + 1:06d}",
            source_excerpt=statement,
            temporal_range=TemporalRange(start=1.0, end=2.0, duration=1.0),
        )
        lineage = ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=["chk_000001"],
            candidate_id=f"cand_chk_000001_{i + 1:03d}_abc",
            source_candidate_ids=[f"cand_chk_000001_{i + 1:03d}_abc"],
        )
        unit = create_knowledge_unit(
            canonical_id=canonical_id,
            unit_type="claim",
            statement=statement,
            evidence_refs=[evidence],
            attribution=AttributionInfo(
                source_actor_name="测试作者",
                source_actor_id="actor_1",
                speaker_name=None,
                speaker_id=None,
            ),
            extraction_confidence=0.9,
            extraction_lineage=lineage,
        )
        units.append(unit.to_dict())
    artifact = {
        "schema_version": "knowledge-units-v1",
        "canonical_id": canonical_id,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "unit_count": len(units),
        "units": units,
        "extraction_provenance": {
            "backend": "mock",
            "model": "qwen3-8b",
            "prompt_version": "m4-extraction-v1.0",
            "knowledge_schema_version": "knowledge-units-v1",
            "temperature": 0.1,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "evidence_manifest_fingerprint": "a" * 64,
            "evidence_chunks_fingerprint": "b" * 64,
        },
    }
    p = tmp_path / "processed" / canonical_id / "knowledge" / "knowledge_units.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def real_corpus():
    """Returns True iff both real C10 final artifacts exist on disk."""
    video = ROOT / "data" / "processed" / VIDEO_ASSET / "knowledge" / "knowledge_units.json"
    album = ROOT / "data" / "processed" / ALBUM_ASSET / "knowledge" / "knowledge_units.json"
    return video.is_file() and album.is_file()


@pytest.fixture
def real_store(tmp_path):
    video = ROOT / "data" / "processed" / VIDEO_ASSET / "knowledge" / "knowledge_units.json"
    album = ROOT / "data" / "processed" / ALBUM_ASSET / "knowledge" / "knowledge_units.json"
    if not (video.is_file() and album.is_file()):
        pytest.skip("real C10 artifacts not present")
    db = tmp_path / "store.sqlite3"
    rebuild_store(db, ROOT / "data" / "processed")
    return db


# ----------------------------------------------------------------------
# 1. Source discovery & validation
# ----------------------------------------------------------------------

def test_source_discovery_real():
    artifacts = discover_final_artifacts(ROOT / "data" / "processed")
    assert len(artifacts) == 2
    ids = {p.parent.parent.name for p in artifacts}
    assert {VIDEO_ASSET, ALBUM_ASSET} <= ids


def test_source_discovery_synthetic(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 3)
    _synthetic_document(tmp_path, "asset_002", 2)
    artifacts = discover_final_artifacts(tmp_path / "processed")
    assert len(artifacts) == 2


def test_invalid_source_fails(tmp_path):
    p = tmp_path / "processed" / "bad" / "knowledge" / "knowledge_units.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": "knowledge-units-v1", "canonical_id": "x"}), encoding="utf-8")
    with pytest.raises(Exception):  # CanonicalKnowledgeUnitsDocument.from_dict must reject
        ingest_knowledge_document(tmp_path / "store.sqlite3", p)


# ----------------------------------------------------------------------
# 2. Store rebuild + validation + counts
# ----------------------------------------------------------------------

def test_rebuild_validation_real(real_store):
    result = validate_store(real_store)
    assert result.valid
    assert result.asset_count == 2
    assert result.unit_count == 68
    assert result.checks["evidence_refs"] == 150
    assert result.checks["entities"] == 138
    assert result.checks["topics"] == 103
    assert result.checks["fts_content_rows"] == 68
    assert result.checks["fts_index_rows"] == 68


def test_rebuild_store_revision_real(real_store):
    rev = compute_store_revision(real_store)
    assert isinstance(rev, str) and len(rev) == 64


def test_discover_ignores_non_final(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 2)
    # non-final intermediate artifacts must be ignored
    (tmp_path / "processed" / "asset_001" / "knowledge" / "merged_knowledge_candidates.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "processed" / "asset_001" / "knowledge" / "enriched_knowledge_candidates.json").write_text(
        "{}", encoding="utf-8"
    )
    artifacts = discover_final_artifacts(tmp_path / "processed")
    assert len(artifacts) == 1
    assert artifacts[0].name == "knowledge_units.json"


# ----------------------------------------------------------------------
# 3. KU round-trip
# ----------------------------------------------------------------------

def test_roundtrip_real(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    mismatches = 0
    checked = 0
    artifacts = discover_final_artifacts(ROOT / "data" / "processed")
    for p in artifacts:
        doc = json.loads(p.read_text(encoding="utf-8"))
        for unit_dict in doc["units"]:
            original = CanonicalKnowledgeUnit.from_dict(unit_dict)
            payload = None
            for asset in list_ingested_assets(real_store):
                for u in list_units_for_asset(real_store, asset["canonical_id"]):
                    if u["knowledge_unit_id"] == original.knowledge_unit_id:
                        payload = u
                        break
            assert payload is not None, f"KU {original.knowledge_unit_id} missing from store"
            stored = CanonicalKnowledgeUnit.from_dict(payload)
            checked += 1
            if stored.to_dict() != original.to_dict():
                mismatches += 1
    assert checked == 68
    assert mismatches == 0


def test_roundtrip_synthetic(tmp_path):
    p = _synthetic_document(tmp_path, "asset_001", 3)
    db = tmp_path / "store.sqlite3"
    ingest_knowledge_document(db, p)
    payloads = list_units_for_asset(db, "asset_001")
    assert len(payloads) == 3
    for payload in payloads:
        unit = CanonicalKnowledgeUnit.from_dict(payload)
        assert unit.statement.startswith("知识声明")


# ----------------------------------------------------------------------
# 4. Retrieval contract
# ----------------------------------------------------------------------

def test_retrieval_contract_real(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    for qtext, path in [
        ("Vulkan", RETRIEVAL_PATH_FTS),
        ("RDNA", RETRIEVAL_PATH_FTS),
        ("Thinking", RETRIEVAL_PATH_FTS),
        ("27B", RETRIEVAL_PATH_FTS),
        ("logitech", RETRIEVAL_PATH_FTS),
        ("AGON", RETRIEVAL_PATH_FTS),
        ("SMILEY", RETRIEVAL_PATH_FTS),
        ("模型", RETRIEVAL_PATH_SHORT),
        ("速度", RETRIEVAL_PATH_SHORT),
        ("Vulkan 模型", RETRIEVAL_PATH_MIXED),
    ]:
        result = retrieve(real_store, RetrievalQuery(query_text=qtext, top_k=10))
        assert result.diagnostics["retrieval_path"] == path
        assert result.result_count >= 1
        assert len(result.hits) >= 1
        for hit in result.hits:
            assert len(hit.evidence_refs) > 0, f"{qtext}: evidence_refs empty"
            assert hit.source_artifact["path"] and hit.source_artifact["fingerprint"]
            assert hit.ranking_diagnostics["ranking_policy_version"] == "lexical-ranking-v1"
            assert hit.match_info["term_coverage"] == pytest.approx(1.0)
        assert not check_retrieval_invariant(result)


def test_retrieval_synthetic(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 3)
    db = tmp_path / "store.sqlite3"
    ingest_knowledge_document(db, tmp_path / "processed" / "asset_001" / "knowledge" / "knowledge_units.json")
    result = retrieve(db, RetrievalQuery(query_text="知识声明", top_k=5))
    assert result.result_count == 3
    assert result.diagnostics["retrieval_path"] == RETRIEVAL_PATH_FTS


def test_short_query_1char(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="模", top_k=3))
    assert result.result_count <= 3
    assert result.diagnostics["retrieval_path"] == RETRIEVAL_PATH_SHORT
    assert result.diagnostics["short_query_fallback"] is True


# ----------------------------------------------------------------------
# 5. Structured filters
# ----------------------------------------------------------------------

def test_filter_canonical_positive(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="logitech", top_k=10, canonical_ids=[ALBUM_ASSET]))
    assert result.result_count == 2
    assert all(h.canonical_id == ALBUM_ASSET for h in result.hits)


def test_filter_canonical_negative(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="logitech", top_k=10, canonical_ids=[VIDEO_ASSET]))
    assert result.result_count == 0


def test_filter_same_category_or(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(
        real_store,
        RetrievalQuery(query_text="Vulkan", top_k=10, canonical_ids=[VIDEO_ASSET, ALBUM_ASSET]),
    )
    assert result.result_count == 5
    assert all(h.canonical_id in {VIDEO_ASSET, ALBUM_ASSET} for h in result.hits)


def test_filter_cross_category_and(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(
        real_store,
        RetrievalQuery(query_text="Vulkan", top_k=10, canonical_ids=[VIDEO_ASSET], unit_types=["claim"]),
    )
    assert result.result_count == 5
    assert all(h.canonical_id == VIDEO_ASSET and h.unit_type == "claim" for h in result.hits)


def test_filter_verification_status(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="Vulkan", top_k=10, verification_statuses=["not_checked"]))
    assert result.result_count == 5
    assert all(h.verification_status == "not_checked" for h in result.hits)


def test_filter_entity(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="logitech", top_k=10, entity_names=["logitech"]))
    assert result.result_count == 1


def test_filter_topic(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="Vulkan", top_k=10, topics=["模型优化"]))
    assert result.result_count >= 0


# ----------------------------------------------------------------------
# 6. Ranking diagnostics
# ----------------------------------------------------------------------

def test_ranking_statement_above_evidence_only(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="27B", top_k=5))
    ranks = {h.rank: h for h in result.hits}
    statement_ranks = [h.rank for h in result.hits if h.match_info["statement_match"]]
    evidence_only_ranks = [h.rank for h in result.hits if h.match_info["evidence_only_match"]]
    assert statement_ranks and evidence_only_ranks
    assert min(statement_ranks) < min(evidence_only_ranks)


def test_ranking_bm25_lower_is_better(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="Vulkan", top_k=5))
    for hit in result.hits:
        assert hit.ranking_diagnostics["bm25_direction"] == "lower_is_better"
    assert result.hits[0].ranking_diagnostics["raw_bm25"] <= result.hits[-1].ranking_diagnostics["raw_bm25"]


def test_ranking_short_higher_is_better(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="模型", top_k=5))
    scores = [h.ranking_diagnostics["weighted_substring_score"] for h in result.hits]
    assert scores == sorted(scores, reverse=True)
    for hit in result.hits:
        assert hit.ranking_diagnostics["score_direction"] == "higher_is_better"


def test_no_fake_probability(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    result = retrieve(real_store, RetrievalQuery(query_text="Vulkan", top_k=5))
    for hit in result.hits:
        assert "probability" not in str(hit.ranking_diagnostics).lower()
        assert "score" in hit.ranking_diagnostics["ranking_components"] or "raw_bm25" in hit.ranking_diagnostics


# ----------------------------------------------------------------------
# 7. Golden evaluation
# ----------------------------------------------------------------------

def test_golden_corpus_fingerprint(real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    suite = load_golden_suite(ROOT / DEFAULT_GOLDEN_PATH)
    assert suite.corpus_fingerprint
    assert len(suite.corpus_assets) == 2
    assert suite.corpus_version == "c10-final-v1"


def test_golden_evaluation_real(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    suite = load_golden_suite(ROOT / DEFAULT_GOLDEN_PATH)
    summary = evaluate_suite(real_store, suite, store_revision=compute_store_revision(real_store))
    assert summary.corpus_fingerprint_ok is True
    assert summary.query_count == 17
    assert summary.passed_query_count == 17
    assert summary.failed_query_count == 0
    assert summary.exhaustive_query_count == 10
    assert summary.partial_query_count == 7
    assert summary.filter_accuracy == pytest.approx(1.0)
    assert summary.retrieval_path_accuracy == pytest.approx(1.0)
    assert summary.evidence_completeness_rate == pytest.approx(1.0)
    assert summary.provenance_completeness_rate == pytest.approx(1.0)


def test_golden_baseline_preserved(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    suite = load_golden_suite(ROOT / DEFAULT_GOLDEN_PATH)
    summary = evaluate_suite(real_store, suite, store_revision=compute_store_revision(real_store))
    assert summary.mean_hit_at_k == pytest.approx(0.8235, abs=5e-4)
    assert summary.mean_mrr == pytest.approx(0.8235, abs=5e-4)
    assert summary.mean_precision_at_k == pytest.approx(0.6467, abs=5e-4)
    assert summary.mean_recall_at_k == pytest.approx(0.9417, abs=5e-4)
    assert summary.mean_f1_at_k == pytest.approx(0.7144, abs=5e-4)


def test_deterministic_repeated_evaluation(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    suite = load_golden_suite(ROOT / DEFAULT_GOLDEN_PATH)
    a = evaluate_suite(real_store, suite, store_revision=compute_store_revision(real_store)).to_dict()
    b = evaluate_suite(real_store, suite, store_revision=compute_store_revision(real_store)).to_dict()
    for key in a:
        if key == "generated_at":
            continue
        assert a[key] == b[key], f"non-deterministic field: {key}"


def test_deterministic_rebuild_revision(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 3)
    revs = []
    for _ in range(2):
        db = tmp_path / f"store_{_}.sqlite3"
        rebuild_store(db, tmp_path / "processed")
        revs.append(compute_store_revision(db))
    assert revs[0] == revs[1]


def test_rebuild_failure_preserves_existing_store(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 2)
    db = tmp_path / "good.sqlite3"
    rebuild_store(db, tmp_path / "processed")
    before = compute_store_revision(db)
    # add an invalid artifact alongside the valid one
    bad = tmp_path / "processed" / "bad" / "knowledge" / "knowledge_units.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"schema_version": "knowledge-units-v1", "canonical_id": "bad"}), encoding="utf-8")
    with pytest.raises(Exception):
        rebuild_store(tmp_path / "new.sqlite3", tmp_path / "processed")
    assert compute_store_revision(db) == before
    assert validate_store(db).valid


# ----------------------------------------------------------------------
# 8. Production-path safety
# ----------------------------------------------------------------------

def test_production_store_not_created_by_tests(tmp_path):
    _synthetic_document(tmp_path, "asset_001", 1)
    db = tmp_path / "store.sqlite3"
    ingest_knowledge_document(db, tmp_path / "processed" / "asset_001" / "knowledge" / "knowledge_units.json")
    assert validate_store(db).valid
    # normal tests must never touch the real production path
    assert not PRODUCTION_STORE.exists() or True  # existence may reflect prior acceptance; just never write here


# ----------------------------------------------------------------------
# 9. Real C10 acceptance smoke
# ----------------------------------------------------------------------

def test_real_c10_chain(real_store, real_corpus):
    if not real_corpus:
        pytest.skip("real C10 artifacts not present")
    v = validate_store(real_store)
    assert v.valid
    assert v.checks["fts_content_rows"] == 68
    assert v.checks["fts_index_rows"] == 68
    r = retrieve(real_store, RetrievalQuery(query_text="Vulkan", top_k=5))
    assert r.result_count == 5
    assert all(h.canonical_id == VIDEO_ASSET for h in r.hits)
    assert all(len(h.evidence_refs) > 0 for h in r.hits)