from __future__ import annotations

import json
import pytest

from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    UnitType,
    VerificationStatus,
    AttributionStatus,
    TemporalRange,
    SequenceRange,
    EvidenceRef,
    AttributionInfo,
    EntityMention,
    ExtractionProvenance,
    ExtractionLineage,
    CanonicalKnowledgeUnit,
    CanonicalKnowledgeUnitsDocument,
    normalize_statement,
    compute_knowledge_unit_id,
    create_knowledge_unit,
    validate_observation_grounding,
    adapt_legacy_point,
)


# ----------------------------------------------------------------------
# Helper Fixtures
# ----------------------------------------------------------------------

def sample_evidence_ref(eid: str = "ev_seg_000041") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=eid,
        source_excerpt="用主线Vulkan版本的引擎来跑27B",
        temporal_range=TemporalRange(start=101.58, end=104.12, duration=2.54),
        sequence_range=None,
    )


def sample_attribution() -> AttributionInfo:
    return AttributionInfo(
        source_actor_name="老林说",
        source_actor_id=None,
        speaker_name=None,
        speaker_id=None,
        attribution_status=AttributionStatus.UNVERIFIED_SPEAKER,
    )


def sample_lineage() -> ExtractionLineage:
    return ExtractionLineage(
        extraction_run_id="run_c10_001",
        input_chunk_ids=["chk_000001"],
        source_candidate_ids=["cand_chk1_001"],
        candidate_id="cand_chk1_001",
        merge_strategy=None,
    )


def sample_provenance() -> ExtractionProvenance:
    return ExtractionProvenance(
        backend="lm-studio",
        model="qwen2.5-7b-instruct",
        prompt_version="knowledge-extraction-v4.0",
        temperature=0.1,
        generated_at="2026-09-09T12:00:00Z",
        evidence_manifest_fingerprint="943bac2c855632f796ac0db6c918ae9531d72722e5b5cd8ca6221f2a16899738",
        evidence_chunks_fingerprint="5b4004cf45f8703bb743caca5c5b77a46d6fb0eee2505fb42e00e7b5d553717a",
        knowledge_schema_version=KNOWLEDGE_SCHEMA_VERSION,
    )


# ----------------------------------------------------------------------
# 1. Valid Unit Type Constructions
# ----------------------------------------------------------------------

def test_01_valid_claim_construction():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.CLAIM,
        statement="Vulkan后端在StructHalo上量化矩阵走的是通用算子",
        evidence_refs=[ref],
        attribution=sample_attribution(),
        extraction_confidence=0.95,
        extraction_lineage=sample_lineage(),
    )
    assert unit.unit_type == UnitType.CLAIM
    assert unit.knowledge_unit_id.startswith("ku_")
    assert len(unit.knowledge_unit_id) == 19
    assert unit.verification_status == VerificationStatus.NOT_CHECKED


def test_02_valid_opinion_construction():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.OPINION,
        statement="建议大家在评测时关注推理引擎与思考模式",
        evidence_refs=[ref],
        attribution=sample_attribution(),
        extraction_confidence=0.88,
        extraction_lineage=sample_lineage(),
    )
    assert unit.unit_type == UnitType.OPINION
    assert unit.attribution.attribution_status == AttributionStatus.UNVERIFIED_SPEAKER


def test_03_valid_observation_structural_model():
    img_ref = EvidenceRef(
        evidence_id="ve_img_001",
        source_excerpt="logitech\nINAMAX",
        temporal_range=None,
        sequence_range=SequenceRange(sequence_index=1),
    )
    unit = create_knowledge_unit(
        canonical_id="douyin_7682038498466993905",
        unit_type=UnitType.OBSERVATION,
        statement="第1张图包含 Logitech G 与 INAMAX 标识",
        evidence_refs=[img_ref],
        attribution=AttributionInfo(
            source_actor_name="姑妈有神王",
            attribution_status=AttributionStatus.VISUAL_MEDIA,
        ),
        extraction_confidence=0.99,
        extraction_lineage=sample_lineage(),
    )
    assert unit.unit_type == UnitType.OBSERVATION
    assert unit.attribution.attribution_status == AttributionStatus.VISUAL_MEDIA


def test_04_valid_procedure_step():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.PROCEDURE_STEP,
        statement="执行前先指定引擎后端参数 --engine vulkan",
        evidence_refs=[ref],
        attribution=sample_attribution(),
        extraction_confidence=0.91,
        extraction_lineage=sample_lineage(),
    )
    assert unit.unit_type == UnitType.PROCEDURE_STEP


# ----------------------------------------------------------------------
# 2. Verification Question Invariants
# ----------------------------------------------------------------------

def test_05_verification_question_requires_system_derived():
    ref = sample_evidence_ref()
    # Providing unverified_speaker should raise ValueError
    with pytest.raises(ValueError, match="system_derived"):
        create_knowledge_unit(
            canonical_id="douyin_7681603850364521734",
            unit_type=UnitType.VERIFICATION_QUESTION,
            statement="Vulkan后端在该架构上是否支持协同矩阵优化？",
            evidence_refs=[ref],
            attribution=sample_attribution(),  # has unverified_speaker
            extraction_confidence=0.85,
            extraction_lineage=sample_lineage(),
        )

    # Valid with system_derived
    valid_unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.VERIFICATION_QUESTION,
        statement="Vulkan后端在该架构上是否支持协同矩阵优化？",
        evidence_refs=[ref],
        attribution=AttributionInfo(attribution_status=AttributionStatus.SYSTEM_DERIVED),
        extraction_confidence=0.85,
        extraction_lineage=sample_lineage(),
    )
    assert valid_unit.unit_type == UnitType.VERIFICATION_QUESTION
    assert valid_unit.attribution.attribution_status == AttributionStatus.SYSTEM_DERIVED


def test_06_verification_question_requires_evidence():
    with pytest.raises(ValueError, match="at least 1 evidence_ref"):
        create_knowledge_unit(
            canonical_id="douyin_7681603850364521734",
            unit_type=UnitType.VERIFICATION_QUESTION,
            statement="空证据质询？",
            evidence_refs=[],
            attribution=AttributionInfo(attribution_status=AttributionStatus.SYSTEM_DERIVED),
            extraction_confidence=0.85,
            extraction_lineage=sample_lineage(),
        )


# ----------------------------------------------------------------------
# 3. Status and Range Validations
# ----------------------------------------------------------------------

def test_07_default_verification_status_not_checked():
    unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.CLAIM,
        statement="默认核验状态测试",
        evidence_refs=[sample_evidence_ref()],
        attribution=sample_attribution(),
        extraction_confidence=0.9,
        extraction_lineage=sample_lineage(),
    )
    assert unit.verification_status == VerificationStatus.NOT_CHECKED


def test_08_extraction_confidence_lower_bound():
    with pytest.raises(ValueError, match="extraction_confidence"):
        create_knowledge_unit(
            canonical_id="douyin_7681603850364521734",
            unit_type=UnitType.CLAIM,
            statement="测试置信度下限",
            evidence_refs=[sample_evidence_ref()],
            attribution=sample_attribution(),
            extraction_confidence=-0.01,
            extraction_lineage=sample_lineage(),
        )


def test_09_extraction_confidence_upper_bound():
    with pytest.raises(ValueError, match="extraction_confidence"):
        create_knowledge_unit(
            canonical_id="douyin_7681603850364521734",
            unit_type=UnitType.CLAIM,
            statement="测试置信度上限",
            evidence_refs=[sample_evidence_ref()],
            attribution=sample_attribution(),
            extraction_confidence=1.01,
            extraction_lineage=sample_lineage(),
        )


def test_10_invalid_unit_type_rejected():
    with pytest.raises(ValueError):
        UnitType("invalid_type")

    with pytest.raises(ValueError):
        CanonicalKnowledgeUnit.from_dict({
            "knowledge_unit_id": "ku_3e18a992cb412d09",
            "canonical_id": "douyin_123",
            "unit_type": "author_claim",  # author_claim is not a canonical enum!
            "statement": "test",
            "evidence_refs": [sample_evidence_ref().to_dict()],
            "attribution": sample_attribution().to_dict(),
            "extraction_confidence": 0.9,
            "verification_status": "not_checked",
            "entities": [],
            "topics": [],
            "extraction_lineage": sample_lineage().to_dict(),
        })


def test_11_invalid_verification_status_rejected():
    with pytest.raises(ValueError):
        VerificationStatus("bogus_status")


def test_12_invalid_attribution_status_rejected():
    with pytest.raises(ValueError):
        AttributionStatus("fake_attribution")


# ----------------------------------------------------------------------
# 4. EvidenceRef Contract & Range Serialization
# ----------------------------------------------------------------------

def test_13_evidence_ref_has_no_chunk_id():
    ref = sample_evidence_ref()
    d = ref.to_dict()
    assert "chunk_id" not in d

    # from_dict must reject chunk_id
    with pytest.raises(ValueError, match="chunk_id is strictly removed"):
        EvidenceRef.from_dict({
            "evidence_id": "ev_001",
            "source_excerpt": "quote",
            "chunk_id": "chk_000001",
        })


def test_14_evidence_ref_preserves_source_excerpt():
    excerpt = "用主线Vulkan版本的引擎来跑27B"
    ref = EvidenceRef(
        evidence_id="ev_seg_000041",
        source_excerpt=excerpt,
    )
    assert ref.source_excerpt == excerpt
    assert ref.to_dict()["source_excerpt"] == excerpt


def test_15_temporal_range_serialization():
    tr = TemporalRange(start=10.1234, end=20.5678, duration=10.4444)
    d = tr.to_dict()
    assert d == {"start": 10.123, "end": 20.568, "duration": 10.444}
    tr_rt = TemporalRange.from_dict(d)
    assert tr_rt.start == 10.123
    assert tr_rt.end == 20.568


def test_16_sequence_range_serialization():
    sr = SequenceRange(sequence_index=2)
    d = sr.to_dict()
    assert d == {"sequence_index": 2}
    sr_rt = SequenceRange.from_dict(d)
    assert sr_rt.sequence_index == 2


def test_16b_evidenceref_multidimensional_coordinates():
    # Case A: temporal only (e.g. ASR speech)
    case_a = EvidenceRef(
        evidence_id="ev_speech_01",
        source_excerpt="speech excerpt",
        temporal_range=TemporalRange(start=10.0, end=15.0, duration=5.0),
        sequence_range=None,
    )
    d_a = case_a.to_dict()
    assert d_a["temporal_range"] == {"start": 10.0, "end": 15.0, "duration": 5.0}
    assert d_a["sequence_range"] is None
    assert EvidenceRef.from_dict(d_a) == case_a

    # Case B: sequence only (e.g. image album OCR)
    case_b = EvidenceRef(
        evidence_id="ev_album_01",
        source_excerpt="album excerpt",
        temporal_range=None,
        sequence_range=SequenceRange(sequence_index=3),
    )
    d_b = case_b.to_dict()
    assert d_b["temporal_range"] is None
    assert d_b["sequence_range"] == {"sequence_index": 3}
    assert EvidenceRef.from_dict(d_b) == case_b

    # Case C: neither coordinate (e.g. web/forum/document evidence)
    case_c = EvidenceRef(
        evidence_id="ev_doc_01",
        source_excerpt="doc excerpt",
        temporal_range=None,
        sequence_range=None,
    )
    d_c = case_c.to_dict()
    assert d_c["temporal_range"] is None
    assert d_c["sequence_range"] is None
    assert EvidenceRef.from_dict(d_c) == case_c

    # Case D: both coordinates simultaneously (e.g. video frame OCR, video VLM sample)
    case_d = EvidenceRef(
        evidence_id="ev_video_frame_01",
        source_excerpt="frame text",
        temporal_range=TemporalRange(start=101.58, end=104.12, duration=2.54),
        sequence_range=SequenceRange(sequence_index=42),
    )
    d_d = case_d.to_dict()
    assert d_d["temporal_range"] == {"start": 101.58, "end": 104.12, "duration": 2.54}
    assert d_d["sequence_range"] == {"sequence_index": 42}
    rt_d = EvidenceRef.from_dict(d_d)
    assert rt_d == case_d
    assert rt_d.temporal_range.start == 101.58
    assert rt_d.sequence_range.sequence_index == 42


# ----------------------------------------------------------------------
# 5. Deterministic KnowledgeUnit ID Invariants
# ----------------------------------------------------------------------

def test_17_evidence_order_preserved():
    # Canonical order vs reordered evidence
    ref1 = EvidenceRef("ev_001", "excerpt 1")
    ref2 = EvidenceRef("ev_002", "excerpt 2")

    ku_id_1 = compute_knowledge_unit_id(
        KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref1, ref2], "statement text"
    )
    ku_id_2 = compute_knowledge_unit_id(
        KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref2, ref1], "statement text"
    )
    assert ku_id_1 != ku_id_2


def test_18_same_inputs_produce_same_ku_id():
    ref = sample_evidence_ref()
    id1 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement text")
    id2 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement text")
    assert id1 == id2


def test_19_reordered_evidence_different_ku_id():
    id1 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, ["e1", "e2"], "statement")
    id2 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, ["e2", "e1"], "statement")
    assert id1 != id2


def test_20_different_statement_different_ku_id():
    ref = sample_evidence_ref()
    id1 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement A")
    id2 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement B")
    assert id1 != id2


def test_21_different_unit_type_different_ku_id():
    ref = sample_evidence_ref()
    id1 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement")
    id2 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.OPINION, [ref], "statement")
    assert id1 != id2


def test_22_different_canonical_id_different_ku_id():
    ref = sample_evidence_ref()
    id1 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_01", UnitType.CLAIM, [ref], "statement")
    id2 = compute_knowledge_unit_id(KNOWLEDGE_SCHEMA_VERSION, "asset_02", UnitType.CLAIM, [ref], "statement")
    assert id1 != id2


def test_23_chunk_lineage_difference_yields_same_ku_id():
    ref = sample_evidence_ref()
    lineage1 = ExtractionLineage("run_01", ["chk_000001"], ["cand_01"], "cand_01")
    lineage2 = ExtractionLineage("run_02", ["chk_000002"], ["cand_02"], "cand_02")

    u1 = create_knowledge_unit("asset_01", UnitType.CLAIM, "same statement", [ref], sample_attribution(), 0.9, lineage1)
    u2 = create_knowledge_unit("asset_01", UnitType.CLAIM, "same statement", [ref], sample_attribution(), 0.9, lineage2)

    assert u1.knowledge_unit_id == u2.knowledge_unit_id


def test_24_candidate_lineage_difference_yields_same_ku_id():
    ref = sample_evidence_ref()
    lineage1 = ExtractionLineage("run_01", ["chk_000001"], ["cand_alpha"])
    lineage2 = ExtractionLineage("run_01", ["chk_000001"], ["cand_beta"])

    u1 = create_knowledge_unit("asset_01", UnitType.CLAIM, "stmt", [ref], sample_attribution(), 0.9, lineage1)
    u2 = create_knowledge_unit("asset_01", UnitType.CLAIM, "stmt", [ref], sample_attribution(), 0.9, lineage2)

    assert u1.knowledge_unit_id == u2.knowledge_unit_id


# ----------------------------------------------------------------------
# 6. Document Container Invariants
# ----------------------------------------------------------------------

def test_25_document_unit_count_invariant():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit("asset_01", UnitType.CLAIM, "stmt", [ref], sample_attribution(), 0.9, sample_lineage())

    # unit_count mismatch
    with pytest.raises(ValueError, match="unit_count"):
        CanonicalKnowledgeUnitsDocument(
            canonical_id="asset_01",
            generated_at="2026-09-09T12:00:00Z",
            unit_count=2,  # mismatch!
            units=[unit],
            extraction_provenance=sample_provenance(),
        )


def test_26_document_canonical_id_consistency():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit("asset_01", UnitType.CLAIM, "stmt", [ref], sample_attribution(), 0.9, sample_lineage())

    with pytest.raises(ValueError, match="mismatching document canonical_id"):
        CanonicalKnowledgeUnitsDocument(
            canonical_id="asset_MISMATCH",
            generated_at="2026-09-09T12:00:00Z",
            unit_count=1,
            units=[unit],
            extraction_provenance=sample_provenance(),
        )


def test_27_schema_version_consistency():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit("asset_01", UnitType.CLAIM, "stmt", [ref], sample_attribution(), 0.9, sample_lineage())

    with pytest.raises(ValueError, match="schema_version"):
        CanonicalKnowledgeUnitsDocument(
            canonical_id="asset_01",
            generated_at="2026-09-09T12:00:00Z",
            unit_count=1,
            units=[unit],
            extraction_provenance=sample_provenance(),
            schema_version="invalid-version",
        )


# ----------------------------------------------------------------------
# 7. Provenance, Lineage, Entity, and Strict Serialization
# ----------------------------------------------------------------------

def test_28_extraction_provenance_serialization():
    prov = sample_provenance()
    d = prov.to_dict()
    assert d["backend"] == "lm-studio"
    assert d["knowledge_schema_version"] == KNOWLEDGE_SCHEMA_VERSION
    prov_rt = ExtractionProvenance.from_dict(d)
    assert prov_rt == prov


def test_29_extraction_lineage_serialization():
    lineage = ExtractionLineage(
        extraction_run_id="run_merge_01",
        input_chunk_ids=["chk_000001", "chk_000002"],
        source_candidate_ids=["cand_1", "cand_2"],
        candidate_id="cand_merged",
        merge_strategy="dedup_exact",
    )
    d = lineage.to_dict()
    assert d["merge_strategy"] == "dedup_exact"
    assert len(d["input_chunk_ids"]) == 2
    rt = ExtractionLineage.from_dict(d)
    assert rt.input_chunk_ids == ["chk_000001", "chk_000002"]
    assert rt.source_candidate_ids == ["cand_1", "cand_2"]


def test_30_entity_and_topic_serialization():
    ent = EntityMention(entity_name="Vulkan", category="inference_framework")
    d = ent.to_dict()
    assert d == {"entity_name": "Vulkan", "category": "inference_framework"}
    assert EntityMention.from_dict(d) == ent


def test_31_relationships_not_accepted_as_canonical_v1_field():
    raw_dict = {
        "knowledge_unit_id": "ku_3e18a992cb412d09",
        "canonical_id": "douyin_123",
        "unit_type": "claim",
        "statement": "test",
        "evidence_refs": [sample_evidence_ref().to_dict()],
        "attribution": sample_attribution().to_dict(),
        "extraction_confidence": 0.9,
        "verification_status": "not_checked",
        "entities": [],
        "topics": [],
        "extraction_lineage": sample_lineage().to_dict(),
        "relationships": [],  # Forbidden placeholder
    }
    with pytest.raises(ValueError, match="relationships field is deferred"):
        CanonicalKnowledgeUnit.from_dict(raw_dict)


def test_32_json_round_trip_stability():
    ref = sample_evidence_ref()
    unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.CLAIM,
        statement="Vulkan后端在StructHalo上量化矩阵走的是通用算子",
        evidence_refs=[ref],
        attribution=sample_attribution(),
        extraction_confidence=0.95,
        extraction_lineage=sample_lineage(),
        entities=[EntityMention("Vulkan", "inference_framework")],
        topics=["端侧大模型"],
    )
    doc = CanonicalKnowledgeUnitsDocument(
        canonical_id="douyin_7681603850364521734",
        generated_at="2026-09-09T12:00:00Z",
        unit_count=1,
        units=[unit],
        extraction_provenance=sample_provenance(),
    )

    doc_dict = doc.to_dict()
    json_str = json.dumps(doc_dict, ensure_ascii=False)
    loaded_dict = json.loads(json_str)
    doc_rt = CanonicalKnowledgeUnitsDocument.from_dict(loaded_dict)

    assert doc_rt.to_dict() == doc_dict
    assert doc_rt.units[0].knowledge_unit_id == unit.knowledge_unit_id


# ----------------------------------------------------------------------
# 8. Real C10 Structural Fixtures & Contextual Observation Validation
# ----------------------------------------------------------------------

def test_33_real_c10_fixtures():
    # Video C10 claim fixture
    video_refs = [
        EvidenceRef("ev_seg_000041", "用主线Vulkan版本的引擎来跑27B", TemporalRange(101.58, 104.12, 2.54)),
        EvidenceRef("ev_seg_000042", "看到的十几Token的速度", TemporalRange(104.12, 105.82, 1.70)),
    ]
    video_unit = create_knowledge_unit(
        canonical_id="douyin_7681603850364521734",
        unit_type=UnitType.CLAIM,
        statement="在Windows系统下使用主线Vulkan后端运行27B模型时速度受限于引擎",
        evidence_refs=video_refs,
        attribution=AttributionInfo(source_actor_name="老林说", attribution_status=AttributionStatus.UNVERIFIED_SPEAKER),
        extraction_confidence=0.95,
        extraction_lineage=sample_lineage(),
    )
    assert video_unit.knowledge_unit_id.startswith("ku_")
    assert len(video_unit.evidence_refs) == 2

    # Album C10 observation fixture
    album_ref = EvidenceRef("ve_img_001", "logitech\nINAMAX", sequence_range=SequenceRange(1))
    album_unit = create_knowledge_unit(
        canonical_id="douyin_7682038498466993905",
        unit_type=UnitType.OBSERVATION,
        statement="图集第1张图经OCR检测到文本内容为 logitech 与 INAMAX",
        evidence_refs=[album_ref],
        attribution=AttributionInfo(source_actor_name="姑妈有神王", attribution_status=AttributionStatus.VISUAL_MEDIA),
        extraction_confidence=0.98,
        extraction_lineage=sample_lineage(),
    )
    assert album_unit.unit_type == UnitType.OBSERVATION
    assert album_unit.evidence_refs[0].sequence_range.sequence_index == 1


def test_34_validate_observation_grounding_helper():
    manifest_modalities = {
        "ev_seg_000041": "speech",
        "ve_img_001": "visual_text",
    }

    # Speech-only observation fails contextual validation
    speech_obs = create_knowledge_unit(
        "douyin_7681603850364521734",
        UnitType.OBSERVATION,
        "口播声称画面里有终端",
        [EvidenceRef("ev_seg_000041", "用主线Vulkan版本的引擎来跑27B")],
        sample_attribution(),
        0.9,
        sample_lineage(),
    )
    with pytest.raises(ValueError, match="machine-observed evidence item"):
        validate_observation_grounding(speech_obs, manifest_modalities)

    # Visual observation passes contextual validation
    visual_obs = create_knowledge_unit(
        "douyin_7682038498466993905",
        UnitType.OBSERVATION,
        "OCR检测到文本",
        [EvidenceRef("ve_img_001", "logitech")],
        AttributionInfo(attribution_status=AttributionStatus.VISUAL_MEDIA),
        0.98,
        sample_lineage(),
    )
    assert validate_observation_grounding(visual_obs, manifest_modalities) is True


def test_35_legacy_point_adaptation_helper():
    legacy_point = {
        "type": "author_claim",
        "title": "旧版标题",
        "content": "旧版事实主张内容",
        "confidence": 0.88,
        "id": "k_001",
    }
    unit = adapt_legacy_point(
        point=legacy_point,
        canonical_id="douyin_7681603850364521734",
        evidence_refs=[sample_evidence_ref()],
        source_actor_name="老林说",
    )
    assert unit.unit_type == UnitType.CLAIM
    assert unit.attribution.source_actor_name == "老林说"
    assert unit.attribution.speaker_name is None
    assert unit.attribution.attribution_status == AttributionStatus.UNVERIFIED_SPEAKER
    assert unit.extraction_confidence == 0.88
    assert unit.statement == "旧版事实主张内容"
