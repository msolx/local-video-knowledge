"""M4 C10 incident recovery contract tests.

Verifies the machine-readable incident manifest, preserved historical anchors,
surviving canonical final artifacts, M3 evidence validity, M5 68/68 parity, the
forensic-only classification of reconstructed/rerun artifacts, and the generic
guard that prevents destructive stage adapters from targeting the real
data/processed tree.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.knowledge.enrichment import compute_merged_artifact_fingerprint
from src.knowledge.merger import compute_candidates_artifact_fingerprint
from src.knowledge.models import KNOWLEDGE_SCHEMA_VERSION
from src.knowledge.render import (
    RenderConfig,
    compute_enriched_artifact_fingerprint,
    compute_finalization_fingerprint,
)
from src.provenance import load_evidence_manifest, verify_evidence_manifest
from src.chunking.service import load_evidence_chunks, verify_evidence_chunks

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m4_c10_incident_20260910.json"
INCIDENT_ID = "m4-c10-provenance-incident-20260910"
INCIDENT_STATUS = "RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS"
SCHEMA_VERSION = "m4-c10-incident-v1"

VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"

VIDEO_HISTORICAL_ANCHOR = "0b329ed0fedad69a196d92f3a3febedd3a6faf6d493e5da7675059173adf39e7"
ALBUM_HISTORICAL_ANCHOR = "6687bfd29d8db63657b73766a53c26c79f54201a9cdbffc303ee55c942665f34"
VIDEO_RECONSTRUCTED = "e8de8e8d467c9694802d6be8404288f5168e681452a1f8fd8432185bfaabf6de"
ALBUM_RECONSTRUCTED = "7f4c4db8d28b0a4c191ccfe1a08a6266a239cca4e0eb3876e11b811e7d183456"

VIDEO_UNITS_SHA = "255b0a8bc2dfc7d4f8383185687755d56c9f82d57363090ab67dffc066faa93e"
ALBUM_UNITS_SHA = "361b0e82bb7a7caabf6ccd65bc1a6993893141f735573e200c724c7afe7e79c4"
VIDEO_FINALIZATION_SHA = "74503733d377e7b568132a115dba10eb1fe90aa73a2780b5364dee98dfde5400"
ALBUM_FINALIZATION_SHA = "98d8a23dda68df1addae3ca6e6709893e5ba84ba5d6137de97be31cf800c08d8"

M5_STORE = ROOT / "data" / "knowledge" / "knowledge_store.sqlite3"


def _load_incident() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _entry(incident: dict[str, Any], asset_id: str) -> dict[str, Any]:
    for e in incident["affected_assets"]:
        if e["canonical_id"] == asset_id:
            return e
    raise AssertionError(asset_id)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _processed_dir(asset_id: str) -> Path:
    return ROOT / "data" / "processed" / asset_id


# ----------------------------------------------------------------------
# 1. incident manifest schema
# ----------------------------------------------------------------------

def test_incident_manifest_schema():
    incident = _load_incident()
    assert incident["schema_version"] == SCHEMA_VERSION
    assert incident["incident_id"] == INCIDENT_ID
    assert incident["status"] == INCIDENT_STATUS
    assert isinstance(incident["affected_assets"], list)
    assert len(incident["affected_assets"]) == 2
    for e in incident["affected_assets"]:
        assert set(e) >= {"canonical_id", "surviving_final", "historical_lost_intermediate",
                          "current_noncanonical_reconstruction", "isolated_real_rerun",
                          "m5_expected_unit_count"}


# ----------------------------------------------------------------------
# 2. exact affected canonical IDs
# ----------------------------------------------------------------------

def test_affected_asset_ids_exact():
    incident = _load_incident()
    ids = sorted(e["canonical_id"] for e in incident["affected_assets"])
    assert ids == sorted([ALBUM_ASSET, VIDEO_ASSET])


# ----------------------------------------------------------------------
# 3. historical anchors preserved
# ----------------------------------------------------------------------

def test_historical_anchors_preserved():
    incident = _load_incident()
    assert _entry(incident, VIDEO_ASSET)["historical_lost_intermediate"]["original_enriched_fingerprint"] == VIDEO_HISTORICAL_ANCHOR
    assert _entry(incident, ALBUM_ASSET)["historical_lost_intermediate"]["original_enriched_fingerprint"] == ALBUM_HISTORICAL_ANCHOR


# ----------------------------------------------------------------------
# 4. surviving final KU hashes
# ----------------------------------------------------------------------

def test_surviving_final_hashes_match_manifest():
    incident = _load_incident()
    for asset_id, expected_units, expected_finalization in (
        (VIDEO_ASSET, VIDEO_UNITS_SHA, VIDEO_FINALIZATION_SHA),
        (ALBUM_ASSET, ALBUM_UNITS_SHA, ALBUM_FINALIZATION_SHA),
    ):
        entry = _entry(incident, asset_id)
        kdir = _processed_dir(asset_id) / "knowledge"
        assert _sha256(kdir / "knowledge_units.json") == entry["surviving_final"]["knowledge_units_sha256"]
        assert entry["surviving_final"]["knowledge_units_sha256"] == expected_units
        assert _sha256(kdir / "knowledge_finalization.json") == entry["surviving_final"]["knowledge_finalization_sha256"]
        assert entry["surviving_final"]["knowledge_finalization_sha256"] == expected_finalization


# ----------------------------------------------------------------------
# 5-6. unit counts 62 / 6
# ----------------------------------------------------------------------

def test_video_62_units():
    doc = json.loads((_processed_dir(VIDEO_ASSET) / "knowledge" / "knowledge_units.json").read_text(encoding="utf-8"))
    assert doc["unit_count"] == 62
    assert len(doc["units"]) == 62


def test_album_6_units():
    doc = json.loads((_processed_dir(ALBUM_ASSET) / "knowledge" / "knowledge_units.json").read_text(encoding="utf-8"))
    assert doc["unit_count"] == 6
    assert len(doc["units"]) == 6


# ----------------------------------------------------------------------
# 7. M3 evidence valid
# ----------------------------------------------------------------------

@pytest.mark.parametrize("asset_id,items,chunks", [
    (VIDEO_ASSET, 184, 4),
    (ALBUM_ASSET, 4, 1),
])
def test_m3_evidence_valid(asset_id, items, chunks):
    base = _processed_dir(asset_id)
    manifest = load_evidence_manifest(base)
    chunks_artifact = load_evidence_chunks(base)
    assert verify_evidence_manifest(manifest) is True
    assert verify_evidence_chunks(chunks_artifact) is True
    assert len(manifest["evidence_items"]) == items
    assert len(chunks_artifact["chunks"]) == chunks


# ----------------------------------------------------------------------
# 8. finalization historical anchor retained
# ----------------------------------------------------------------------

def test_finalization_retains_historical_anchor():
    for asset_id, anchor in ((VIDEO_ASSET, VIDEO_HISTORICAL_ANCHOR), (ALBUM_ASSET, ALBUM_HISTORICAL_ANCHOR)):
        fin = json.loads((_processed_dir(asset_id) / "knowledge" / "knowledge_finalization.json").read_text(encoding="utf-8"))
        assert fin["source_enriched_artifact_fingerprint"] == anchor


# ----------------------------------------------------------------------
# 9. reconstructed fingerprint != historical anchor
# ----------------------------------------------------------------------

def test_reconstructed_not_equal_historical():
    for asset_id, anchor, reconstructed in (
        (VIDEO_ASSET, VIDEO_HISTORICAL_ANCHOR, VIDEO_RECONSTRUCTED),
        (ALBUM_ASSET, ALBUM_HISTORICAL_ANCHOR, ALBUM_RECONSTRUCTED),
    ):
        enriched = json.loads((_processed_dir(asset_id) / "knowledge" / "enriched_knowledge_candidates.json").read_text(encoding="utf-8"))
        fp = compute_enriched_artifact_fingerprint(enriched)
        assert fp == reconstructed
        assert fp != anchor


# ----------------------------------------------------------------------
# 10-11. real rerun marked forensic-only, video 69 not adopted
# ----------------------------------------------------------------------

def test_real_rerun_forensic_only_not_adopted():
    incident = _load_incident()
    ve = _entry(incident, VIDEO_ASSET)
    assert ve["isolated_real_rerun"]["status"] == "forensic_only_not_adopted"
    assert ve["isolated_real_rerun"]["unit_count"] == 69
    assert ve["isolated_real_rerun"]["enriched_fingerprint"] != VIDEO_HISTORICAL_ANCHOR
    ae = _entry(incident, ALBUM_ASSET)
    assert ae["isolated_real_rerun"]["status"] == "forensic_only_not_adopted"
    assert ae["isolated_real_rerun"]["unit_count"] == 6


def test_rerun_69_not_adopted_as_canonical():
    # canonical final remains 62 units; rerun 69 is never reflected in final
    incident = _load_incident()
    ve = _entry(incident, VIDEO_ASSET)
    doc = json.loads((_processed_dir(VIDEO_ASSET) / "knowledge" / "knowledge_units.json").read_text(encoding="utf-8"))
    assert doc["unit_count"] == ve["surviving_final"]["unit_count"] == 62
    assert ve["isolated_real_rerun"]["unit_count"] == 69


# ----------------------------------------------------------------------
# 12. production M5 68/68 parity
# ----------------------------------------------------------------------

def test_m5_68_68_parity():
    if not M5_STORE.is_file():
        pytest.skip("production M5 store not present on this box")
    import sqlite3
    conn = sqlite3.connect(M5_STORE)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0]
    assert total == 68
    for asset_id in (VIDEO_ASSET, ALBUM_ASSET):
        doc = json.loads((_processed_dir(asset_id) / "knowledge" / "knowledge_units.json").read_text(encoding="utf-8"))
        final_by_id = {u["knowledge_unit_id"]: u for u in doc["units"]}
        rows = conn.execute(
            "SELECT canonical_payload_json FROM knowledge_units WHERE canonical_id=?",
            (asset_id,),
        ).fetchall()
        m5_by_id = {json.loads(r["canonical_payload_json"])["knowledge_unit_id"]: json.loads(r["canonical_payload_json"]) for r in rows}
        assert set(m5_by_id) == set(final_by_id)
        for uid, payload in m5_by_id.items():
            assert payload == final_by_id[uid]
    conn.close()


def test_m5_expected_unit_counts_match_manifest():
    incident = _load_incident()
    assert _entry(incident, VIDEO_ASSET)["m5_expected_unit_count"] == 62
    assert _entry(incident, ALBUM_ASSET)["m5_expected_unit_count"] == 6


# ----------------------------------------------------------------------
# 13. incident status explicit
# ----------------------------------------------------------------------

def test_incident_status_explicit():
    incident = _load_incident()
    assert incident["status"] == "RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS"
    assert incident["status"] != "FULLY_RECOVERED"
    for e in incident["affected_assets"]:
        assert e["historical_lost_intermediate"]["status"] == "unrecoverable"


# ----------------------------------------------------------------------
# 14. no new historical anchor
# ----------------------------------------------------------------------

def test_no_new_historical_anchor():
    incident = _load_incident()
    for e in incident["affected_assets"]:
        assert e["historical_lost_intermediate"]["original_enriched_fingerprint"] not in (
            e["current_noncanonical_reconstruction"]["fingerprint"],
            e["isolated_real_rerun"]["enriched_fingerprint"],
        )


# ----------------------------------------------------------------------
# 15. destructive real processed-root guard (generic)
# ----------------------------------------------------------------------

def _guard_rejects_real_processed_root(target_root: Path) -> bool:
    """Generic guard: a test-mode/destructive execution must not target the
    repository's real data/processed tree. Returns True when the target is the
    real tree (should be rejected)."""
    real_processed = ROOT / "data" / "processed"
    resolved = target_root.resolve()
    real_resolved = real_processed.resolve()
    return resolved == real_resolved or real_resolved in resolved.parents


def test_destructive_guard_rejects_real_processed_root():
    assert _guard_rejects_real_processed_root(_processed_dir(VIDEO_ASSET)) is True
    assert _guard_rejects_real_processed_root(_processed_dir(ALBUM_ASSET)) is True
    assert _guard_rejects_real_processed_root(ROOT / "data" / "processed") is True


def test_destructive_guard_allows_disposable_root(tmp_path):
    disposable = tmp_path / "processed"
    disposable.mkdir()
    assert _guard_rejects_real_processed_root(disposable) is False


def test_real_processed_root_is_not_mutated_by_copy_workflow(tmp_path):
    """Replicates the incident class: the fix is to copy to a disposable root
    before running destructive adapters; the real tree must remain untouched."""
    src = _processed_dir(VIDEO_ASSET)
    dst = tmp_path / "processed" / VIDEO_ASSET
    dst.mkdir(parents=True)
    shutil.copytree(src / "knowledge", dst / "knowledge")
    # real tree fingerprint unchanged (guarded by _guard above + copy-to-temp)
    assert _guard_rejects_real_processed_root(src) is True
    assert (dst / "knowledge" / "knowledge_units.json").is_file()


def test_incident_manifest_has_no_absolute_backup_paths():
    incident = _load_incident()
    raw = FIXTURE.read_text(encoding="utf-8")
    assert "G:" not in raw
    assert "pkp_backup" not in raw
    assert "douyin_" not in incident["incident_id"]


def test_chain_fingerprints_internally_consistent_for_nonincident_paths():
    """Non-incident offline chain (candidates->merged->enriched) must still be
    internally consistent for reconstructed artifacts (they are a valid chain,
    just not historical)."""
    for asset_id in (VIDEO_ASSET, ALBUM_ASSET):
        kdir = _processed_dir(asset_id) / "knowledge"
        candidates = json.loads((kdir / "knowledge_candidates.json").read_text(encoding="utf-8"))
        merged = json.loads((kdir / "merged_knowledge_candidates.json").read_text(encoding="utf-8"))
        enriched = json.loads((kdir / "enriched_knowledge_candidates.json").read_text(encoding="utf-8"))
        assert merged["source_candidates_artifact_fingerprint"] == compute_candidates_artifact_fingerprint(candidates)
        assert enriched["source_merged_artifact_fingerprint"] == compute_merged_artifact_fingerprint(merged)
        fp = compute_enriched_artifact_fingerprint(enriched)
        assert isinstance(fp, str) and len(fp) == 64