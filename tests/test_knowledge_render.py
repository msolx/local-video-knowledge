from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.knowledge.render import (
    FINAL_DOCUMENT_FILENAME,
    FINAL_RENDER_FILENAME,
    FINALIZATION_FILENAME,
    FINALIZATION_SCHEMA_VERSION,
    RENDER_POLICY_VERSION,
    RenderConfig,
    audit_finalization_identity,
    build_final_document,
    compute_enriched_artifact_fingerprint,
    compute_finalization_fingerprint,
    escape_source_excerpt,
    finalize_knowledge_document,
    render_audit_markdown,
    validate_verification_status,
)
from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    AttributionInfo,
    AttributionStatus,
    EntityMention,
    EvidenceRef,
    ExtractionLineage,
    ExtractionProvenance,
    SequenceRange,
    TemporalRange,
    UnitType,
    VerificationStatus,
    create_knowledge_unit,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def _unit(
    *,
    statement: str = "The device has a USB-C port.",
    unit_type: UnitType = UnitType.CLAIM,
    evidence_id: str = "ev_001",
    excerpt: str = "The device has a USB-C port.",
    confidence: float = 0.4,
    verification_status: VerificationStatus | str = VerificationStatus.NOT_CHECKED,
    source_actor_name: str | None = "Author",
    attribution_status: AttributionStatus = AttributionStatus.UNVERIFIED_SPEAKER,
    speaker_name: str | None = None,
    entities: list[dict] | None = None,
    topics: list[str] | None = None,
    temporal: bool = True,
    sequence: bool = True,
) -> dict:
    unit = create_knowledge_unit(
        canonical_id="douyin_test",
        unit_type=unit_type,
        statement=statement,
        evidence_refs=[
            EvidenceRef(
                evidence_id=evidence_id,
                source_excerpt=excerpt,
                temporal_range=TemporalRange(start=1.0, end=2.0, duration=9.0) if temporal else None,
                sequence_range=SequenceRange(sequence_index=4) if sequence else None,
            )
        ],
        attribution=AttributionInfo(
            source_actor_name=source_actor_name,
            attribution_status=attribution_status,
            speaker_name=speaker_name,
        ),
        extraction_confidence=confidence,
        extraction_lineage=ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=["chk_000001"],
            candidate_id="cand_001",
            source_candidate_ids=["cand_001"],
            merge_strategy="dedup_exact",
        ),
        verification_status=verification_status,
        entities=[EntityMention(entity_name=e["entity_name"], category=e["category"]) for e in (entities or [])],
        topics=topics or [],
    )
    return unit.to_dict()


def _enriched_artifact(units: list[dict]) -> dict:
    return {
        "schema_version": "m4-enriched-candidates-v1",
        "canonical_id": "douyin_test",
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source_merged_artifact_fingerprint": "srcfp",
        "enrichment_policy_version": "m4-surface-grounded-enrichment-v1",
        "enrichment_fingerprint": "efp",
        "enrichment_provenance": {
            "backend": "mock",
            "model": "qwen3-8b",
            "prompt_version": "m4-enrichment-v1.0",
            "policy_version": "m4-surface-grounded-enrichment-v1",
            "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "generation_config": {"temperature": 0.1, "max_tokens": 4096, "batch_size": 10},
        },
        "input_unit_count": len(units),
        "output_unit_count": len(units),
        "enriched_unit_count": len(units),
        "failed_unit_count": 0,
        "units": units,
        "audit": {"llm_call_count": 1, "failures": [], "rejections": [], "batch_summaries": []},
        "fingerprint": "efp",
    }


def _provenance() -> dict:
    return {
        "backend": "openai_compatible",
        "model": "qwen3-8b",
        "prompt_version": "m4-extraction-v1.0",
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "temperature": 0.1,
        "generated_at": "2026-09-09T05:04:28+00:00",
        "evidence_manifest_fingerprint": "a" * 64,
        "evidence_chunks_fingerprint": "b" * 64,
    }


def _make_final(enriched: dict, provenance: dict | None = None) -> dict:
    return build_final_document(
        enriched,
        provenance or _provenance(),
        generated_at="2026-09-09T06:00:00+00:00",
    )


# ----------------------------------------------------------------------
# 1. final document valid
# 2. input/output unit count identical
# ----------------------------------------------------------------------

def test_final_document_is_valid_knowledge_units_document():
    enriched = _enriched_artifact([_unit(), _unit(statement="Second.")])
    final = _make_final(enriched)
    assert final["schema_version"] == KNOWLEDGE_SCHEMA_VERSION
    assert final["canonical_id"] == "douyin_test"
    assert final["unit_count"] == 2
    assert final["extraction_provenance"]["model"] == "qwen3-8b"
    assert final["extraction_provenance"]["generated_at"] == "2026-09-09T05:04:28+00:00"


def test_input_output_unit_count_identical():
    enriched = _enriched_artifact([_unit(), _unit(statement="Two."), _unit(statement="Three.")])
    final = _make_final(enriched)
    assert len(final["units"]) == len(enriched["units"]) == 3
    assert final["unit_count"] == 3


# ----------------------------------------------------------------------
# 3-11. canonical fields unchanged
# ----------------------------------------------------------------------

def test_ku_id_unchanged():
    enriched = _enriched_artifact([_unit()])
    final = _make_final(enriched)
    assert final["units"][0]["knowledge_unit_id"] == enriched["units"][0]["knowledge_unit_id"]


def test_statement_unchanged():
    enriched = _enriched_artifact([_unit(statement="Exact wording retained.")])
    final = _make_final(enriched)
    assert final["units"][0]["statement"] == "Exact wording retained."


def test_evidence_refs_unchanged():
    enriched = _enriched_artifact([_unit()])
    final = _make_final(enriched)
    assert final["units"][0]["evidence_refs"] == enriched["units"][0]["evidence_refs"]


def test_attribution_unchanged():
    enriched = _enriched_artifact([_unit(source_actor_name="老林说")])
    final = _make_final(enriched)
    assert final["units"][0]["attribution"] == enriched["units"][0]["attribution"]


def test_extraction_confidence_unchanged():
    enriched = _enriched_artifact([_unit(confidence=0.93)])
    final = _make_final(enriched)
    assert final["units"][0]["extraction_confidence"] == 0.93


def test_verification_unchanged():
    enriched = _enriched_artifact([_unit()])
    final = _make_final(enriched)
    assert final["units"][0]["verification_status"] == "not_checked"


def test_entities_unchanged():
    enriched = _enriched_artifact([
        _unit(entities=[{"entity_name": "Vulkan", "category": "inference_framework"}])
    ])
    final = _make_final(enriched)
    assert final["units"][0]["entities"] == [{"entity_name": "Vulkan", "category": "inference_framework"}]


def test_topics_unchanged():
    enriched = _enriched_artifact([_unit(topics=["本地大模型推理"])])
    final = _make_final(enriched)
    assert final["units"][0]["topics"] == ["本地大模型推理"]


def test_lineage_unchanged():
    enriched = _enriched_artifact([_unit()])
    final = _make_final(enriched)
    assert final["units"][0]["extraction_lineage"] == enriched["units"][0]["extraction_lineage"]


# ----------------------------------------------------------------------
# 12-15. verification rendering without generating verification
# ----------------------------------------------------------------------

def test_not_checked_rendered_correctly():
    final = _make_final(_enriched_artifact([_unit()]))
    md = render_audit_markdown(final)
    assert "Verification: Not checked" in md


def test_verified_rendered_without_generating_verification():
    unit = _unit(verification_status=VerificationStatus.VERIFIED)
    enriched = _enriched_artifact([unit])
    final = _make_final(enriched)
    # status field preserves the input value
    assert final["units"][0]["verification_status"] == "verified"
    md = render_audit_markdown(final)
    assert "Verification: Verified" in md


def test_contested_render():
    unit = _unit(verification_status=VerificationStatus.CONTESTED)
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Verification: Contested" in md


def test_unsupported_render():
    unit = _unit(verification_status=VerificationStatus.UNSUPPORTED)
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Verification: Unsupported" in md


# ----------------------------------------------------------------------
# 16-19. attribution rendering
# ----------------------------------------------------------------------

def test_source_actor_vs_speaker_distinction():
    unit = _unit(source_actor_name="老林说", speaker_name="DiarizedUser")
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Source actor: 老林说" in md
    assert "Speaker: DiarizedUser" in md


def test_unknown_speaker_render():
    unit = _unit(source_actor_name="老林说", speaker_name=None)
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Source actor: 老林说" in md
    assert "Speaker: Unknown" in md
    assert "Attribution status: Unverified speaker" in md


def test_visual_media_render():
    unit = _unit(attribution_status=AttributionStatus.VISUAL_MEDIA, source_actor_name=None)
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Attribution status: Visual media" in md


def test_system_derived_render():
    unit = _unit(
        unit_type=UnitType.VERIFICATION_QUESTION,
        attribution_status=AttributionStatus.SYSTEM_DERIVED,
        source_actor_name=None,
    )
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Attribution status: System derived" in md


# ----------------------------------------------------------------------
# 20-23. coordinate rendering
# ----------------------------------------------------------------------

def test_temporal_render():
    final = _make_final(_enriched_artifact([_unit(temporal=True, sequence=False)]))
    md = render_audit_markdown(final)
    assert "temporal range: start=1.0s, end=2.0s, duration=9.0s" in md


def test_sequence_render():
    final = _make_final(_enriched_artifact([_unit(temporal=False, sequence=True)]))
    md = render_audit_markdown(final)
    assert "sequence range: sequence=4" in md


def test_both_coordinate_render():
    final = _make_final(_enriched_artifact([_unit(temporal=True, sequence=True)]))
    md = render_audit_markdown(final)
    assert "temporal range:" in md
    assert "sequence range:" in md


def test_neither_coordinate_render():
    final = _make_final(_enriched_artifact([_unit(temporal=False, sequence=False)]))
    md = render_audit_markdown(final)
    assert "temporal range:" not in md
    assert "sequence range:" not in md


# ----------------------------------------------------------------------
# 24. exact excerpt retained
# ----------------------------------------------------------------------

def test_exact_excerpt_retained():
    excerpt = "The 4090 card runs Vulkan inference at 60 FPS."
    final = _make_final(_enriched_artifact([_unit(excerpt=excerpt)]))
    md = render_audit_markdown(final)
    assert "The 4090 card runs Vulkan inference at 60 FPS." in md


# ----------------------------------------------------------------------
# 25. Markdown injection safely rendered
# ----------------------------------------------------------------------

def test_markdown_injection_safely_rendered():
    excerpt = "# Fake Heading\n```\nrm -rf /\n```\n<script>alert(1)</script>\n> nested quote"
    final = _make_final(_enriched_artifact([_unit(excerpt=excerpt)]))
    md = render_audit_markdown(final)
    # Original semantic text still present verbatim.
    assert "Fake Heading" in md
    assert "rm -rf /" in md
    # No top-level heading/fence produced from untrusted content.
    assert "\n# Fake Heading" not in md
    assert "<script>" not in md
    assert "&lt;script&gt;" in md


def test_escape_source_excerpt_blockquote_and_escaping():
    escaped = escape_source_excerpt("line1\nline2")
    assert escaped == "> line1\n> line2"
    assert escape_source_excerpt("<b>") == "> &lt;b&gt;"


# ----------------------------------------------------------------------
# 26. entity/topic render
# ----------------------------------------------------------------------

def test_entity_topic_render():
    unit = _unit(
        entities=[{"entity_name": "Vulkan", "category": "inference_framework"}],
        topics=["本地大模型推理"],
    )
    md = render_audit_markdown(_make_final(_enriched_artifact([unit])))
    assert "Entities:" in md
    assert "- Vulkan (inference_framework)" in md
    assert "Topics:" in md
    assert "- 本地大模型推理" in md


# ----------------------------------------------------------------------
# 27-28. stable ordering
# ----------------------------------------------------------------------

def test_stable_unit_ordering():
    units = [_unit(statement="First."), _unit(statement="Second."), _unit(statement="Third.")]
    enriched = _enriched_artifact(units)
    final = _make_final(enriched)
    assert [u["knowledge_unit_id"] for u in final["units"]] == [
        u["knowledge_unit_id"] for u in enriched["units"]
    ]


def test_stable_evidence_ordering():
    first = _unit(evidence_id="ev_002")
    second = _unit(statement="Second.", evidence_id="ev_001")
    enriched = _enriched_artifact([first, second])
    final = _make_final(enriched)
    assert [r["evidence_id"] for r in final["units"][0]["evidence_refs"]] == ["ev_002"]


# ----------------------------------------------------------------------
# 29. zero-unit document
# ----------------------------------------------------------------------

def test_zero_unit_document():
    final = _make_final(_enriched_artifact([]))
    assert final["unit_count"] == 0
    assert final["units"] == []
    md = render_audit_markdown(final)
    assert "unit count: 0" in md


# ----------------------------------------------------------------------
# 30-31. deterministic output
# ----------------------------------------------------------------------

def test_deterministic_json_output():
    enriched = _enriched_artifact([_unit()])
    a = _make_final(enriched)
    b = _make_final(enriched)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b, ensure_ascii=False, sort_keys=True
    )


def test_deterministic_markdown_output():
    enriched = _enriched_artifact([_unit(), _unit(statement="Second.")])
    final = _make_final(enriched)
    md1 = render_audit_markdown(final)
    md2 = render_audit_markdown(final)
    assert md1 == md2


# ----------------------------------------------------------------------
# 32-34. cache / idempotency
# ----------------------------------------------------------------------

def test_cache_hit(tmp_path):
    processed_dir = tmp_path / "douyin_test"
    knowledge_dir = processed_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "enriched_knowledge_candidates.json").write_text(
        json.dumps(_enriched_artifact([_unit()]), ensure_ascii=False), encoding="utf-8"
    )
    (knowledge_dir / "knowledge_candidates.json").write_text(
        json.dumps({"canonical_id": "douyin_test", "provenance": _provenance()}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = finalize_knowledge_document(processed_dir)
    assert first["cache_hit"] is False
    units_path = knowledge_dir / FINAL_DOCUMENT_FILENAME
    render_path = knowledge_dir / FINAL_RENDER_FILENAME
    units_before = units_path.read_bytes()
    render_before = render_path.read_bytes()

    second = finalize_knowledge_document(processed_dir)
    assert second["cache_hit"] is True
    assert units_path.read_bytes() == units_before
    assert render_path.read_bytes() == render_before
    assert second["generated_at"] == first["generated_at"]


def test_enriched_fingerprint_invalidates_cache(tmp_path):
    processed_dir = tmp_path / "douyin_test"
    knowledge_dir = processed_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "knowledge_candidates.json").write_text(
        json.dumps({"canonical_id": "douyin_test", "provenance": _provenance()}, ensure_ascii=False),
        encoding="utf-8",
    )
    enriched = _enriched_artifact([_unit()])
    (knowledge_dir / "enriched_knowledge_candidates.json").write_text(
        json.dumps(enriched, ensure_ascii=False), encoding="utf-8"
    )

    first = finalize_knowledge_document(processed_dir)
    assert first["cache_hit"] is False

    changed = _enriched_artifact([_unit(statement="Changed statement.")])
    (knowledge_dir / "enriched_knowledge_candidates.json").write_text(
        json.dumps(changed, ensure_ascii=False), encoding="utf-8"
    )
    third = finalize_knowledge_document(processed_dir)
    assert third["cache_hit"] is False
    assert third["source_enriched_artifact_fingerprint"] != first["source_enriched_artifact_fingerprint"]


def test_render_policy_invalidates_cache(tmp_path):
    processed_dir = tmp_path / "douyin_test"
    knowledge_dir = processed_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "enriched_knowledge_candidates.json").write_text(
        json.dumps(_enriched_artifact([_unit()]), ensure_ascii=False), encoding="utf-8"
    )
    (knowledge_dir / "knowledge_candidates.json").write_text(
        json.dumps({"canonical_id": "douyin_test", "provenance": _provenance()}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = finalize_knowledge_document(processed_dir)
    assert first["cache_hit"] is False

    new_policy = RenderConfig(policy_version="m4-audit-render-v2")
    second = finalize_knowledge_document(processed_dir, new_policy)
    assert second["cache_hit"] is False
    assert second["finalization_policy_version"] == "m4-audit-render-v2"


# ----------------------------------------------------------------------
# 35. invalid verification status rejected
# ----------------------------------------------------------------------

def test_invalid_verification_status_rejected():
    validate_verification_status("not_checked")
    validate_verification_status("verified")
    with pytest.raises(ValueError):
        validate_verification_status("definitely_true")
    with pytest.raises(ValueError):
        validate_verification_status(123)


# ----------------------------------------------------------------------
# 36. canonical identity mismatch rejected
# ----------------------------------------------------------------------

def test_canonical_identity_mismatch_rejected():
    enriched = _enriched_artifact([_unit()])
    final = _make_final(enriched)
    tampered = json.loads(json.dumps(final))
    tampered["units"][0]["statement"] = "Mutated."
    audit = audit_finalization_identity(enriched, tampered)
    assert audit["valid"] is False
    assert audit["identity_violation_count"] == 1


# ----------------------------------------------------------------------
# misc: fingerprint determinism
# ----------------------------------------------------------------------

def test_finalization_fingerprint_deterministic():
    enriched = _enriched_artifact([_unit()])
    fp = compute_enriched_artifact_fingerprint(enriched)
    a = compute_finalization_fingerprint(fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig())
    b = compute_finalization_fingerprint(fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig())
    assert a == b
    c = compute_finalization_fingerprint(fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig(policy_version="v2"))
    assert a != c


def test_enriched_fingerprint_content_addressed():
    a = _enriched_artifact([_unit()])
    b = json.loads(json.dumps(a))
    assert compute_enriched_artifact_fingerprint(a) == compute_enriched_artifact_fingerprint(b)
    b["units"][0]["statement"] = "Different."
    assert compute_enriched_artifact_fingerprint(a) != compute_enriched_artifact_fingerprint(b)


def test_no_secret_persisted(tmp_path):
    processed_dir = tmp_path / "douyin_test"
    knowledge_dir = processed_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    prov = _provenance()
    prov["api_key"] = "sk-SUPERSECRET"
    (knowledge_dir / "enriched_knowledge_candidates.json").write_text(
        json.dumps(_enriched_artifact([_unit()]), ensure_ascii=False), encoding="utf-8"
    )
    (knowledge_dir / "knowledge_candidates.json").write_text(
        json.dumps({"canonical_id": "douyin_test", "provenance": prov}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = finalize_knowledge_document(processed_dir)
    persisted = json.dumps(
        json.loads((knowledge_dir / FINAL_DOCUMENT_FILENAME).read_text(encoding="utf-8")),
        ensure_ascii=False,
    )
    wrapper = (knowledge_dir / FINALIZATION_FILENAME).read_text(encoding="utf-8")
    md = (knowledge_dir / FINAL_RENDER_FILENAME).read_text(encoding="utf-8")
    assert "SUPERSECRET" not in persisted
    assert "SUPERSECRET" not in wrapper
    assert "SUPERSECRET" not in md
    assert "api_key" not in json.loads(persisted)["extraction_provenance"]


# ----------------------------------------------------------------------
# 37-38. real C10 fixtures
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "asset_id",
    ["douyin_7681603850364521734", "douyin_7682038498466993905"],
)
def test_real_c10_finalize_offline(asset_id: str):
    root = Path(__file__).resolve().parents[1]
    enriched_path = root / "data" / "processed" / asset_id / "knowledge" / "enriched_knowledge_candidates.json"
    assert enriched_path.is_file(), f"missing {enriched_path}"
    enriched = json.loads(enriched_path.read_text(encoding="utf-8"))
    provenance_path = root / "data" / "processed" / asset_id / "knowledge" / "knowledge_candidates.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))["provenance"]
    final = build_final_document(enriched, provenance, generated_at="2026-09-09T06:00:00+00:00")
    audit = audit_finalization_identity(enriched, final)
    assert audit["valid"] is True
    assert audit["identity_violation_count"] == 0
    assert final["unit_count"] == enriched["output_unit_count"]
    assert final["schema_version"] == KNOWLEDGE_SCHEMA_VERSION
    md = render_audit_markdown(final)
    assert "# Knowledge Audit" in md
    assert len(md) > 100


def test_real_c10_video_final_document_counts():
    root = Path(__file__).resolve().parents[1]
    enriched_path = root / "data" / "processed" / "douyin_7681603850364521734" / "knowledge" / "enriched_knowledge_candidates.json"
    enriched = json.loads(enriched_path.read_text(encoding="utf-8"))
    assert enriched["output_unit_count"] == 62
    final = _make_final(enriched)
    assert final["unit_count"] == 62
    assert len(final["units"]) == 62
    types = {}
    statuses = {}
    entity_count = 0
    topic_count = 0
    for unit in final["units"]:
        types[unit["unit_type"]] = types.get(unit["unit_type"], 0) + 1
        statuses[unit["verification_status"]] = statuses.get(unit["verification_status"], 0) + 1
        entity_count += len(unit["entities"])
        topic_count += len(unit["topics"])
    assert statuses.get("not_checked", 0) == 62
    assert entity_count > 0
    assert topic_count > 0


def test_real_c10_album_final_document_counts_and_ocr_excerpt():
    root = Path(__file__).resolve().parents[1]
    enriched_path = root / "data" / "processed" / "douyin_7682038498466993905" / "knowledge" / "enriched_knowledge_candidates.json"
    enriched = json.loads(enriched_path.read_text(encoding="utf-8"))
    assert enriched["output_unit_count"] == 6
    final = _make_final(enriched)
    assert final["unit_count"] == 6
    md = render_audit_markdown(final)
    # OCR excerpt must appear verbatim (line-wise) in the markdown.
    excerpt0 = final["units"][0]["evidence_refs"][0]["source_excerpt"]
    assert excerpt0
    for line in excerpt0.split("\n"):
        if line.strip():
            assert line in md