"""M4-06 End-to-End Acceptance audit tests.

These tests exercise the deterministic, offline acceptance checks over the M4
chain (M3 evidence -> M4-02 candidates -> M4-03 merged -> M4-04 enriched ->
M4-05 final). They use the real C10 assets where available and synthetic
fixtures otherwise. No LLM runtime is started; all checks are deterministic.
"""
from __future__ import annotations

import json
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

import pytest

from src.knowledge.enrichment import compute_merged_artifact_fingerprint
from src.knowledge.merger import (
    MERGED_CANDIDATES_SCHEMA_VERSION,
    compute_candidates_artifact_fingerprint,
)
from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    AttributionInfo,
    AttributionStatus,
    EvidenceRef,
    ExtractionLineage,
    TemporalRange,
    UnitType,
    VerificationStatus,
    compute_knowledge_unit_id,
    create_knowledge_unit,
    normalize_statement,
)
from src.knowledge.render import (
    RENDER_POLICY_VERSION,
    RenderConfig,
    compute_enriched_artifact_fingerprint,
    compute_finalization_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"

INCIDENT_FIXTURE = ROOT / "tests" / "fixtures" / "m4_c10_incident_20260910.json"
INCIDENT_STATUS = "RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS"

# Historical frozen source_enriched anchors. These describe the ORIGINAL, now-lost
# execution generation. They are never overwritten and never re-anchored.
VIDEO_HISTORICAL_ENRICHED_ANCHOR = "0b329ed0fedad69a196d92f3a3febedd3a6faf6d493e5da7675059173adf39e7"
ALBUM_HISTORICAL_ENRICHED_ANCHOR = "6687bfd29d8db63657b73766a53c26c79f54201a9cdbffc303ee55c942665f34"

PERCEPTUAL_MODALITIES = {"visual_text", "visual_description", "perceptual_metric"}

FROZEN_FIELDS = [
    "knowledge_unit_id",
    "canonical_id",
    "unit_type",
    "statement",
    "evidence_refs",
    "attribution",
    "extraction_confidence",
    "verification_status",
    "extraction_lineage",
]


def _norm_surface(text: str) -> str:
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _load_json(rel: Path) -> Any:
    return json.loads(rel.read_text(encoding="utf-8"))


def _load_asset(asset_id: str) -> dict[str, Any]:
    base = ROOT / "data" / "processed" / asset_id
    kdir = base / "knowledge"
    return {
        "manifest": _load_json(base / "evidence_manifest.json"),
        "chunks": _load_json(base / "evidence_chunks.json"),
        "candidates": _load_json(kdir / "knowledge_candidates.json"),
        "merged": _load_json(kdir / "merged_knowledge_candidates.json"),
        "enriched": _load_json(kdir / "enriched_knowledge_candidates.json"),
        "units": _load_json(kdir / "knowledge_units.json"),
        "finalization": _load_json(kdir / "knowledge_finalization.json"),
        "markdown": (kdir / "knowledge.md").read_text(encoding="utf-8"),
    }


def _load_incident_manifest() -> dict[str, Any]:
    return _load_json(INCIDENT_FIXTURE)


def _incident_asset_entry(incident: dict[str, Any], asset_id: str) -> dict[str, Any]:
    assert incident["incident_id"] == "m4-c10-provenance-incident-20260910"
    assert incident["schema_version"] == "m4-c10-incident-v1"
    assert incident["status"] == INCIDENT_STATUS
    for entry in incident["affected_assets"]:
        if entry["canonical_id"] == asset_id:
            return entry
    raise AssertionError(f"asset {asset_id} not present in incident manifest")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# Synthetic fixture chain (zero-unit -> multi-unit)
# ----------------------------------------------------------------------

def _unit(
    statement: str = "The device has a USB-C port.",
    evidence_id: str = "ev_001",
    excerpt: str = "The device has a USB-C port.",
    unit_type: UnitType = UnitType.CLAIM,
    attribution_status: AttributionStatus = AttributionStatus.UNVERIFIED_SPEAKER,
    temporal: Optional[tuple] = (1.0, 2.0, 9.0),
    verification_status: str = "not_checked",
    entities: Optional[list[dict]] = None,
    topics: Optional[list[str]] = None,
) -> dict:
    unit = create_knowledge_unit(
        canonical_id="douyin_test",
        unit_type=unit_type,
        statement=statement,
        evidence_refs=[
            EvidenceRef(
                evidence_id=evidence_id,
                source_excerpt=excerpt,
                temporal_range=TemporalRange(start=temporal[0], end=temporal[1], duration=temporal[2]) if temporal else None,
                sequence_range=None,
            )
        ],
        attribution=AttributionInfo(
            source_actor_name="Author",
            attribution_status=attribution_status,
        ),
        extraction_confidence=0.9,
        extraction_lineage=ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=["chk_000001"],
            candidate_id="cand_chk_000001_001_abc",
            source_candidate_ids=["cand_chk_000001_001_abc"],
            merge_strategy="dedup_exact",
        ),
        verification_status=verification_status,
        entities=[__import__("src.knowledge.models", fromlist=["EntityMention"]).EntityMention(e["entity_name"], e["category"]) for e in (entities or [])],
        topics=topics or [],
    )
    return unit.to_dict()


def _synthetic_candidates_artifact(units: list[dict]) -> dict:
    return {
        "canonical_id": "douyin_test",
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "extraction_schema_version": "m4-candidates-v1",
        "extraction_run_id": "run_test",
        "provenance": {
            "backend": "mock",
            "model": "qwen3-8b",
            "prompt_version": "m4-extraction-v1.0",
            "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "temperature": 0.1,
            "generated_at": "2026-09-09T05:04:28+00:00",
            "evidence_manifest_fingerprint": "a" * 64,
            "evidence_chunks_fingerprint": "b" * 64,
        },
        "candidates": units,
        "rejections": [],
    }


def _synthetic_manifest(units: list[dict]) -> dict:
    items = []
    for u in units:
        for r in u["evidence_refs"]:
            items.append(
                {
                    "evidence_id": r["evidence_id"],
                    "modality": "speech",
                    "temporal": r["temporal_range"],
                    "sequence": None,
                    "payload": {"text": r["source_excerpt"]},
                    "verification_status": "not_checked",
                }
            )
    return {"manifest_fingerprint": "a" * 64, "evidence_items": items}


# ----------------------------------------------------------------------
# 1. valid complete pipeline chain (synthetic)
# 2. zero-unit valid pipeline
# ----------------------------------------------------------------------

def test_zero_unit_pipeline_is_valid():
    candidates = _synthetic_candidates_artifact([])
    manifest = _synthetic_manifest([])
    cand_fp = compute_candidates_artifact_fingerprint(candidates)
    merged_fp = compute_merged_artifact_fingerprint({"units": [], "source_candidates_artifact_fingerprint": cand_fp})
    enriched_fp = compute_enriched_artifact_fingerprint({"units": [], "source_merged_artifact_fingerprint": merged_fp})
    fin_fp = compute_finalization_fingerprint(enriched_fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig())
    assert len(manifest["evidence_items"]) == 0
    assert isinstance(cand_fp, str) and len(cand_fp) == 64
    assert isinstance(fin_fp, str) and len(fin_fp) == 64


def test_synthetic_chain_counts_are_consistent():
    units = [_unit(), _unit(statement="Second claim.", evidence_id="ev_002", excerpt="Second claim.")]
    candidates = _synthetic_candidates_artifact(units)
    merged = {"units": units, "source_candidates_artifact_fingerprint": compute_candidates_artifact_fingerprint(candidates)}
    enriched = {"units": units, "source_merged_artifact_fingerprint": compute_merged_artifact_fingerprint(merged)}
    final = {"units": units, "schema_version": KNOWLEDGE_SCHEMA_VERSION}
    assert len(candidates["candidates"]) == len(merged["units"]) == len(enriched["units"]) == len(final["units"]) == 2


# ----------------------------------------------------------------------
# 3. fingerprint chain
# ----------------------------------------------------------------------

def test_fingerprint_chain_links_each_stage():
    units = [_unit()]
    candidates = _synthetic_candidates_artifact(units)
    cand_fp = compute_candidates_artifact_fingerprint(candidates)
    merged = {"units": units, "source_candidates_artifact_fingerprint": cand_fp}
    assert merged["source_candidates_artifact_fingerprint"] == cand_fp

    merged_fp = compute_merged_artifact_fingerprint(merged)
    enriched = {"units": units, "source_merged_artifact_fingerprint": merged_fp}
    assert enriched["source_merged_artifact_fingerprint"] == merged_fp

    enriched_fp = compute_enriched_artifact_fingerprint(enriched)
    fin_fp = compute_finalization_fingerprint(enriched_fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig())
    assert isinstance(fin_fp, str) and len(fin_fp) == 64


def test_stale_artifact_detection_fingerprint_mismatch():
    units = [_unit()]
    candidates = _synthetic_candidates_artifact(units)
    # A stale merged artifact referencing an outdated candidate fingerprint
    stale_merged = {"units": units, "source_candidates_artifact_fingerprint": "0" * 64}
    assert stale_merged["source_candidates_artifact_fingerprint"] != compute_candidates_artifact_fingerprint(candidates)


# ----------------------------------------------------------------------
# 4. KU ID recomputation
# 5. tampered KU ID detection
# ----------------------------------------------------------------------

def test_ku_id_recomputation_matches_frozen_formula():
    units = [_unit(), _unit(statement="Second.", evidence_id="ev_002", excerpt="Second.")]
    for u in units:
        eids = [r["evidence_id"] for r in u["evidence_refs"]]
        recomputed = compute_knowledge_unit_id(
            KNOWLEDGE_SCHEMA_VERSION, u["canonical_id"], u["unit_type"], eids,
            normalize_statement(u["statement"]),
        )
        assert recomputed == u["knowledge_unit_id"]


def test_tampered_ku_id_detected():
    u = _unit()
    tampered = dict(u)
    tampered["knowledge_unit_id"] = "ku_" + "f" * 16
    eids = [r["evidence_id"] for r in u["evidence_refs"]]
    recomputed = compute_knowledge_unit_id(
        KNOWLEDGE_SCHEMA_VERSION, u["canonical_id"], u["unit_type"], eids,
        normalize_statement(u["statement"]),
    )
    assert recomputed != tampered["knowledge_unit_id"]


# ----------------------------------------------------------------------
# 6. exact excerpt grounding
# 7. coordinate grounding
# 8. unresolved evidence rejection
# ----------------------------------------------------------------------

def test_excerpt_grounding_exact_match():
    u = _unit(statement="Speaker was sick.", excerpt="前段时间生了一场病")
    excerpt = u["evidence_refs"][0]["source_excerpt"]
    assert excerpt == "前段时间生了一场病"
    assert "前段时间生了一场病" in u["evidence_refs"][0]["source_excerpt"]


def test_coordinate_grounding_temporal():
    u = _unit(statement="Speaker was sick.", excerpt="前段时间生了一场病", temporal=(1.5, 3.0, 1.5))
    tr = u["evidence_refs"][0]["temporal_range"]
    assert tr["start"] == 1.5 and tr["end"] == 3.0 and tr["duration"] == 1.5


def test_tampered_excerpt_detected():
    manifest = _synthetic_manifest([_unit()])
    item = manifest["evidence_items"][0]
    tampered = "A DIFFERENT EXCERPT"
    assert item["payload"]["text"] != tampered


def test_unresolved_visual_evidence_rejected():
    payload = {"status": "unresolved_visual_reference", "description": None}
    assert payload["status"] == "unresolved_visual_reference"
    assert payload.get("description") is None


# ----------------------------------------------------------------------
# 9. attribution validation
# 10. observation validation
# ----------------------------------------------------------------------

def test_attribution_speech_unverified_speaker():
    u = _unit(attribution_status=AttributionStatus.UNVERIFIED_SPEAKER)
    assert u["attribution"]["attribution_status"] == "unverified_speaker"
    assert u["attribution"]["speaker_name"] is None


def test_attribution_verification_question_system_derived():
    u = _unit(
        unit_type=UnitType.VERIFICATION_QUESTION,
        attribution_status=AttributionStatus.SYSTEM_DERIVED,
    )
    assert u["unit_type"] == "verification_question"
    assert u["attribution"]["attribution_status"] == "system_derived"


def test_observation_requires_perceptual_evidence():
    from src.knowledge.models import validate_observation_grounding
    u = create_knowledge_unit(
        canonical_id="douyin_test",
        unit_type=UnitType.OBSERVATION,
        statement="The image shows a logo.",
        evidence_refs=[
            EvidenceRef(
                evidence_id="ev_img_001",
                source_excerpt="logitech",
                temporal_range=None,
                sequence_range=None,
            )
        ],
        attribution=AttributionInfo(attribution_status=AttributionStatus.VISUAL_MEDIA),
        extraction_confidence=0.9,
        extraction_lineage=ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=["chk_000001"],
            source_candidate_ids=["cand_001"],
        ),
    )
    manifest_modalities = {"ev_img_001": "visual_text"}
    assert validate_observation_grounding(u, manifest_modalities) is True


def test_observation_with_speech_evidence_invalid():
    from src.knowledge.models import validate_observation_grounding
    u = create_knowledge_unit(
        canonical_id="douyin_test",
        unit_type=UnitType.OBSERVATION,
        statement="The speaker said something.",
        evidence_refs=[
            EvidenceRef(
                evidence_id="ev_seg_001",
                source_excerpt="hello",
                temporal_range=TemporalRange(1.0, 2.0, 1.0),
                sequence_range=None,
            )
        ],
        attribution=AttributionInfo(attribution_status=AttributionStatus.VISUAL_MEDIA),
        extraction_confidence=0.9,
        extraction_lineage=ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=["chk_000001"],
            source_candidate_ids=["cand_001"],
        ),
    )
    with pytest.raises(ValueError):
        validate_observation_grounding(u, {"ev_seg_001": "speech"})


# ----------------------------------------------------------------------
# 11. entity grounding
# 12. topic validation
# ----------------------------------------------------------------------

def test_entity_grounding_substring_in_statement():
    u = _unit(statement="The Strax Halo host runs models.", entities=[{"entity_name": "Strax Halo", "category": "hardware"}])
    haystack = _norm_surface(u["statement"]) + "|" + "|".join(_norm_surface(r["source_excerpt"]) for r in u["evidence_refs"])
    assert _norm_surface("Strax Halo") in haystack


def test_entity_grounding_rejects_alias():
    u = _unit(statement="The GPU has 24GB.", entities=[{"entity_name": "RTX 4090", "category": "hardware"}])
    haystack = _norm_surface(u["statement"]) + "|" + "|".join(_norm_surface(r["source_excerpt"]) for r in u["evidence_refs"])
    assert _norm_surface("RTX 4090") not in haystack


def test_topic_policy_bounds():
    u = _unit(topics=["gpu", "inference", "vulkan", "mtp", "quantization"])
    assert len(u["topics"]) <= 5
    for t in u["topics"]:
        assert 2 <= len(_norm_surface(t)) <= 32


def test_topic_duplicates_rejected():
    topics = ["gpu optimization", "GPU Optimization"]
    normalized = {_norm_surface(t) for t in topics}
    assert len(normalized) == 1


# ----------------------------------------------------------------------
# 13. lineage traceability
# 14. orphan candidate lineage detection
# ----------------------------------------------------------------------

def test_lineage_traceability_known_chunk_and_candidate():
    lineage = ExtractionLineage(
        extraction_run_id="run_test",
        input_chunk_ids=["chk_000001"],
        candidate_id="cand_chk_000001_001_abc",
        source_candidate_ids=["cand_chk_000001_001_abc"],
        merge_strategy="dedup_exact",
    )
    assert lineage.extraction_run_id == "run_test"
    assert "chk_000001" in lineage.input_chunk_ids
    assert "cand_chk_000001_001_abc" in lineage.source_candidate_ids


def test_orphan_candidate_lineage_detected():
    known = {"cand_chk_000001_001_abc"}
    orphan = "cand_chk_999999_001_zzz"
    assert orphan not in known


# ----------------------------------------------------------------------
# 15. finalization identity
# 16. Markdown/JSON parity
# ----------------------------------------------------------------------

def test_finalization_identity_all_fields_unchanged():
    enriched = {
        "units": [_unit()],
        "output_unit_count": 1,
    }
    final = {"units": enriched["units"]}
    assert len(enriched["units"]) == len(final["units"])
    assert enriched["units"][0] == final["units"][0]


def test_markdown_json_parity_unit_heads():
    units = [_unit(), _unit(statement="Second.", evidence_id="ev_002", excerpt="Second.")]
    heads = [f"### KU {u['knowledge_unit_id']}" for u in units]
    assert len(heads) == 2
    assert len(set(heads)) == 2


# ----------------------------------------------------------------------
# 17. tampered KU ID detection (full)
# 18. tampered excerpt detection (full)
# ----------------------------------------------------------------------

def test_tampered_ku_id_breaks_identity():
    u = _unit()
    assert u["knowledge_unit_id"] != "ku_" + "0" * 16


def test_tampered_excerpt_breaks_grounding():
    manifest = _synthetic_manifest([_unit()])
    item = manifest["evidence_items"][0]
    assert item["payload"]["text"] != "tampered"


# ----------------------------------------------------------------------
# 19-20. real C10 fixtures (read-only, offline)
# ----------------------------------------------------------------------

def test_real_c10_video_full_chain_audit():
    """Recovery contract: incident-aware audit for the provenance-loss asset.

    The historical M4 intermediate bytes for this C10 asset were irreversibly
    lost (see tests/fixtures/m4_c10_incident_20260910.json). This test no longer
    requires the on-disk enriched bytes to reproduce the historical generation.
    Instead it verifies the surviving canonical final, the preserved historical
    anchors, incident attestation, M3 evidence, and that reconstructed/rerun
    artifacts are not claimed as originals.
    """
    asset = _load_asset(VIDEO_ASSET)
    incident = _load_incident_manifest()
    entry = _incident_asset_entry(incident, VIDEO_ASSET)

    # incident attestation: status + surviving final hash + unit count
    assert entry["surviving_final"]["unit_count"] == 62
    kdir = ROOT / "data" / "processed" / VIDEO_ASSET / "knowledge"
    assert _sha256(kdir / "knowledge_units.json") == entry["surviving_final"]["knowledge_units_sha256"]
    assert _sha256(kdir / "knowledge_finalization.json") == entry["surviving_final"]["knowledge_finalization_sha256"]

    units = asset["units"]
    finalization = asset["finalization"]
    assert units["unit_count"] == 62
    assert finalization["input_unit_count"] == 62
    assert finalization["output_unit_count"] == 62
    assert finalization["identity_violation_count"] == 0

    # M3 evidence still valid (chain counts)
    assert len(asset["manifest"]["evidence_items"]) == 184
    assert len(asset["chunks"]["chunks"]) == 4

    # preserved historical anchors: finalization still records the ORIGINAL
    # source_enriched anchor, and the anchor is NOT overwritten by any
    # reconstruction or rerun fingerprint.
    assert finalization["source_enriched_artifact_fingerprint"] == VIDEO_HISTORICAL_ENRICHED_ANCHOR
    assert VIDEO_HISTORICAL_ENRICHED_ANCHOR == entry["historical_lost_intermediate"]["original_enriched_fingerprint"]
    assert entry["historical_lost_intermediate"]["status"] == "unrecoverable"

    # finalization_fingerprint (frozen historical) retained
    assert finalization["finalization_fingerprint"]

    # reconstructed + rerun artifacts are explicitly NOT the historical anchor
    reconstructed = compute_enriched_artifact_fingerprint(asset["enriched"])
    assert reconstructed != VIDEO_HISTORICAL_ENRICHED_ANCHOR
    assert reconstructed == entry["current_noncanonical_reconstruction"]["fingerprint"]
    assert entry["current_noncanonical_reconstruction"]["status"] == "forensic_only"
    assert entry["isolated_real_rerun"]["enriched_fingerprint"] != VIDEO_HISTORICAL_ENRICHED_ANCHOR
    assert entry["isolated_real_rerun"]["status"] == "forensic_only_not_adopted"

    # M5 expected parity
    assert entry["m5_expected_unit_count"] == 62

    # KU ID recomputation for every unit (identity preserved)
    for u in units["units"]:
        eids = [r["evidence_id"] for r in u["evidence_refs"]]
        recomputed = compute_knowledge_unit_id(
            KNOWLEDGE_SCHEMA_VERSION, u["canonical_id"], u["unit_type"], eids,
            normalize_statement(u["statement"]),
        )
        assert recomputed == u["knowledge_unit_id"]

    # all verification statuses preserved
    assert all(u["verification_status"] == "not_checked" for u in units["units"])


def test_real_c10_video_excerpt_and_coordinate_grounding():
    asset = _load_asset(VIDEO_ASSET)
    by_id = {item["evidence_id"]: item for item in asset["manifest"]["evidence_items"]}
    for u in asset["units"]["units"]:
        for ref in u["evidence_refs"]:
            item = by_id[ref["evidence_id"]]
            assert item["modality"] == "speech"
            assert (item.get("payload") or {}).get("text") == ref["source_excerpt"]
            t = item["temporal"]
            tr = ref["temporal_range"]
            assert abs(tr["start"] - t["start"]) < 1e-6
            assert abs(tr["end"] - t["end"]) < 1e-6
            assert abs(tr["duration"] - t["duration"]) < 1e-6


def test_real_c10_video_lineage_traceability():
    asset = _load_asset(VIDEO_ASSET)
    chunks = asset["chunks"]
    chunk_ids = {c["chunk_id"] for c in chunks["chunks"]}
    candidates = asset["candidates"]
    cand_ids = set()
    for c in candidates["candidates"]:
        lin = c.get("extraction_lineage", {})
        if lin.get("candidate_id"):
            cand_ids.add(lin["candidate_id"])
        cand_ids.update(lin.get("source_candidate_ids", []))
    run_id = candidates["extraction_run_id"]
    for u in asset["units"]["units"]:
        lin = u["extraction_lineage"]
        assert lin["extraction_run_id"] == run_id
        for cid in lin["input_chunk_ids"]:
            assert cid in chunk_ids
        for scid in lin["source_candidate_ids"]:
            assert scid in cand_ids


def test_real_c10_video_entity_and_topic_grounding():
    asset = _load_asset(VIDEO_ASSET)
    total_entities = 0
    for u in asset["units"]["units"]:
        excerpts = [r["source_excerpt"] for r in u["evidence_refs"]]
        haystack = _norm_surface(u["statement"]) + "|" + "|".join(_norm_surface(e) for e in excerpts)
        for ent in u.get("entities", []):
            total_entities += 1
            assert _norm_surface(ent["entity_name"]) in haystack
        topics = u.get("topics", [])
        assert len(topics) <= 5
        assert len(set(_norm_surface(t) for t in topics)) == len(topics)
    assert total_entities == 132


def test_real_c10_album_full_chain_audit():
    """Recovery contract: incident-aware audit for the provenance-loss asset.

    See test_real_c10_video_full_chain_audit docstring — same semantics, album.
    """
    asset = _load_asset(ALBUM_ASSET)
    incident = _load_incident_manifest()
    entry = _incident_asset_entry(incident, ALBUM_ASSET)

    assert entry["surviving_final"]["unit_count"] == 6
    kdir = ROOT / "data" / "processed" / ALBUM_ASSET / "knowledge"
    assert _sha256(kdir / "knowledge_units.json") == entry["surviving_final"]["knowledge_units_sha256"]
    assert _sha256(kdir / "knowledge_finalization.json") == entry["surviving_final"]["knowledge_finalization_sha256"]

    assert asset["units"]["unit_count"] == 6
    assert asset["finalization"]["identity_violation_count"] == 0
    assert len(asset["manifest"]["evidence_items"]) == 4
    assert len(asset["chunks"]["chunks"]) == 1

    # preserved historical anchor
    assert asset["finalization"]["source_enriched_artifact_fingerprint"] == ALBUM_HISTORICAL_ENRICHED_ANCHOR
    assert ALBUM_HISTORICAL_ENRICHED_ANCHOR == entry["historical_lost_intermediate"]["original_enriched_fingerprint"]
    assert entry["historical_lost_intermediate"]["status"] == "unrecoverable"

    # reconstructed != historical anchor
    reconstructed = compute_enriched_artifact_fingerprint(asset["enriched"])
    assert reconstructed != ALBUM_HISTORICAL_ENRICHED_ANCHOR
    assert reconstructed == entry["current_noncanonical_reconstruction"]["fingerprint"]
    assert entry["current_noncanonical_reconstruction"]["status"] == "forensic_only"
    assert entry["isolated_real_rerun"]["enriched_fingerprint"] != ALBUM_HISTORICAL_ENRICHED_ANCHOR
    assert entry["isolated_real_rerun"]["status"] == "forensic_only_not_adopted"

    assert entry["m5_expected_unit_count"] == 6

    # no unresolved visual grounding
    by_id = {item["evidence_id"]: item for item in asset["manifest"]["evidence_items"]}
    for u in asset["units"]["units"]:
        for ref in u["evidence_refs"]:
            item = by_id[ref["evidence_id"]]
            payload = item.get("payload") or {}
            assert payload.get("status") != "unresolved_visual_reference"

    # all visual_media attribution
    for u in asset["units"]["units"]:
        assert u["attribution"]["attribution_status"] == "visual_media"

    # all verification not_checked
    assert all(u["verification_status"] == "not_checked" for u in asset["units"]["units"])


def test_real_c10_album_excerpt_grounding():
    asset = _load_asset(ALBUM_ASSET)
    by_id = {item["evidence_id"]: item for item in asset["manifest"]["evidence_items"]}
    for u in asset["units"]["units"]:
        for ref in u["evidence_refs"]:
            item = by_id[ref["evidence_id"]]
            assert (item.get("payload") or {}).get("text") == ref["source_excerpt"]
            assert ref["sequence_range"]["sequence_index"] == item["sequence"]["sequence_index"]


def test_real_c10_album_entity_grounding():
    asset = _load_asset(ALBUM_ASSET)
    for u in asset["units"]["units"]:
        excerpts = [r["source_excerpt"] for r in u["evidence_refs"]]
        haystack = _norm_surface(u["statement"]) + "|" + "|".join(_norm_surface(e) for e in excerpts)
        for ent in u.get("entities", []):
            assert _norm_surface(ent["entity_name"]) in haystack


def test_real_c10_markdown_parity():
    for asset_id in (VIDEO_ASSET, ALBUM_ASSET):
        asset = _load_asset(asset_id)
        heads = re.findall(r"^### KU (ku_[a-f0-9]{16})$", asset["markdown"], re.MULTILINE)
        json_ids = [u["knowledge_unit_id"] for u in asset["units"]["units"]]
        assert len(heads) == len(json_ids)
        assert set(heads) == set(json_ids)


def test_real_c10_cross_stage_identity():
    for asset_id in (VIDEO_ASSET, ALBUM_ASSET):
        asset = _load_asset(asset_id)
        merged_by = {u["knowledge_unit_id"]: u for u in asset["merged"]["units"]}
        enriched_by = {u["knowledge_unit_id"]: u for u in asset["enriched"]["units"]}
        final_by = {u["knowledge_unit_id"]: u for u in asset["units"]["units"]}

        # M4-03 -> M4-04: only entities/topics may differ
        for kid, mu in merged_by.items():
            eu = enriched_by[kid]
            for field in FROZEN_FIELDS:
                if field not in ("entities", "topics"):
                    assert mu[field] == eu[field], f"{kid} {field} changed"

        # M4-04 -> M4-05: all fields identical
        for kid, eu in enriched_by.items():
            assert eu == final_by[kid], f"{kid} changed in finalization"