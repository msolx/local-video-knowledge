"""Milestone M4-06 End-to-End Acceptance Audit Runner (read-only).

Performs a full deterministic, offline acceptance audit over the two real C10
formal assets:

  - douyin_7681603850364521734 (video)
  - douyin_7682038498466993905 (image album)

Chain under audit (M3 -> M4-02 -> M4-03 -> M4-04 -> M4-05):

  evidence_manifest.json + evidence_chunks.json
      -> knowledge_candidates.json
      -> merged_knowledge_candidates.json
      -> enriched_knowledge_candidates.json
      -> knowledge_units.json + knowledge.md + knowledge_finalization.json

This script NEVER mutates any knowledge artifact. It only reads, validates, and
emits an acceptance summary. It starts no LLM runtime and performs no inference.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# Prevent Windows GBK console encoding crashes on Unicode characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.enrichment import compute_merged_artifact_fingerprint
from src.knowledge.merger import compute_candidates_artifact_fingerprint
from src.knowledge.models import (
    KNOWLEDGE_SCHEMA_VERSION,
    compute_knowledge_unit_id,
    normalize_statement,
)
from src.knowledge.render import (
    RenderConfig,
    compute_enriched_artifact_fingerprint,
    compute_finalization_fingerprint,
)

VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"

PERCEPTUAL_MODALITIES = {"visual_text", "visual_description", "perceptual_metric"}


def norm_surface(text: str) -> str:
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    return re.sub(r"\s+", " ", normalized).strip()


# ----------------------------------------------------------------------
# Artifact loading
# ----------------------------------------------------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_asset(knowledge_dir: Path, asset_id: str) -> dict[str, Any]:
    base = knowledge_dir.parent  # data/processed/<asset_id>
    return {
        "asset_id": asset_id,
        "processed_dir": knowledge_dir.parent,
        "knowledge_dir": knowledge_dir,
        "manifest": load_json(base / "evidence_manifest.json"),
        "chunks": load_json(base / "evidence_chunks.json"),
        "candidates": load_json(knowledge_dir / "knowledge_candidates.json"),
        "merged": load_json(knowledge_dir / "merged_knowledge_candidates.json"),
        "enriched": load_json(knowledge_dir / "enriched_knowledge_candidates.json"),
        "units": load_json(knowledge_dir / "knowledge_units.json"),
        "finalization": load_json(knowledge_dir / "knowledge_finalization.json"),
        "markdown": (knowledge_dir / "knowledge.md").read_text(encoding="utf-8"),
    }


# ----------------------------------------------------------------------
# Audit 1: artifact chain counts
# ----------------------------------------------------------------------

def audit_chain_counts(asset: dict[str, Any]) -> dict[str, Any]:
    manifest = asset["manifest"]
    chunks = asset["chunks"]
    candidates = asset["candidates"]
    merged = asset["merged"]
    enriched = asset["enriched"]
    units = asset["units"]

    manifest_items = len(manifest.get("evidence_items", []))
    chunk_count = len(chunks.get("chunks", []))
    candidate_count = len(candidates.get("candidates", []))
    merged_count = len(merged.get("units", []))
    enriched_count = len(enriched.get("units", []))
    final_count = len(units.get("units", []))

    expected = {
        "manifest_items": manifest_items,
        "chunk_count": chunk_count,
        "candidate_count": candidate_count,
        "merged_count": merged_count,
        "enriched_count": enriched_count,
        "final_count": final_count,
        "final_schema": units.get("schema_version"),
        "final_generated_at": units.get("generated_at"),
        "manifest_fingerprint": manifest.get("manifest_fingerprint"),
        "chunks_fingerprint": chunks.get("chunks_fingerprint"),
        "candidates_extraction_run_id": candidates.get("extraction_run_id"),
    }
    # Intra-stage count identity
    stage_ok = (
        merged_count == candidate_count
        and enriched_count == merged_count
        and final_count == enriched_count
    )
    return {**expected, "stage_count_identity": stage_ok}


# ----------------------------------------------------------------------
# Audit 2: artifact fingerprint chain
# ----------------------------------------------------------------------

def audit_fingerprint_chain(asset: dict[str, Any]) -> dict[str, Any]:
    manifest = asset["manifest"]
    chunks = asset["chunks"]
    candidates = asset["candidates"]
    merged = asset["merged"]
    enriched = asset["enriched"]
    finalization = asset["finalization"]

    provenance = candidates.get("provenance", {})
    findings: list[dict[str, Any]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        findings.append({"check": label, "ok": bool(ok), "detail": detail})

    # M3 self-link: chunks reference manifest
    check(
        "chunks.source_manifest_fingerprint == manifest.manifest_fingerprint",
        chunks.get("source_manifest_fingerprint") == manifest.get("manifest_fingerprint"),
        f"{chunks.get('source_manifest_fingerprint')} vs {manifest.get('manifest_fingerprint')}",
    )

    # M4-02 provenance references M3 fingerprints
    check(
        "candidates.provenance.evidence_manifest_fingerprint == manifest.manifest_fingerprint",
        provenance.get("evidence_manifest_fingerprint") == manifest.get("manifest_fingerprint"),
    )
    check(
        "candidates.provenance.evidence_chunks_fingerprint == chunks.chunks_fingerprint",
        provenance.get("evidence_chunks_fingerprint") == chunks.get("chunks_fingerprint"),
    )

    # M4-03: merged.source_candidates_artifact_fingerprint == fp(knowledge_candidates.json)
    cand_fp = compute_candidates_artifact_fingerprint(candidates)
    check(
        "merged.source_candidates_artifact_fingerprint == fp(knowledge_candidates.json)",
        merged.get("source_candidates_artifact_fingerprint") == cand_fp,
        f"computed={cand_fp} stored={merged.get('source_candidates_artifact_fingerprint')}",
    )

    # M4-04: enriched.source_merged_artifact_fingerprint == fp(merged_knowledge_candidates.json)
    merged_fp = compute_merged_artifact_fingerprint(merged)
    check(
        "enriched.source_merged_artifact_fingerprint == fp(merged_knowledge_candidates.json)",
        enriched.get("source_merged_artifact_fingerprint") == merged_fp,
        f"computed={merged_fp} stored={enriched.get('source_merged_artifact_fingerprint')}",
    )

    # M4-05: finalization.source_enriched_artifact_fingerprint == fp(enriched_knowledge_candidates.json)
    enriched_fp = compute_enriched_artifact_fingerprint(enriched)
    check(
        "finalization.source_enriched_artifact_fingerprint == fp(enriched_knowledge_candidates.json)",
        finalization.get("source_enriched_artifact_fingerprint") == enriched_fp,
        f"computed={enriched_fp} stored={finalization.get('source_enriched_artifact_fingerprint')}",
    )

    # M4-05: finalization fingerprint recomputable
    fin_fp = compute_finalization_fingerprint(enriched_fp, KNOWLEDGE_SCHEMA_VERSION, RenderConfig())
    check(
        "finalization.finalization_fingerprint recomputable",
        finalization.get("finalization_fingerprint") == fin_fp,
        f"computed={fin_fp} stored={finalization.get('finalization_fingerprint')}",
    )

    return {
        "findings": findings,
        "valid": all(f["ok"] for f in findings),
        "candidates_artifact_fingerprint": cand_fp,
        "merged_artifact_fingerprint": merged_fp,
        "enriched_artifact_fingerprint": enriched_fp,
        "finalization_fingerprint": fin_fp,
    }


# ----------------------------------------------------------------------
# Audit 3: final KU ID recomputation
# ----------------------------------------------------------------------

def audit_ku_id_recomputation(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    checked = 0
    mismatches: list[dict[str, Any]] = []
    for unit in units:
        eids = [ref["evidence_id"] for ref in unit["evidence_refs"]]
        recomputed = compute_knowledge_unit_id(
            schema_version=KNOWLEDGE_SCHEMA_VERSION,
            canonical_id=unit["canonical_id"],
            unit_type=unit["unit_type"],
            evidence_refs=eids,
            normalized_statement=normalize_statement(unit["statement"]),
        )
        checked += 1
        if recomputed != unit["knowledge_unit_id"]:
            mismatches.append(
                {"ku_id": unit["knowledge_unit_id"], "recomputed": recomputed}
            )
    return {
        "checked_count": checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "valid": len(mismatches) == 0,
    }


# ----------------------------------------------------------------------
# Audit 4: full evidence grounding audit
# ----------------------------------------------------------------------

def audit_evidence_grounding(asset: dict[str, Any]) -> dict[str, Any]:
    manifest = asset["manifest"]
    chunks = asset["chunks"]
    units = asset["units"]["units"]
    by_id = {item["evidence_id"]: item for item in manifest["evidence_items"]}
    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks["chunks"]}

    ku_count = len(units)
    ref_count = 0
    violations: list[dict[str, Any]] = []
    manifest_eids = set(by_id.keys())
    chunk_eids_by_chunk = {
        cid: set(chunk["evidence_ids"]) for cid, chunk in chunk_by_id.items()
    }

    for unit in units:
        kid = unit["knowledge_unit_id"]
        last_key: Optional[tuple] = None
        for ref in unit["evidence_refs"]:
            eid = ref["evidence_id"]
            ref_count += 1
            excerpt = ref.get("source_excerpt", "")
            tr = ref.get("temporal_range")
            sr = ref.get("sequence_range")

            # 1. evidence_id exists in manifest
            if eid not in manifest_eids:
                violations.append({"ku_id": kid, "rule": "eid_not_in_manifest", "evidence_id": eid})
                continue
            item = by_id[eid]

            # 2. lineage chunk membership legal
            lineage_chunks = set(unit["extraction_lineage"]["input_chunk_ids"])
            for cid in lineage_chunks:
                if cid not in chunk_by_id:
                    violations.append({"ku_id": kid, "rule": "lineage_chunk_unknown", "detail": cid})
                elif eid not in chunk_eids_by_chunk[cid]:
                    violations.append(
                        {"ku_id": kid, "rule": "eid_not_in_lineage_chunk", "detail": f"{eid} not in {cid}"}
                    )

            # 3. source_excerpt == authoritative semantic payload
            payload_text = (item.get("payload") or {}).get("text")
            modality = item.get("modality")
            if modality == "speech":
                if payload_text != excerpt:
                    violations.append(
                        {"ku_id": kid, "rule": "excerpt_mismatch", "evidence_id": eid,
                         "detail": f"payload={payload_text!r} excerpt={excerpt!r}"}
                    )
            elif modality == "visual_text":
                if payload_text != excerpt:
                    violations.append(
                        {"ku_id": kid, "rule": "excerpt_mismatch", "evidence_id": eid,
                         "detail": f"payload={payload_text!r} excerpt={excerpt!r}"}
                    )

            # 4. temporal_range identical
            item_temporal = item.get("temporal")
            if modality == "speech" and item_temporal:
                if tr is None or not all(
                    abs(float(tr.get(k, -1)) - float(item_temporal.get(k, -2))) < 1e-6
                    for k in ("start", "end", "duration")
                ):
                    violations.append(
                        {"ku_id": kid, "rule": "temporal_mismatch", "evidence_id": eid,
                         "detail": f"ref={tr} item={item_temporal}"}
                    )
            elif modality == "speech" and not item_temporal:
                if tr is not None:
                    violations.append(
                        {"ku_id": kid, "rule": "temporal_not_null_for_speech", "evidence_id": eid}
                    )

            # 5. sequence_range identical
            item_seq = item.get("sequence")
            if modality == "visual_text" and item_seq:
                if sr is None or sr.get("sequence_index") != item_seq.get("sequence_index"):
                    violations.append(
                        {"ku_id": kid, "rule": "sequence_mismatch", "evidence_id": eid,
                         "detail": f"ref={sr} item={item_seq}"}
                    )
            elif modality == "visual_text" and not item_seq:
                if sr is not None:
                    violations.append(
                        {"ku_id": kid, "rule": "sequence_not_null_for_visual", "evidence_id": eid}
                    )

            # 6. no empty source_excerpt
            if not excerpt.strip():
                violations.append({"ku_id": kid, "rule": "empty_excerpt", "evidence_id": eid})

            # 7. no unresolved visual evidence grounding
            if modality == "visual_description":
                payload = item.get("payload") or {}
                if payload.get("status") == "unresolved_visual_reference":
                    violations.append(
                        {"ku_id": kid, "rule": "unresolved_visual_grounding", "evidence_id": eid}
                    )

            # 8. canonical ordering (temporal asc / sequence asc)
            key: Optional[tuple]
            if tr:
                key = ("t", tr["start"])
            elif sr:
                key = ("s", sr["sequence_index"])
            else:
                key = None
            if last_key is not None and key is not None and key < last_key:
                violations.append(
                    {"ku_id": kid, "rule": "evidence_order_regression", "detail": f"{last_key} -> {key}"}
                )
            if key is not None:
                last_key = key

    return {
        "ku_count": ku_count,
        "evidence_ref_count": ref_count,
        "violations_count": len(violations),
        "violations": violations[:30],
        "valid": len(violations) == 0,
    }


# ----------------------------------------------------------------------
# Audit 5: attribution audit
# ----------------------------------------------------------------------

def audit_attribution(asset: dict[str, Any]) -> dict[str, Any]:
    manifest = asset["manifest"]
    by_id = {item["evidence_id"]: item for item in manifest["evidence_items"]}
    units = asset["units"]["units"]
    violations: list[dict[str, Any]] = []
    status_counts: Counter = Counter()
    for unit in units:
        attr = unit["attribution"]
        status = attr["attribution_status"]
        status_counts[status] += 1
        kid = unit["knowledge_unit_id"]
        utype = unit["unit_type"]

        modalities = {by_id.get(r["evidence_id"], {}).get("modality") for r in unit["evidence_refs"]}
        is_visual = bool(modalities & PERCEPTUAL_MODALITIES)
        is_speech = "speech" in modalities

        if utype == "verification_question":
            if status != "system_derived":
                violations.append({"ku_id": kid, "rule": "vq_not_system_derived", "detail": status})
        elif is_visual and not is_speech:
            if status != "visual_media":
                violations.append({"ku_id": kid, "rule": "visual_not_visual_media", "detail": status})
        elif is_speech:
            if status != "unverified_speaker":
                violations.append({"ku_id": kid, "rule": "speech_not_unverified_speaker", "detail": status})
            if attr.get("speaker_name") is not None or attr.get("speaker_id") is not None:
                violations.append({"ku_id": kid, "rule": "speech_has_speaker_guess", "detail": str(attr)})
        else:
            violations.append({"ku_id": kid, "rule": "no_known_modality", "detail": str(modalities)})

    return {
        "status_counts": dict(status_counts),
        "violations_count": len(violations),
        "violations": violations[:20],
        "valid": len(violations) == 0,
    }


# ----------------------------------------------------------------------
# Audit 6: observation audit
# ----------------------------------------------------------------------

def audit_observations(asset: dict[str, Any]) -> dict[str, Any]:
    manifest = asset["manifest"]
    by_id = {item["evidence_id"]: item for item in manifest["evidence_items"]}
    units = asset["units"]["units"]
    observations = [u for u in units if u["unit_type"] == "observation"]
    invalid: list[dict[str, Any]] = []
    for unit in observations:
        modalities = [by_id.get(r["evidence_id"], {}).get("modality") for r in unit["evidence_refs"]]
        if not any(m in PERCEPTUAL_MODALITIES for m in modalities):
            invalid.append({"ku_id": unit["knowledge_unit_id"], "modalities": modalities})
    return {
        "observation_count": len(observations),
        "valid_count": len(observations) - len(invalid),
        "invalid_count": len(invalid),
        "invalid": invalid[:20],
        "valid": len(invalid) == 0,
    }


# ----------------------------------------------------------------------
# Audit 7: verification status audit
# ----------------------------------------------------------------------

def audit_verification(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    counts: Counter = Counter(u["verification_status"] for u in units)
    return {
        "status_counts": dict(counts),
        "all_not_checked": all(u["verification_status"] == "not_checked" for u in units),
    }


# ----------------------------------------------------------------------
# Audit 8: entity grounding audit
# ----------------------------------------------------------------------

def audit_entity_grounding(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    total_mentions = 0
    grounded = 0
    violations: list[dict[str, Any]] = []
    for unit in units:
        excerpts = [r["source_excerpt"] for r in unit["evidence_refs"]]
        haystack = norm_surface(unit["statement"]) + "|" + "|".join(norm_surface(e) for e in excerpts)
        for ent in unit.get("entities", []):
            total_mentions += 1
            if norm_surface(ent["entity_name"]) and norm_surface(ent["entity_name"]) in haystack:
                grounded += 1
            else:
                violations.append(
                    {"ku_id": unit["knowledge_unit_id"], "entity": ent["entity_name"]}
                )
    return {
        "total_entity_mentions": total_mentions,
        "grounded_count": grounded,
        "violations_count": len(violations),
        "violations": violations[:20],
        "valid": len(violations) == 0,
    }


# ----------------------------------------------------------------------
# Audit 9: topic policy audit
# ----------------------------------------------------------------------

def audit_topics(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    violations: list[dict[str, Any]] = []
    total_topics = 0
    units_with_topics = 0
    for unit in units:
        topics = unit.get("topics", [])
        if topics:
            units_with_topics += 1
        total_topics += len(topics)
        if len(topics) > 5:
            violations.append({"ku_id": unit["knowledge_unit_id"], "rule": "too_many", "count": len(topics)})
        seen = set()
        for t in topics:
            if not isinstance(t, str):
                violations.append({"ku_id": unit["knowledge_unit_id"], "rule": "not_string", "value": t})
                continue
            tn = norm_surface(t)
            if not (2 <= len(tn) <= 32):
                violations.append({"ku_id": unit["knowledge_unit_id"], "rule": "length", "value": t})
            if tn in seen:
                violations.append({"ku_id": unit["knowledge_unit_id"], "rule": "duplicate", "value": t})
            seen.add(tn)
    return {
        "units_with_topics": units_with_topics,
        "total_topics": total_topics,
        "violations_count": len(violations),
        "violations": violations[:20],
        "valid": len(violations) == 0,
    }


# ----------------------------------------------------------------------
# Audit 10: lineage audit
# ----------------------------------------------------------------------

def audit_lineage(asset: dict[str, Any]) -> dict[str, Any]:
    chunks = asset["chunks"]
    candidates = asset["candidates"]
    units = asset["units"]["units"]
    chunk_ids = {c["chunk_id"] for c in chunks["chunks"]}
    candidate_ids = set()
    for cand in candidates["candidates"]:
        lin = cand.get("extraction_lineage", {})
        if lin.get("candidate_id"):
            candidate_ids.add(lin["candidate_id"])
        candidate_ids.update(lin.get("source_candidate_ids", []))
    run_id = candidates.get("extraction_run_id")

    units_checked = 0
    orphan_lineage = 0
    invalid_chunk_ref = 0
    invalid_candidate_ref = 0
    details: list[dict[str, Any]] = []

    for unit in units:
        lin = unit["extraction_lineage"]
        units_checked += 1
        if lin.get("extraction_run_id") != run_id:
            orphan_lineage += 1
            details.append({"ku_id": unit["knowledge_unit_id"], "rule": "run_id_mismatch",
                            "detail": f"{lin.get('extraction_run_id')} vs {run_id}"})
        for cid in lin.get("input_chunk_ids", []):
            if cid not in chunk_ids:
                invalid_chunk_ref += 1
                details.append({"ku_id": unit["knowledge_unit_id"], "rule": "chunk_unknown", "detail": cid})
        for scid in lin.get("source_candidate_ids", []):
            if scid not in candidate_ids:
                invalid_candidate_ref += 1
                details.append({"ku_id": unit["knowledge_unit_id"], "rule": "candidate_orphan", "detail": scid})

    return {
        "units_checked": units_checked,
        "orphan_lineage_count": orphan_lineage,
        "invalid_chunk_ref_count": invalid_chunk_ref,
        "invalid_candidate_ref_count": invalid_candidate_ref,
        "extraction_run_id": run_id,
        "details": details[:20],
        "valid": orphan_lineage == 0 and invalid_chunk_ref == 0 and invalid_candidate_ref == 0,
    }


# ----------------------------------------------------------------------
# Audit 11: cross-stage identity audits
# ----------------------------------------------------------------------

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


def audit_cross_stage_identity(asset: dict[str, Any]) -> dict[str, Any]:
    merged = asset["merged"]["units"]
    enriched = asset["enriched"]["units"]
    final_units = asset["units"]["units"]

    # M4-03 -> M4-04: only entities/topics may change
    merged_by_id = {u["knowledge_unit_id"]: u for u in merged}
    enriched_by_id = {u["knowledge_unit_id"]: u for u in enriched}
    stage34_violations = []
    for kid, mu in merged_by_id.items():
        eu = enriched_by_id.get(kid)
        if eu is None:
            stage34_violations.append({"ku_id": kid, "rule": "unit_dropped"})
            continue
        for field in FROZEN_FIELDS:
            if field in ("knowledge_unit_id", "canonical_id", "unit_type", "statement",
                         "evidence_refs", "attribution", "extraction_confidence",
                         "verification_status", "extraction_lineage"):
                if mu.get(field) != eu.get(field):
                    stage34_violations.append({"ku_id": kid, "rule": f"field_changed:{field}"})

    # M4-04 -> M4-05: all canonical fields byte-identical (entities/topics included)
    stage45_violations = []
    for kid, eu in enriched_by_id.items():
        fu = next((u for u in final_units if u["knowledge_unit_id"] == kid), None)
        if fu is None:
            stage45_violations.append({"ku_id": kid, "rule": "unit_dropped"})
            continue
        if eu != fu:
            stage45_violations.append({"ku_id": kid, "rule": "content_changed"})

    return {
        "stage34_valid": len(stage34_violations) == 0,
        "stage34_violations": stage34_violations[:20],
        "stage45_valid": len(stage45_violations) == 0,
        "stage45_violations": stage45_violations[:20],
    }


# ----------------------------------------------------------------------
# Audit 12: Markdown / JSON parity
# ----------------------------------------------------------------------

def audit_markdown_parity(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    markdown = asset["markdown"]
    ku_heads = re.findall(r"^### KU (ku_[a-f0-9]{16})$", markdown, re.MULTILINE)
    json_ids = [u["knowledge_unit_id"] for u in units]
    head_counter = Counter(ku_heads)
    missing = [kid for kid in json_ids if head_counter.get(kid, 0) == 0]
    duplicated = [kid for kid, cnt in head_counter.items() if cnt > 1]
    extra = [kid for kid in head_counter if kid not in set(json_ids)]
    return {
        "json_unit_count": len(units),
        "markdown_rendered_unit_count": len(ku_heads),
        "missing": missing[:20],
        "duplicated": duplicated[:20],
        "extra": extra[:20],
        "valid": len(missing) == 0 and len(duplicated) == 0 and len(extra) == 0,
    }


# ----------------------------------------------------------------------
# Audit 13: qualitative sample selection (deterministic)
# ----------------------------------------------------------------------

def select_qualitative_samples(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    ids = [u["knowledge_unit_id"] for u in units]
    n = len(units)
    selected: dict[str, list[str]] = {
        "first": ids[:3],
        "middle": ids[n // 2 - 1 : n // 2 + 2],
        "last": ids[-3:],
    }
    lowest_conf = sorted(units, key=lambda u: u["extraction_confidence"])[:3]
    selected["lowest_confidence"] = [u["knowledge_unit_id"] for u in lowest_conf]
    most_evidence = sorted(units, key=lambda u: len(u["evidence_refs"]), reverse=True)[:3]
    selected["most_evidence"] = [u["knowledge_unit_id"] for u in most_evidence]

    ordered: list[str] = []
    for group in ("first", "middle", "last", "lowest_confidence", "most_evidence"):
        for kid in selected[group]:
            if kid not in ordered:
                ordered.append(kid)

    by_id = {u["knowledge_unit_id"]: u for u in units}
    samples = []
    for kid in ordered:
        u = by_id[kid]
        samples.append({
            "knowledge_unit_id": kid,
            "unit_type": u["unit_type"],
            "extraction_confidence": u["extraction_confidence"],
            "evidence_count": len(u["evidence_refs"]),
            "statement": u["statement"],
            "excerpts": [r["source_excerpt"] for r in u["evidence_refs"][:3]],
        })
    return {"sample_count": len(samples), "samples": samples}


# ----------------------------------------------------------------------
# Audit 14: classification quality markers (heuristic scan)
# ----------------------------------------------------------------------

ADVICE_MARKERS = [
    "建议", "最好", "应该", "先来", "第一步", "不要只看", "要优化", "记住",
    "please", "recommend", "should", "先问", "执行", "的命令", "原则",
]


def audit_classification_markers(asset: dict[str, Any]) -> dict[str, Any]:
    units = asset["units"]["units"]
    flagged: list[dict[str, Any]] = []
    for u in units:
        stmt = u["statement"]
        hits = [m for m in ADVICE_MARKERS if m in stmt]
        if hits and u["unit_type"] == "claim":
            flagged.append({
                "ku_id": u["knowledge_unit_id"],
                "markers": hits,
                "statement": stmt,
            })
    return {
        "flagged_claim_count": len(flagged),
        "flagged": flagged,
    }


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def audit_asset(asset_id: str) -> dict[str, Any]:
    knowledge_dir = PROJECT_ROOT / "data" / "processed" / asset_id / "knowledge"
    if not knowledge_dir.is_dir():
        raise FileNotFoundError(f"Missing knowledge dir: {knowledge_dir}")
    asset = load_asset(knowledge_dir, asset_id)
    return {
        "asset_id": asset_id,
        "chain_counts": audit_chain_counts(asset),
        "fingerprint_chain": audit_fingerprint_chain(asset),
        "ku_id_recomputation": audit_ku_id_recomputation(asset),
        "evidence_grounding": audit_evidence_grounding(asset),
        "attribution": audit_attribution(asset),
        "observations": audit_observations(asset),
        "verification": audit_verification(asset),
        "entity_grounding": audit_entity_grounding(asset),
        "topics": audit_topics(asset),
        "lineage": audit_lineage(asset),
        "cross_stage_identity": audit_cross_stage_identity(asset),
        "markdown_parity": audit_markdown_parity(asset),
        "qualitative_samples": select_qualitative_samples(asset),
        "classification_markers": audit_classification_markers(asset),
    }


def aggregate_pass(result: dict[str, Any]) -> bool:
    checks = [
        result["fingerprint_chain"]["valid"],
        result["ku_id_recomputation"]["valid"],
        result["evidence_grounding"]["valid"],
        result["attribution"]["valid"],
        result["observations"]["valid"],
        result["entity_grounding"]["valid"],
        result["topics"]["valid"],
        result["lineage"]["valid"],
        result["cross_stage_identity"]["stage34_valid"],
        result["cross_stage_identity"]["stage45_valid"],
        result["markdown_parity"]["valid"],
        result["chain_counts"]["stage_count_identity"],
    ]
    return all(checks)


def main() -> int:
    print("=" * 70)
    print("M4-06 End-to-End Acceptance Audit (read-only, offline)")
    print("=" * 70)

    summary: dict[str, Any] = {
        "artifact_kind": "m4-acceptance-audit",
        "knowledge_layer": False,
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "assets": {},
        "overall": {},
    }
    ok = True
    for asset_id in (VIDEO_ASSET, ALBUM_ASSET):
        result = audit_asset(asset_id)
        pass_ = aggregate_pass(result)
        ok = ok and pass_
        summary["assets"][asset_id] = {
            "pass": pass_,
            "chain_counts": result["chain_counts"],
            "fingerprint_chain_valid": result["fingerprint_chain"]["valid"],
            "ku_id_recomputation": {
                "checked": result["ku_id_recomputation"]["checked_count"],
                "mismatch": result["ku_id_recomputation"]["mismatch_count"],
            },
            "evidence_grounding": {
                "ku_count": result["evidence_grounding"]["ku_count"],
                "evidence_ref_count": result["evidence_grounding"]["evidence_ref_count"],
                "violations": result["evidence_grounding"]["violations_count"],
            },
            "attribution": result["attribution"],
            "observations": result["observations"],
            "verification": result["verification"],
            "entity_grounding": result["entity_grounding"],
            "topics": result["topics"],
            "lineage": result["lineage"],
            "cross_stage_identity": result["cross_stage_identity"],
            "markdown_parity": result["markdown_parity"],
            "classification_flagged_claims": result["classification_markers"]["flagged_claim_count"],
        }
        print(f"\n--- {asset_id} ---")
        cc = result["chain_counts"]
        print(f"  chain: manifest={cc['manifest_items']} chunks={cc['chunk_count']} "
              f"candidates={cc['candidate_count']} merged={cc['merged_count']} "
              f"enriched={cc['enriched_count']} final={cc['final_count']}")
        print(f"  fingerprint chain valid: {result['fingerprint_chain']['valid']}")
        print(f"  KU ID recomputation: {result['ku_id_recomputation']['checked_count']} checked, "
              f"{result['ku_id_recomputation']['mismatch_count']} mismatched")
        eg = result["evidence_grounding"]
        print(f"  evidence grounding: {eg['evidence_ref_count']} refs, {eg['violations_count']} violations")
        print(f"  attribution: {result['attribution']['violations_count']} violations")
        print(f"  observations: {result['observations']['observation_count']} total, "
              f"{result['observations']['invalid_count']} invalid")
        print(f"  verification: {result['verification']['status_counts']}")
        print(f"  entity grounding: {result['entity_grounding']['total_entity_mentions']} mentions, "
              f"{result['entity_grounding']['violations_count']} violations")
        print(f"  topics: {result['topics']['total_topics']} total, {result['topics']['violations_count']} violations")
        print(f"  lineage: orphan={result['lineage']['orphan_lineage_count']} "
              f"chunk={result['lineage']['invalid_chunk_ref_count']} "
              f"candidate={result['lineage']['invalid_candidate_ref_count']}")
        print(f"  cross-stage identity: 3->4={result['cross_stage_identity']['stage34_valid']} "
              f"4->5={result['cross_stage_identity']['stage45_valid']}")
        print(f"  md parity: json={result['markdown_parity']['json_unit_count']} "
              f"md={result['markdown_parity']['markdown_rendered_unit_count']} "
              f"valid={result['markdown_parity']['valid']}")
        print(f"  classification-flagged claims: {result['classification_markers']['flagged_claim_count']}")
        print(f"  OVERALL PASS: {pass_}")

    summary["overall"] = {"pass": ok, "decision": "ACCEPT" if ok else "HOLD"}

    out_dir = PROJECT_ROOT / "data" / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "m4_06_acceptance_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSummary written: {out_path}")
    print(f"OVERALL DECISION: {summary['overall']['decision']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())