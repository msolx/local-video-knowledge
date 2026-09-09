"""M5-01 Canonical Knowledge Store & Idempotent Ingestion tests.

Covers the sealed knowledge-store-v1 schema: create/open/version handling,
atomic ingestion, idempotency, deterministic replace, rollback isolation,
deletion semantics, store validation, store revision, rebuild, and real C10
ingestion. Deterministic and offline; no LLM/runtime, no FTS.
"""
from __future__ import annotations

import json
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
    UnitType,
    VerificationStatus,
    create_knowledge_unit,
)
from src.knowledge.store import (
    DEFAULT_STORE_PATH,
    STORE_SCHEMA_POLICY_VERSION,
    STORE_SCHEMA_VERSION,
    STORE_USER_VERSION,
    StoreError,
    StoreIngestError,
    StoreSchemaError,
    StoreValidationError,
    compute_source_artifact_fingerprint,
    compute_store_revision,
    create_store,
    discover_final_artifacts,
    get_ingested_asset,
    get_unit,
    ingest_knowledge_document,
    list_ingested_assets,
    list_units_for_asset,
    open_store,
    rebuild_store,
    remove_asset,
    validate_store,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"


# ----------------------------------------------------------------------
# Synthetic fixtures
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


@pytest.fixture
def v1_artifact() -> dict[str, Any]:
    return _asset_document(
        "asset_test_001",
        [
            _unit("asset_test_001", "第一条知识声明。"),
            _unit("asset_test_001", "第二条知识声明。"),
        ],
    )


@pytest.fixture
def v2_artifact() -> dict[str, Any]:
    return _asset_document(
        "asset_test_001",
        [_unit("asset_test_001", "替换后的唯一知识声明。")],
    )


# ----------------------------------------------------------------------
# 1-4. Create store / version / tables / incompatible schema
# ----------------------------------------------------------------------

def test_create_fresh_store(db_path: Path) -> None:
    create_store(db_path)
    assert db_path.is_file()
    conn = open_store(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "store_meta",
            "ingested_assets",
            "knowledge_units",
            "evidence_refs",
            "entities",
            "topics",
        }.issubset(tables)
    finally:
        conn.close()


def test_user_version(db_path: Path) -> None:
    create_store(db_path)
    conn = open_store(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STORE_USER_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_store_meta_records_schema(db_path: Path) -> None:
    create_store(db_path)
    conn = open_store(db_path)
    try:
        version = conn.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()[0]
        policy = conn.execute(
            "SELECT value FROM store_meta WHERE key='schema_policy_version'"
        ).fetchone()[0]
        assert version == STORE_SCHEMA_VERSION
        assert policy == STORE_SCHEMA_POLICY_VERSION
    finally:
        conn.close()


def test_incompatible_schema_rejected(db_path: Path) -> None:
    create_store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StoreSchemaError):
        open_store(db_path)


def test_open_store_initializes_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    conn = open_store(empty)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STORE_USER_VERSION
    finally:
        conn.close()


def test_open_store_rejects_tables_without_version(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.sqlite3"
    conn = sqlite3.connect(bogus)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StoreSchemaError):
        create_store(bogus)


# ----------------------------------------------------------------------
# 5-14. Ingestion correctness & round-trip
# ----------------------------------------------------------------------

def test_ingest_valid_document(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    result = ingest_knowledge_document(db_path, path)
    assert result.status == "inserted"
    assert result.unit_count == 2
    validation = validate_store(db_path)
    assert validation.valid
    assert validation.asset_count == 1
    assert validation.unit_count == 2


def test_ingest_invalid_document_rejected(db_path: Path) -> None:
    artifact = _asset_document("asset_test_002", [])
    artifact["units"] = [{"not": "a valid unit"}]
    path = _write_asset(Path(db_path).parent, artifact)
    with pytest.raises(StoreIngestError):
        ingest_knowledge_document(db_path, path)
    assert get_ingested_asset(db_path, "asset_test_002") is None


def test_ingest_rejects_unsupported_schema(db_path: Path) -> None:
    artifact = _asset_document("asset_test_003", [_unit("asset_test_003", "x")])
    artifact["schema_version"] = "knowledge-units-v9"
    path = _write_asset(Path(db_path).parent, artifact)
    with pytest.raises(StoreIngestError):
        ingest_knowledge_document(db_path, path)


def test_asset_metadata_recorded(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    meta = get_ingested_asset(db_path, "asset_test_001")
    assert meta is not None
    assert meta["canonical_id"] == "asset_test_001"
    assert meta["knowledge_schema_version"] == KNOWLEDGE_SCHEMA_VERSION
    assert meta["source_artifact_fingerprint"] == compute_source_artifact_fingerprint(v1_artifact)
    assert meta["unit_count"] == 2
    assert "ingested_at" in meta and meta["ingested_at"]
    assert meta["source_artifact_path"].endswith("knowledge_units.json")


def test_unit_canonical_payload_stored(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, v1_artifact["units"][0]["knowledge_unit_id"])
    assert unit == v1_artifact["units"][0]


def test_evidence_roundtrip_order(db_path: Path) -> None:
    refs = [
        EvidenceRef("ev_seg_000001", "证据A", temporal_range=TemporalRange(0.0, 1.0, 1.0)),
        EvidenceRef("ev_seg_000002", "证据B", temporal_range=TemporalRange(1.0, 2.0, 1.0)),
        EvidenceRef("ev_seg_000003", "证据C", temporal_range=TemporalRange(2.0, 3.0, 1.0)),
    ]
    artifact = _asset_document("asset_ev", [_unit("asset_ev", "三证据声明", evidence=refs)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert [r["evidence_id"] for r in unit["evidence_refs"]] == [
        "ev_seg_000001", "ev_seg_000002", "ev_seg_000003",
    ]
    assert [r["source_excerpt"] for r in unit["evidence_refs"]] == ["证据A", "证据B", "证据C"]


def test_entity_roundtrip_order(db_path: Path) -> None:
    entities = [
        {"entity_name": "Vulkan", "category": "inference_framework"},
        {"entity_name": "RDNA 3.5", "category": "hardware_architecture"},
    ]
    artifact = _asset_document(
        "asset_ent", [_unit("asset_ent", "Vulkan与RDNA声明", entities=entities)]
    )
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["entities"] == entities


def test_topic_roundtrip_order(db_path: Path) -> None:
    topics = ["端侧大模型", "统一内存", "qwen"]
    artifact = _asset_document("asset_top", [_unit("asset_top", "主题声明", topics=topics)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["topics"] == topics


def test_attribution_roundtrip(db_path: Path) -> None:
    unit = _unit("asset_attr", "属性声明")
    unit["attribution"] = {
        "source_actor_name": "姑妈有神王",
        "source_actor_id": "1295683635130569",
        "speaker_name": None,
        "speaker_id": None,
        "attribution_status": "visual_media",
    }
    artifact = _asset_document("asset_attr", [unit])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    stored = get_unit(db_path, unit["knowledge_unit_id"])
    assert stored["attribution"] == unit["attribution"]


def test_lineage_roundtrip(db_path: Path) -> None:
    unit = _unit("asset_lin", "血缘声明")
    unit["extraction_lineage"] = {
        "extraction_run_id": "run_3173da6b799ffb02",
        "input_chunk_ids": ["chk_000001", "chk_000002"],
        "candidate_id": "ku_abc",
        "source_candidate_ids": ["cand_a", "cand_b"],
        "merge_strategy": "dedup_exact",
    }
    artifact = _asset_document("asset_lin", [unit])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    stored = get_unit(db_path, unit["knowledge_unit_id"])
    assert stored["extraction_lineage"] == unit["extraction_lineage"]


def test_full_ku_semantic_roundtrip(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    units = list_units_for_asset(db_path, "asset_test_001")
    assert units == v1_artifact["units"]


# ----------------------------------------------------------------------
# 15-22. Idempotency, replace, rollback
# ----------------------------------------------------------------------

def test_same_fingerprint_noop(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    first = ingest_knowledge_document(db_path, path)
    second = ingest_knowledge_document(db_path, path)
    assert first.status == "inserted"
    assert second.status == "unchanged"
    assert first.ingested_at == second.ingested_at


def test_noop_preserves_ingested_at(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    before = get_ingested_asset(db_path, "asset_test_001")["ingested_at"]
    ingest_knowledge_document(db_path, path)
    after = get_ingested_asset(db_path, "asset_test_001")["ingested_at"]
    assert before == after


def test_repeated_ingest_no_duplicate_rows(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    ingest_knowledge_document(db_path, path)
    conn = open_store(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_refs").fetchone()[0] == 2
    finally:
        conn.close()


def test_changed_fingerprint_replace(db_path: Path, v1_artifact: dict[str, Any], v2_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    v2_path = _write_asset(Path(db_path).parent, v2_artifact)
    result = ingest_knowledge_document(db_path, v2_path)
    assert result.status == "replaced"
    units = list_units_for_asset(db_path, "asset_test_001")
    assert len(units) == 1
    assert units[0]["statement"] == "替换后的唯一知识声明。"


def test_replace_removes_stale_units_and_children(db_path: Path, v1_artifact: dict[str, Any], v2_artifact: dict[str, Any]) -> None:
    v1_units = v1_artifact["units"]
    v1_units[0]["entities"] = [{"entity_name": "StaleEntity", "category": "product"}]
    v1_units[0]["topics"] = ["旧主题"]
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    v2_path = _write_asset(Path(db_path).parent, v2_artifact)
    ingest_knowledge_document(db_path, v2_path)
    conn = open_store(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_refs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0
    finally:
        conn.close()


def test_replace_rollback_preserves_old_version(db_path: Path, v1_artifact: dict[str, Any], v2_artifact: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    # v2 is a valid document; a mid-transaction DB failure is simulated after
    # the old asset rows were deleted but before the new ones commit.

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr("src.knowledge.store._insert_unit_rows", _boom)
    with pytest.raises(RuntimeError):
        ingest_knowledge_document(db_path, _write_asset(Path(db_path).parent, v2_artifact, name="v2.json"))
    # Rollback must leave V1 fully intact: metadata AND units from V1, not a mix.
    units = list_units_for_asset(db_path, "asset_test_001")
    assert len(units) == 2
    assert units == v1_artifact["units"]
    meta = get_ingested_asset(db_path, "asset_test_001")
    assert meta["unit_count"] == 2
    assert meta["source_artifact_fingerprint"] == compute_source_artifact_fingerprint(v1_artifact)


# ----------------------------------------------------------------------
# 23-25. Removal
# ----------------------------------------------------------------------

def test_remove_asset(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    result = remove_asset(db_path, "asset_test_001")
    assert result["removed"] is True
    assert result["unit_count"] == 2
    assert get_ingested_asset(db_path, "asset_test_001") is None
    assert list_units_for_asset(db_path, "asset_test_001") == []
    conn = open_store(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence_refs").fetchone()[0] == 0
    finally:
        conn.close()


def test_remove_nonexistent_asset(db_path: Path) -> None:
    create_store(db_path)
    result = remove_asset(db_path, "does_not_exist")
    assert result["removed"] is False
    assert result["unit_count"] == 0


def test_remove_does_not_touch_source_file(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    source = _write_asset(Path(db_path).parent, v1_artifact)
    original = source.read_text(encoding="utf-8")
    ingest_knowledge_document(db_path, source)
    remove_asset(db_path, "asset_test_001")
    assert source.is_file()
    assert source.read_text(encoding="utf-8") == original


# ----------------------------------------------------------------------
# 26-32. Store validation
# ----------------------------------------------------------------------

def test_fk_integrity(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_no_orphan_rows(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    validation = validate_store(db_path)
    assert validation.checks["orphan_rows"] == 0
    assert validation.valid


def test_projection_consistency(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    validation = validate_store(db_path)
    assert validation.checks["projection_violations"] == 0
    assert validation.valid


def test_corrupted_projection_detected(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE knowledge_units SET statement='篡改的投影' "
            "WHERE knowledge_unit_id=?",
            (v1_artifact["units"][0]["knowledge_unit_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    validation = validate_store(db_path)
    assert not validation.valid
    assert validation.checks["projection_violations"] >= 1


def test_asset_unit_count_invariant(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ingested_assets SET unit_count=999 WHERE canonical_id=?",
            ("asset_test_001",),
        )
        conn.commit()
    finally:
        conn.close()
    validation = validate_store(db_path)
    assert not validation.valid
    assert validation.checks["asset_unit_count_mismatches"] >= 1


def test_invalid_ordinal_detected(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE evidence_refs SET ordinal=99 WHERE ordinal=0"
        )
        conn.commit()
    finally:
        conn.close()
    validation = validate_store(db_path)
    assert not validation.valid
    assert validation.checks["invalid_ordinals"] >= 1


def test_validation_success_report(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    validation = validate_store(db_path)
    assert validation.valid
    assert validation.schema_version == STORE_SCHEMA_VERSION
    assert validation.store_revision == compute_store_revision(db_path)


# ----------------------------------------------------------------------
# 33-39. Robustness: parameters, unicode, coords, zero-unit, multi-asset
# ----------------------------------------------------------------------

def test_parameterized_text_quotes_safe(db_path: Path) -> None:
    statement = "包含 '单引号' \"双引号\" 分号; 以及 -- 注释符号"
    artifact = _asset_document("asset_sql", [_unit("asset_sql", statement)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["statement"] == statement


def test_unicode_chinese_roundtrip(db_path: Path) -> None:
    statement = "中文声明：Vulkan 后端在 Strix Halo 上跑 27B 模型。"
    artifact = _asset_document("asset_uni", [_unit("asset_uni", statement)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["statement"] == statement


def test_multiline_excerpt_roundtrip(db_path: Path) -> None:
    excerpt = "第一行 logitech\n第二行 INAMAX\n第三行 AGON"
    refs = [EvidenceRef("ve_img_001", excerpt, sequence_range=SequenceRange(1))]
    artifact = _asset_document("asset_ml", [_unit("asset_ml", "多行证据", evidence=refs)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["evidence_refs"][0]["source_excerpt"] == excerpt


def test_both_temporal_and_sequence_roundtrip(db_path: Path) -> None:
    refs = [
        EvidenceRef(
            "ev_seg_000001", "时间证据",
            temporal_range=TemporalRange(101.58, 104.12, 2.54),
        ),
        EvidenceRef(
            "ve_img_001", "序列证据",
            sequence_range=SequenceRange(2),
        ),
    ]
    artifact = _asset_document("asset_coord", [_unit("asset_coord", "双坐标", evidence=refs)])
    path = _write_asset(Path(db_path).parent, artifact)
    ingest_knowledge_document(db_path, path)
    unit = get_unit(db_path, artifact["units"][0]["knowledge_unit_id"])
    assert unit["evidence_refs"][0]["temporal_range"] == {
        "start": 101.58, "end": 104.12, "duration": 2.54,
    }
    assert unit["evidence_refs"][1]["sequence_range"] == {"sequence_index": 2}


def test_zero_unit_document(db_path: Path) -> None:
    artifact = _asset_document("asset_zero", [])
    path = _write_asset(Path(db_path).parent, artifact)
    result = ingest_knowledge_document(db_path, path)
    assert result.status == "inserted"
    assert result.unit_count == 0
    validation = validate_store(db_path)
    assert validation.valid


def test_multiple_assets(db_path: Path) -> None:
    a1 = _asset_document("asset_multi_a", [_unit("asset_multi_a", "资产A声明")])
    a2 = _asset_document("asset_multi_b", [_unit("asset_multi_b", "资产B声明")])
    ingest_knowledge_document(db_path, _write_asset(Path(db_path).parent, a1, name="a.json"))
    ingest_knowledge_document(db_path, _write_asset(Path(db_path).parent, a2, name="b.json"))
    assert len(list_ingested_assets(db_path)) == 2
    assert validate_store(db_path).valid


def test_same_ku_id_ingest_stable(db_path: Path, v1_artifact: dict[str, Any]) -> None:
    # Re-ingesting the same artifact twice yields identical units.
    path = _write_asset(Path(db_path).parent, v1_artifact)
    ingest_knowledge_document(db_path, path)
    ingest_knowledge_document(db_path, path)
    assert list_units_for_asset(db_path, "asset_test_001") == v1_artifact["units"]


# ----------------------------------------------------------------------
# 40-43. Rebuild
# ----------------------------------------------------------------------

def test_rebuild_discovery_final_artifacts_only(tmp_path: Path) -> None:
    processed = tmp_path / "data" / "processed"
    asset_dir = processed / "asset_x" / "knowledge"
    asset_dir.mkdir(parents=True)
    (asset_dir / "knowledge_units.json").write_text("{}", encoding="utf-8")
    (asset_dir / "knowledge_candidates.json").write_text("{}", encoding="utf-8")
    (asset_dir / "merged_knowledge_candidates.json").write_text("{}", encoding="utf-8")
    (asset_dir / "enriched_knowledge_candidates.json").write_text("{}", encoding="utf-8")
    artifacts = discover_final_artifacts(processed)
    assert len(artifacts) == 1
    assert artifacts[0].name == "knowledge_units.json"


def test_rebuild_invalid_asset_failfast(db_path: Path, tmp_path: Path) -> None:
    processed = tmp_path / "data" / "processed"
    good_dir = processed / "asset_good" / "knowledge"
    good_dir.mkdir(parents=True)
    good = _asset_document("asset_good", [_unit("asset_good", "好资产")])
    _write_asset(good_dir, good)
    bad_dir = processed / "asset_bad" / "knowledge"
    bad_dir.mkdir(parents=True)
    (bad_dir / "knowledge_units.json").write_text('{"schema_version": "broken"', encoding="utf-8")
    with pytest.raises(StoreIngestError):
        rebuild_store(db_path, processed)
    # Old store preserved: nothing should have been created.
    assert not db_path.exists()


def test_rebuild_failure_preserves_old_store(db_path: Path, tmp_path: Path, v1_artifact: dict[str, Any]) -> None:
    good_dir = tmp_path / "data" / "processed" / "asset_good" / "knowledge"
    good_dir.mkdir(parents=True)
    good = _asset_document("asset_good", [_unit("asset_good", "好资产")])
    _write_asset(good_dir, good)
    rebuild_store(db_path, tmp_path / "data" / "processed")
    before = list_units_for_asset(db_path, "asset_good")
    # Now add an invalid asset and attempt a rebuild that must fail.
    bad_dir = tmp_path / "data" / "processed" / "asset_bad" / "knowledge"
    bad_dir.mkdir(parents=True)
    (bad_dir / "knowledge_units.json").write_text('{"schema_version": "nope"', encoding="utf-8")
    with pytest.raises(StoreIngestError):
        rebuild_store(db_path, tmp_path / "data" / "processed")
    assert list_units_for_asset(db_path, "asset_good") == before


def test_rebuild_success(db_path: Path, tmp_path: Path) -> None:
    processed = tmp_path / "data" / "processed"
    for asset_id in ("asset_r1", "asset_r2"):
        d = processed / asset_id / "knowledge"
        d.mkdir(parents=True)
        _write_asset(d, _asset_document(asset_id, [_unit(asset_id, f"{asset_id} 声明")]))
    validation = rebuild_store(db_path, processed)
    assert validation.valid
    assert validation.asset_count == 2
    assert validation.unit_count == 2
    assert get_ingested_asset(db_path, "asset_r1") is not None


# ----------------------------------------------------------------------
# 44-46. Real C10 ingestion
# ----------------------------------------------------------------------

def _c10_path(asset_id: str) -> Path:
    return ROOT / "data" / "processed" / asset_id / "knowledge" / "knowledge_units.json"


def _count_rows(db_path: Path) -> tuple[int, int, int, int]:
    conn = open_store(db_path)
    try:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("knowledge_units", "evidence_refs", "entities", "topics")
        )
    finally:
        conn.close()


def test_real_c10_video(db_path: Path) -> None:
    if not _c10_path(VIDEO_ASSET).is_file():
        pytest.skip("real C10 video artifact not present")
    result = ingest_knowledge_document(db_path, _c10_path(VIDEO_ASSET))
    assert result.status == "inserted"
    assert result.unit_count == 62
    assert len(list_units_for_asset(db_path, VIDEO_ASSET)) == 62
    assert validate_store(db_path).valid


def test_real_c10_album(db_path: Path) -> None:
    if not _c10_path(ALBUM_ASSET).is_file():
        pytest.skip("real C10 album artifact not present")
    result = ingest_knowledge_document(db_path, _c10_path(ALBUM_ASSET))
    assert result.status == "inserted"
    assert result.unit_count == 6
    assert len(list_units_for_asset(db_path, ALBUM_ASSET)) == 6
    assert validate_store(db_path).valid


def test_real_c10_combined_count_68(db_path: Path) -> None:
    if not (_c10_path(VIDEO_ASSET).is_file() and _c10_path(ALBUM_ASSET).is_file()):
        pytest.skip("real C10 artifacts not present")
    ingest_knowledge_document(db_path, _c10_path(VIDEO_ASSET))
    ingest_knowledge_document(db_path, _c10_path(ALBUM_ASSET))
    validation = validate_store(db_path)
    assert validation.asset_count == 2
    assert validation.unit_count == 68
    # Idempotency: second pass is unchanged and row counts are identical.
    r1 = _count_rows(db_path)
    ingest_knowledge_document(db_path, _c10_path(VIDEO_ASSET))
    ingest_knowledge_document(db_path, _c10_path(ALBUM_ASSET))
    r2 = _count_rows(db_path)
    assert r1 == r2
    assert r1 == (68, 150, 138, 103)
    assert get_ingested_asset(db_path, VIDEO_ASSET)["unit_count"] == 62
    assert get_ingested_asset(db_path, ALBUM_ASSET)["unit_count"] == 6