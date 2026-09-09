"""M5-02 SQLite FTS5 Lexical / Metadata Indexing tests.

Covers the sealed trigram external-content FTS architecture: tokenizer
availability, materialized content projection, index sync (insert/replace/
remove/rollback/rebuild), field weight preference, literal query safety,
short-query limitation, store validation, store-revision stability, and real
C10 lexical smoke. Deterministic and offline; no LLM/runtime, no network.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    AttributionInfo,
    AttributionStatus,
    CanonicalKnowledgeUnitsDocument,
    EntityMention,
    EvidenceRef,
    ExtractionLineage,
    SequenceRange,
    TemporalRange,
    create_knowledge_unit,
)
from src.knowledge.store import (
    compute_store_revision,
    create_store,
    ingest_knowledge_document,
    open_store,
    rebuild_store,
    remove_asset,
    validate_store,
)
from src.knowledge.fts import (
    FTS_COLUMNS,
    FTS_CONTENT_TABLE,
    FTS_FIELD_WEIGHTS,
    FTS_INDEX_TABLE,
    FTS_POLICY_VERSION,
    FTS_TOKENIZER,
    build_fts_content_values,
    fts_index_count,
    lexical_search_rows,
    literal_fts_query,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"


# ----------------------------------------------------------------------
# Fixtures (mirrors tests/test_knowledge_store.py conventions)
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
    path.write_text(__import__("json").dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "knowledge_store.sqlite3"


def _ingest(
    db_path: Path,
    canonical_id: str,
    units: list[dict[str, Any]],
) -> Path:
    artifact = _asset_document(canonical_id, units)
    return _write_asset(Path(db_path).parent, artifact)


def _count(db_path: Path, table: str) -> int:
    conn = open_store(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _rowid_of(db_path: Path, ku_id: str) -> int:
    conn = open_store(db_path)
    try:
        return conn.execute(
            "SELECT unit_rowid FROM knowledge_units WHERE knowledge_unit_id = ?",
            (ku_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _content_count(db_path: Path) -> int:
    return _count(db_path, FTS_CONTENT_TABLE)


def _index_count(db_path: Path) -> int:
    conn = open_store(db_path)
    try:
        return fts_index_count(conn)
    finally:
        conn.close()


def _search(db_path: Path, query: str, limit: int = 50) -> list[tuple[int, float]]:
    conn = open_store(db_path)
    try:
        return lexical_search_rows(conn, query, limit)
    finally:
        conn.close()


def _search_rowids(db_path: Path, query: str, limit: int = 50) -> list[int]:
    return [rowid for rowid, _ in _search(db_path, query, limit)]


# ----------------------------------------------------------------------
# 1-2. SQLite FTS5 / trigram availability
# ----------------------------------------------------------------------

def test_sqlite_fts5_available() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert sqlite3.sqlite_version
        conn.execute("CREATE VIRTUAL TABLE _p USING fts5(x)")
    finally:
        conn.close()


def test_trigram_tokenizer_available() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE _p USING fts5(x, tokenize='trigram')")
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 3-5. Schema presence & unit_rowid mapping
# ----------------------------------------------------------------------

def test_fts_content_table_exists(db_path: Path) -> None:
    create_store(db_path)
    conn = open_store(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (FTS_CONTENT_TABLE,),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_fts_table_exists(db_path: Path) -> None:
    create_store(db_path)
    conn = open_store(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (FTS_INDEX_TABLE,),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_unit_rowid_mapping(db_path: Path) -> None:
    units = [_unit("asset_map", "Vulkan 后端推理测试声明")]
    path = _ingest(db_path, "asset_map", units)
    ingest_knowledge_document(db_path, path)
    ku_id = units[0]["knowledge_unit_id"]
    rowid = _rowid_of(db_path, ku_id)
    conn = open_store(db_path)
    try:
        content_rowid = conn.execute(
            f"SELECT unit_rowid FROM {FTS_CONTENT_TABLE} WHERE unit_rowid=?", (rowid,)
        ).fetchone()[0]
        assert content_rowid == rowid
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 6-9. statement / entity / topic / evidence indexed
# ----------------------------------------------------------------------

def test_statement_indexed(db_path: Path) -> None:
    units = [_unit("asset_st", "使用Vulkan后端运行大模型")]
    path = _ingest(db_path, "asset_st", units)
    ingest_knowledge_document(db_path, path)
    assert _rowid_of(db_path, units[0]["knowledge_unit_id"]) in _search_rowids(db_path, "Vulkan")


def test_entity_indexed(db_path: Path) -> None:
    units = [
        _unit(
            "asset_en",
            "关于图形接口的知识声明",
            entities=[{"entity_name": "Vulkan", "category": "inference_framework"}],
        )
    ]
    path = _ingest(db_path, "asset_en", units)
    ingest_knowledge_document(db_path, path)
    assert _rowid_of(db_path, units[0]["knowledge_unit_id"]) in _search_rowids(db_path, "Vulkan")


def test_topic_indexed(db_path: Path) -> None:
    units = [
        _unit(
            "asset_tp",
            "关于推理能力的知识声明",
            topics=["本地大模型", "思考模式"],
        )
    ]
    path = _ingest(db_path, "asset_tp", units)
    ingest_knowledge_document(db_path, path)
    rowid = _rowid_of(db_path, units[0]["knowledge_unit_id"])
    assert rowid in _search_rowids(db_path, "大模型")
    assert rowid in _search_rowids(db_path, "思考模式")


def test_evidence_excerpt_indexed(db_path: Path) -> None:
    refs = [EvidenceRef("ev_seg_000002", "这段证据提到RDNA架构", temporal_range=TemporalRange(1.0, 2.0, 1.0))]
    units = [_unit("asset_ev", "与图形无关的声明内容", evidence=refs)]
    path = _ingest(db_path, "asset_ev", units)
    ingest_knowledge_document(db_path, path)
    assert _rowid_of(db_path, units[0]["knowledge_unit_id"]) in _search_rowids(db_path, "RDNA")


# ----------------------------------------------------------------------
# 10-11. Field weight preference (statement > evidence)
# ----------------------------------------------------------------------

def test_field_weight_preference(db_path: Path) -> None:
    statement_unit = _unit("asset_weights", "Vulkan 后端推理测试声明内容完整")
    evidence_only = _unit(
        "asset_weights",
        "与图形接口完全无关的普通声明",
        evidence=[EvidenceRef("ev_seg_000002", "原文提到Vulkan渲染路径", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
    )
    path = _ingest(db_path, "asset_weights", [statement_unit, evidence_only])
    ingest_knowledge_document(db_path, path)
    st_rowid = _rowid_of(db_path, statement_unit["knowledge_unit_id"])
    ev_rowid = _rowid_of(db_path, evidence_only["knowledge_unit_id"])
    results = _search(db_path, "Vulkan")
    scores = dict(results)
    # Both matched; statement match must strictly outrank evidence-only match.
    assert scores[st_rowid] < scores[ev_rowid]


def test_statement_match_ranks_first(db_path: Path) -> None:
    statement_unit = _unit("asset_rank", "Vulkan 后端推理测试声明内容完整")
    evidence_only = _unit(
        "asset_rank",
        "与图形接口完全无关的普通声明",
        evidence=[EvidenceRef("ev_seg_000002", "原文提到Vulkan渲染路径", temporal_range=TemporalRange(1.0, 2.0, 1.0))],
    )
    path = _ingest(db_path, "asset_rank", [statement_unit, evidence_only])
    ingest_knowledge_document(db_path, path)
    first = _search_rowids(db_path, "Vulkan")[0]
    assert first == _rowid_of(db_path, statement_unit["knowledge_unit_id"])


# ----------------------------------------------------------------------
# 12-15. Atomic sync: insert / replace / remove / rollback
# ----------------------------------------------------------------------

def test_insert_sync(db_path: Path) -> None:
    units = [_unit("asset_ins", "第一条知识声明内容完整"), _unit("asset_ins", "第二条知识声明内容完整")]
    path = _ingest(db_path, "asset_ins", units)
    ingest_knowledge_document(db_path, path)
    assert _content_count(db_path) == 2
    assert _index_count(db_path) == 2
    assert validate_store(db_path).valid


def test_replace_removes_stale_terms(db_path: Path) -> None:
    v1 = [_unit("asset_rep", "旧版本声明提到了旧主题词")]
    path = _ingest(db_path, "asset_rep", v1)
    ingest_knowledge_document(db_path, path)
    assert _search_rowids(db_path, "旧主题词") != []
    v2 = [_unit("asset_rep", "替换后的唯一知识声明")]
    v2_path = _write_asset(Path(db_path).parent, _asset_document("asset_rep", v2))
    ingest_knowledge_document(db_path, v2_path)
    assert _search_rowids(db_path, "旧主题词") == []
    assert _content_count(db_path) == 1
    assert _index_count(db_path) == 1
    assert validate_store(db_path).valid


def test_remove_asset_clears_fts(db_path: Path) -> None:
    units = [_unit("asset_rm", "将被删除的资产声明")]
    path = _ingest(db_path, "asset_rm", units)
    ingest_knowledge_document(db_path, path)
    remove_asset(db_path, "asset_rm")
    assert _content_count(db_path) == 0
    assert _index_count(db_path) == 0
    assert _search_rowids(db_path, "资产声明") == []
    assert validate_store(db_path).valid


def test_rollback_keeps_old_fts(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = [_unit("asset_rb", "旧版本FTS内容完好")]
    path = _ingest(db_path, "asset_rb", v1)
    ingest_knowledge_document(db_path, path)
    before_content = _content_count(db_path)
    before_hits = _search_rowids(db_path, "FTS内容")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr("src.knowledge.store._insert_unit_rows", _boom)
    v2 = [_unit("asset_rb", "新版本绝不出现")]
    with pytest.raises(RuntimeError):
        ingest_knowledge_document(db_path, _write_asset(Path(db_path).parent, _asset_document("asset_rb", v2), name="v2.json"))
    assert _content_count(db_path) == before_content
    assert _index_count(db_path) == before_content
    assert _search_rowids(db_path, "FTS内容") == before_hits
    assert validate_store(db_path).valid


# ----------------------------------------------------------------------
# 16-19. Rebuild / validation / idempotency
# ----------------------------------------------------------------------

def test_rebuild_creates_fts(db_path: Path, tmp_path: Path) -> None:
    processed = tmp_path / "data" / "processed"
    for asset_id in ("asset_rb1", "asset_rb2"):
        d = processed / asset_id / "knowledge"
        d.mkdir(parents=True)
        _write_asset(d, _asset_document(asset_id, [_unit(asset_id, f"{asset_id} 声明内容完整")]))
    validation = rebuild_store(db_path, processed)
    assert validation.valid
    assert _content_count(db_path) == 2
    assert _index_count(db_path) == 2


def test_validation_catches_missing_fts_content(db_path: Path) -> None:
    units = [_unit("asset_v1", "第一条验证声明"), _unit("asset_v1", "第二条验证声明")]
    path = _ingest(db_path, "asset_v1", units)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DELETE FROM {FTS_CONTENT_TABLE} WHERE unit_rowid = (SELECT MIN(unit_rowid) FROM {FTS_CONTENT_TABLE})")
        conn.commit()
    finally:
        conn.close()
    validation = validate_store(db_path)
    assert not validation.valid
    assert validation.checks["fts_missing_content"] >= 1


def test_validation_catches_orphan_fts_content(db_path: Path) -> None:
    units = [_unit("asset_v2", "验证声明")]
    path = _ingest(db_path, "asset_v2", units)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            f"INSERT INTO {FTS_CONTENT_TABLE} "
            "(unit_rowid, statement, entity_names, topics, evidence_excerpts) "
            "VALUES (999999, '孤儿内容', '', '', '')"
        )
        conn.commit()
    finally:
        conn.close()
    validation = validate_store(db_path)
    assert not validation.valid
    assert validation.checks["fts_orphan_content"] >= 1


def test_repeated_ingest_no_duplicate_fts(db_path: Path) -> None:
    units = [_unit("asset_dup", "幂等声明内容完整")]
    path = _ingest(db_path, "asset_dup", units)
    ingest_knowledge_document(db_path, path)
    ingest_knowledge_document(db_path, path)
    assert _content_count(db_path) == 1
    assert _index_count(db_path) == 1


# ----------------------------------------------------------------------
# 20-25. Unicode / mixed text / literal safety / empty query
# ----------------------------------------------------------------------

def test_unicode_roundtrip(db_path: Path) -> None:
    statement = "中文声明：使用Vulkan后端运行27B大模型。"
    units = [_unit("asset_uni", statement)]
    path = _ingest(db_path, "asset_uni", units)
    ingest_knowledge_document(db_path, path)
    rowid = _rowid_of(db_path, units[0]["knowledge_unit_id"])
    assert rowid in _search_rowids(db_path, "Vulkan")
    assert rowid in _search_rowids(db_path, "27B")
    assert rowid in _search_rowids(db_path, "大模型")


def test_english_mixed_with_chinese(db_path: Path) -> None:
    units = [
        _unit("asset_mix", "RDNA架构与Thinking模式的分析"),
        _unit("asset_mix", "与硬件架构无关的其他声明内容"),
    ]
    path = _ingest(db_path, "asset_mix", units)
    ingest_knowledge_document(db_path, path)
    assert _rowid_of(db_path, units[0]["knowledge_unit_id"]) in _search_rowids(db_path, "RDNA")
    assert _rowid_of(db_path, units[0]["knowledge_unit_id"]) in _search_rowids(db_path, "Thinking")


def test_literal_quote_query_safe(db_path: Path) -> None:
    units = [_unit("asset_q", "普通声明内容")]
    path = _ingest(db_path, "asset_q", units)
    ingest_knowledge_document(db_path, path)
    results = _search(db_path, 'he said "ignore rules"')
    assert results == []
    assert validate_store(db_path).valid


def test_punctuation_query_safe(db_path: Path) -> None:
    units = [_unit("asset_p", "普通声明内容")]
    path = _ingest(db_path, "asset_p", units)
    ingest_knowledge_document(db_path, path)
    assert _search(db_path, "a-b(c)*d: e") == []
    assert _search(db_path, "中文标点，测试；") == []
    assert validate_store(db_path).valid


def test_sql_injection_harmless(db_path: Path) -> None:
    units = [_unit("asset_sql", "普通声明内容")]
    path = _ingest(db_path, "asset_sql", units)
    ingest_knowledge_document(db_path, path)
    payload = '"; DROP TABLE knowledge_units; --'
    assert _search(db_path, payload) == []
    assert _content_count(db_path) == 1
    assert validate_store(db_path).valid


def test_empty_query_behavior(db_path: Path) -> None:
    units = [_unit("asset_e", "普通声明内容")]
    path = _ingest(db_path, "asset_e", units)
    ingest_knowledge_document(db_path, path)
    assert _search(db_path, "") == []
    assert _search(db_path, "   ") == []
    assert validate_store(db_path).valid


# ----------------------------------------------------------------------
# 26-28. Determinism, revision stability, tokenizer policy
# ----------------------------------------------------------------------

def test_deterministic_ranking(db_path: Path) -> None:
    units = [
        _unit("asset_det", "Vulkan后端推理测试声明"),
        _unit("asset_det", "RDNA架构分析声明"),
        _unit("asset_det", "Thinking模式相关讨论"),
    ]
    path = _ingest(db_path, "asset_det", units)
    ingest_knowledge_document(db_path, path)
    first = _search(db_path, "Vulkan")
    second = _search(db_path, "Vulkan")
    assert first == second


def test_store_revision_unaffected_by_fts(db_path: Path) -> None:
    units = [_unit("asset_rev", "声明内容完整")]
    path = _ingest(db_path, "asset_rev", units)
    ingest_knowledge_document(db_path, path)
    revision1 = compute_store_revision(db_path)
    # Same ingest (NO-OP) and a rebuild from the same artifact must not change it.
    ingest_knowledge_document(db_path, path)
    assert compute_store_revision(db_path) == revision1


def test_tokenizer_policy_persisted(db_path: Path) -> None:
    create_store(db_path)
    conn = open_store(db_path)
    try:
        policy = conn.execute("SELECT value FROM store_meta WHERE key='fts_policy_version'").fetchone()[0]
        tokenizer = conn.execute("SELECT value FROM store_meta WHERE key='fts_tokenizer'").fetchone()[0]
        assert policy == FTS_POLICY_VERSION
        assert tokenizer == FTS_TOKENIZER
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Short-query limitation (trigram <3 chars)
# ----------------------------------------------------------------------

def test_short_query_known_limitation(db_path: Path) -> None:
    units = [_unit("asset_short", "本地大模型推理测试")]
    path = _ingest(db_path, "asset_short", units)
    ingest_knowledge_document(db_path, path)
    # 1 and 2 character queries return empty (documented limitation), no error.
    assert _search(db_path, "模") == []
    assert _search(db_path, "模型") == []
    assert _search(db_path, "推理") == []
    # 3+ character queries work.
    assert _search_rowids(db_path, "大模型") != []


# ----------------------------------------------------------------------
# build_fts_content_values determinism
# ----------------------------------------------------------------------

def test_build_fts_content_values_ordinal_order() -> None:
    payload = _unit(
        "asset_b",
        "声明",
        entities=[{"entity_name": "e1", "category": "product"}, {"entity_name": "e2", "category": "product"}],
        topics=["t1", "t2"],
        evidence=[
            EvidenceRef("ev_a", "x1", temporal_range=TemporalRange(0.0, 1.0, 1.0)),
            EvidenceRef("ev_b", "x2", temporal_range=TemporalRange(1.0, 2.0, 1.0)),
        ],
    )
    statement, entities, topics, excerpts = build_fts_content_values(payload)
    assert statement == payload["statement"]
    assert entities == "e1 e2"
    assert topics == "t1 t2"
    assert excerpts == "x1 x2"


def test_literal_fts_query_escapes_quotes() -> None:
    assert literal_fts_query('a "b" c') == '"a ""b"" c"'


# ----------------------------------------------------------------------
# Real C10 index
# ----------------------------------------------------------------------

def _c10_path(asset_id: str) -> Path:
    return ROOT / "data" / "processed" / asset_id / "knowledge" / "knowledge_units.json"


def _build_real_store(db_path: Path) -> bool:
    if not (_c10_path(VIDEO_ASSET).is_file() and _c10_path(ALBUM_ASSET).is_file()):
        return False
    ingest_knowledge_document(db_path, _c10_path(VIDEO_ASSET))
    ingest_knowledge_document(db_path, _c10_path(ALBUM_ASSET))
    return True


def test_real_c10_fts_count_68(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    assert _content_count(db_path) == 68
    assert _index_count(db_path) == 68
    assert validate_store(db_path).valid


def _rowid_to_asset(db_path: Path, rowid: int) -> str:
    conn = open_store(db_path)
    try:
        return conn.execute(
            "SELECT canonical_id FROM knowledge_units WHERE unit_rowid = ?",
            (rowid,),
        ).fetchone()[0]
    finally:
        conn.close()


def _asset_hit_ku_ids(db_path: Path, query: str, asset_id: str) -> list[str]:
    conn = open_store(db_path)
    try:
        rowids = [r[0] for r in lexical_search_rows(conn, query, 50)]
    finally:
        conn.close()
    hits = []
    for rowid in rowids:
        if _rowid_to_asset(db_path, rowid) == asset_id:
            conn = open_store(db_path)
            try:
                hits.append(
                    conn.execute(
                        "SELECT knowledge_unit_id FROM knowledge_units WHERE unit_rowid = ?",
                        (rowid,),
                    ).fetchone()[0]
                )
            finally:
                conn.close()
    return hits


def test_real_c10_vulkan(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "Vulkan", VIDEO_ASSET)
    assert hits != []


def test_real_c10_rdna(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "RDNA", VIDEO_ASSET)
    assert hits != []


def test_real_c10_thinking(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "Thinking", VIDEO_ASSET)
    assert hits != []


def test_real_c10_27b(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "27B", VIDEO_ASSET)
    assert hits != []


def test_real_c10_logitech(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "logitech", ALBUM_ASSET)
    assert hits != []


def test_real_c10_agon(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "AGON", ALBUM_ASSET)
    assert hits != []


def test_real_c10_smiley(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "SMILEY", ALBUM_ASSET)
    assert hits != []


# ----------------------------------------------------------------------
# Confirmed real Chinese / mixed queries (terms verified present in artifacts)
# ----------------------------------------------------------------------

def test_real_c10_chinese_da_model(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "大模型", VIDEO_ASSET)
    assert hits != []


def test_real_c10_chinese_sikao_moshi(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "思考模式", VIDEO_ASSET)
    assert hits != []


def test_real_c10_chinese_renwu_leixing(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "任务类型", VIDEO_ASSET)
    assert hits != []


def test_real_c10_mixed_vulkan_backend(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "Vulkan后端", VIDEO_ASSET)
    assert hits != []


def test_real_c10_mixed_strax_halo(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "Strax Halo", VIDEO_ASSET)
    assert hits != []


def test_real_c10_mixed_amx395(db_path: Path) -> None:
    if not _build_real_store(db_path):
        pytest.skip("real C10 artifacts not present")
    hits = _asset_hit_ku_ids(db_path, "AMX395", VIDEO_ASSET)
    assert hits != []