"""Deterministic M4-05 finalization: canonical document + audit markdown render.

This module performs no inference, no knowledge creation, and no fact checking.
It only validates, serializes, and renders the M4-04 enriched candidates into:

  - knowledge_units.json   (schema knowledge-units-v1 via CanonicalKnowledgeUnitsDocument)
  - knowledge.md           (internal human-readable audit representation)

Every unit is carried through byte-identically; only document wrapper metadata,
final serialization, and the audit render may differ from the M4-04 input.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .models import (
    KNOWLEDGE_SCHEMA_VERSION,
    CanonicalKnowledgeUnit,
    CanonicalKnowledgeUnitsDocument,
    ExtractionProvenance,
    VerificationStatus,
)
from ..storage import atomic_write_json, atomic_write_text, load_json, utc_now


FINALIZATION_SCHEMA_VERSION = "m4-finalization-v1"
RENDER_POLICY_VERSION = "m4-audit-render-v1"
FINAL_DOCUMENT_FILENAME = "knowledge_units.json"
FINAL_RENDER_FILENAME = "knowledge.md"
FINALIZATION_FILENAME = "knowledge_finalization.json"

ALLOWED_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)

# Ordered render labels for the four epistemic states.
_VERIFICATION_LABELS = {
    VerificationStatus.NOT_CHECKED.value: "Not checked",
    VerificationStatus.VERIFIED.value: "Verified",
    VerificationStatus.CONTESTED.value: "Contested",
    VerificationStatus.UNSUPPORTED.value: "Unsupported",
}

_ATTRIBUTION_LABELS = {
    "source_actor_explicit_speaker": "Source actor is explicit speaker",
    "named_speaker": "Named speaker",
    "quoted_third_party": "Quoted third party",
    "unverified_speaker": "Unverified speaker",
    "visual_media": "Visual media",
    "system_derived": "System derived",
}


@dataclass(frozen=True)
class RenderConfig:
    """M4-05 render/finalization configuration; participates in cache identity."""

    policy_version: str = RENDER_POLICY_VERSION
    force: bool = False


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_enriched_artifact_fingerprint(enriched_artifact: dict[str, Any]) -> str:
    """Content fingerprint of the complete authoritative M4-04 input artifact."""
    return _sha256_json(enriched_artifact)


def compute_finalization_fingerprint(
    enriched_artifact_fingerprint: str,
    knowledge_schema_version: str,
    config: RenderConfig,
) -> str:
    """Identity of a deterministic M4-05 finalization result."""
    return _sha256_json(
        {
            "enriched_artifact_fingerprint": enriched_artifact_fingerprint,
            "knowledge_schema_version": knowledge_schema_version,
            "render_policy_version": config.policy_version,
        }
    )


# ----------------------------------------------------------------------
# Verification Contract Validation
# ----------------------------------------------------------------------

def validate_verification_status(status: Any) -> None:
    """Reject verification_status values outside the sealed contract.

    M4-05 never produces verified/contested/unsupported itself; it only
    preserves statuses already present in the input artifact.
    """
    if not isinstance(status, str) or status not in ALLOWED_VERIFICATION_STATUSES:
        raise ValueError(
            f"invalid verification_status: {status!r}; "
            f"allowed values: {sorted(ALLOWED_VERIFICATION_STATUSES)}"
        )


# ----------------------------------------------------------------------
# Identity Audit (enriched vs final)
# ----------------------------------------------------------------------

def audit_finalization_identity(
    enriched_artifact: dict[str, Any],
    final_document: dict[str, Any],
) -> dict[str, Any]:
    """Byte-compare every unit between the M4-04 input and the M4-05 output.

    M4-05 is not permitted to change ANY unit field, including entities and
    topics. identity_violation_count must be 0 or M4-05 FAILs.
    """
    violations: list[str] = []
    before_units = enriched_artifact.get("units", [])
    after_units = final_document.get("units", [])
    if len(before_units) != len(after_units):
        violations.append(
            f"unit count mismatch: {len(before_units)} != {len(after_units)}"
        )
    before_by_id = {unit["knowledge_unit_id"]: unit for unit in before_units}
    after_by_id = {unit["knowledge_unit_id"]: unit for unit in after_units}
    if set(before_by_id) != set(after_by_id):
        only_before = set(before_by_id) - set(after_by_id)
        only_after = set(after_by_id) - set(before_by_id)
        violations.append(
            f"unit set mismatch: only_before={sorted(only_before)} "
            f"only_after={sorted(only_after)}"
        )
    for ku_id in sorted(set(before_by_id) & set(after_by_id)):
        if before_by_id[ku_id] != after_by_id[ku_id]:
            violations.append(f"{ku_id} canonical content changed")
    return {
        "input_unit_count": len(before_by_id),
        "output_unit_count": len(after_by_id),
        "identity_violation_count": len(violations),
        "violations": violations,
        "valid": len(violations) == 0,
    }


# ----------------------------------------------------------------------
# Final Document Construction (no filesystem effects)
# ----------------------------------------------------------------------

def build_final_document(
    enriched_artifact: dict[str, Any],
    extraction_provenance: dict[str, Any],
    config: Optional[RenderConfig] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Construct the knowledge-units-v1 final document from M4-04 input.

    ``extraction_provenance`` must be the real M4 extraction provenance from the
    M4-02 knowledge_candidates.json ``provenance`` block; it is copied verbatim.
    """
    if not isinstance(enriched_artifact, dict):
        raise TypeError("enriched candidates artifact must be a dict")
    if not isinstance(extraction_provenance, dict):
        raise TypeError("extraction provenance must be a dict")
    if enriched_artifact.get("knowledge_schema_version") != KNOWLEDGE_SCHEMA_VERSION:
        raise ValueError(
            "knowledge schema version mismatch: "
            f"expected {KNOWLEDGE_SCHEMA_VERSION!r}, got "
            f"{enriched_artifact.get('knowledge_schema_version')!r}"
        )
    canonical_id = enriched_artifact.get("canonical_id")
    if not isinstance(canonical_id, str) or not canonical_id:
        raise ValueError("enriched candidates artifact has no canonical_id")
    raw_units = enriched_artifact.get("units")
    if not isinstance(raw_units, list):
        raise ValueError("enriched candidates artifact units must be a list")
    for unit in raw_units:
        validate_verification_status(unit.get("verification_status"))
    units = [CanonicalKnowledgeUnit.from_dict(item) for item in raw_units]
    provenance = ExtractionProvenance.from_dict(extraction_provenance)
    document = CanonicalKnowledgeUnitsDocument(
        canonical_id=canonical_id,
        generated_at=generated_at or utc_now(),
        unit_count=len(units),
        units=units,
        extraction_provenance=provenance,
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
    )
    return document.to_dict()


# ----------------------------------------------------------------------
# Markdown Safety
# ----------------------------------------------------------------------

def escape_source_excerpt(text: str) -> str:
    """Render untrusted excerpt text safely without altering its meaning.

    Escapes backslashes/backticks and HTML angle brackets, then emits every
    line as a blockquote so untrusted content can never break the audit
    document's heading/fence structure.
    """
    if not isinstance(text, str):
        raise TypeError("source_excerpt must be a string")
    escaped = text.replace("\\", "\\\\").replace("`", "\\`")
    escaped = escaped.replace("<", "&lt;").replace(">", "&gt;")
    return "\n".join(f"> {line}" for line in escaped.split("\n")) if escaped else ">"


# ----------------------------------------------------------------------
# Audit Markdown Render
# ----------------------------------------------------------------------

def render_audit_markdown(
    final_document: dict[str, Any],
    finalization_metadata: Optional[dict[str, Any]] = None,
) -> str:
    """Render the canonical document into the internal audit markdown."""
    lines: list[str] = []
    lines.append("# Knowledge Audit")
    lines.append("")

    # --- Asset ---
    lines.append("## Asset")
    lines.append("")
    lines.append(f"- canonical_id: {final_document['canonical_id']}")
    lines.append(f"- schema version: {final_document['schema_version']}")
    lines.append(f"- generated_at: {final_document['generated_at']}")
    lines.append(f"- unit count: {final_document['unit_count']}")
    lines.append("")

    # --- Finalization metadata (wrapper audit, never part of canonical schema) ---
    if finalization_metadata:
        lines.append("## Finalization")
        lines.append("")
        lines.append(f"- finalization policy: {finalization_metadata.get('finalization_policy_version')}")
        lines.append(f"- source enriched fingerprint: {finalization_metadata.get('source_enriched_artifact_fingerprint')}")
        lines.append(f"- finalization fingerprint: {finalization_metadata.get('finalization_fingerprint')}")
        lines.append(f"- identity violations: {finalization_metadata.get('identity_violation_count')}")
        lines.append("")

    # --- Summary ---
    lines.append("## Summary")
    lines.append("")
    type_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for unit in final_document["units"]:
        type_counts[unit["unit_type"]] = type_counts.get(unit["unit_type"], 0) + 1
        status = unit.get("verification_status", VerificationStatus.NOT_CHECKED.value)
        status_counts[status] = status_counts.get(status, 0) + 1
    for unit_type in (
        "claim",
        "opinion",
        "observation",
        "procedure_step",
        "verification_question",
    ):
        lines.append(f"- {unit_type} count: {type_counts.get(unit_type, 0)}")
    lines.append("")
    lines.append("Verification status:")
    for status in (
        VerificationStatus.NOT_CHECKED.value,
        VerificationStatus.VERIFIED.value,
        VerificationStatus.CONTESTED.value,
        VerificationStatus.UNSUPPORTED.value,
    ):
        lines.append(f"- {status}: {status_counts.get(status, 0)}")
    lines.append("")

    # --- Knowledge Units ---
    lines.append("## Knowledge Units")
    lines.append("")
    for unit in final_document["units"]:
        _render_unit(lines, unit)
    return "\n".join(lines) + "\n"


def _render_unit(lines: list[str], unit: dict[str, Any]) -> None:
    lines.append(f"### KU {unit['knowledge_unit_id']}")
    lines.append("")
    lines.append(f"Type: {unit['unit_type']}")
    lines.append(f"Statement: {unit['statement']}")
    status = unit.get("verification_status", VerificationStatus.NOT_CHECKED.value)
    lines.append(f"Verification: {_VERIFICATION_LABELS.get(status, status)}")
    lines.append(f"Extraction confidence: {unit['extraction_confidence']}")
    _render_attribution(lines, unit.get("attribution", {}))
    lines.append("")

    entities = unit.get("entities", [])
    if entities:
        lines.append("Entities:")
        for entity in entities:
            lines.append(
                f"- {entity['entity_name']} ({entity['category']})"
            )
        lines.append("")
    else:
        lines.append("Entities: (none)")
        lines.append("")

    topics = unit.get("topics", [])
    if topics:
        lines.append("Topics:")
        for topic in topics:
            lines.append(f"- {topic}")
        lines.append("")
    else:
        lines.append("Topics: (none)")
        lines.append("")

    lines.append("Evidence:")
    for ref in unit.get("evidence_refs", []):
        lines.append("")
        lines.append(f"- evidence_id: {ref['evidence_id']}")
        lines.append("  - source_excerpt:")
        for excerpt_line in escape_source_excerpt(ref.get("source_excerpt", "")).split("\n"):
            lines.append(f"    {excerpt_line}")
        temporal = ref.get("temporal_range")
        sequence = ref.get("sequence_range")
        if temporal:
            lines.append(
                f"  - temporal range: start={temporal['start']}s, "
                f"end={temporal['end']}s, duration={temporal['duration']}s"
            )
        if sequence:
            lines.append(f"  - sequence range: sequence={sequence['sequence_index']}")
    lines.append("")

    lineage = unit.get("extraction_lineage", {})
    lines.append("Extraction lineage:")
    lines.append("")
    lines.append(f"- run id: {lineage.get('extraction_run_id')}")
    lines.append(f"- input chunks: {', '.join(lineage.get('input_chunk_ids', []))}")
    lines.append(f"- candidate id: {lineage.get('candidate_id') or 'None'}")
    lines.append(
        f"- source candidate ids: {', '.join(lineage.get('source_candidate_ids', []))}"
    )
    lines.append(f"- merge strategy: {lineage.get('merge_strategy') or 'None'}")
    lines.append("")


def _render_attribution(lines: list[str], attribution: dict[str, Any]) -> None:
    status_label = _ATTRIBUTION_LABELS.get(
        attribution.get("attribution_status"),
        attribution.get("attribution_status") or "unknown",
    )
    lines.append(f"Attribution status: {status_label}")
    source_actor = attribution.get("source_actor_name") or "Unknown"
    source_actor_id = attribution.get("source_actor_id")
    if source_actor_id:
        source_actor = f"{source_actor} (id: {source_actor_id})"
    lines.append(f"Source actor: {source_actor}")
    speaker = attribution.get("speaker_name") or "Unknown"
    speaker_id = attribution.get("speaker_id")
    if speaker_id:
        speaker = f"{speaker} (id: {speaker_id})"
    lines.append(f"Speaker: {speaker}")


# ----------------------------------------------------------------------
# Filesystem Pipeline Entry
# ----------------------------------------------------------------------

def finalize_knowledge_document(
    processed_dir: Path,
    config: Optional[RenderConfig] = None,
) -> dict[str, Any]:
    """Read M4-04 enriched candidates and persist final M4-05 artifacts.

    Reads:
      data/processed/<canonical_id>/knowledge/enriched_knowledge_candidates.json
      data/processed/<canonical_id>/knowledge/knowledge_candidates.json (provenance)

    Writes:
      knowledge_units.json
      knowledge.md
      knowledge_finalization.json (wrapper/cache metadata)

    Idempotent: identical input + policy re-runs return a cache hit and never
    rewrite generated_at or the artifacts.
    """
    config = config or RenderConfig()
    processed_dir = Path(processed_dir)
    knowledge_dir = processed_dir / "knowledge"
    enriched_path = knowledge_dir / "enriched_knowledge_candidates.json"
    candidates_path = knowledge_dir / "knowledge_candidates.json"
    units_path = knowledge_dir / FINAL_DOCUMENT_FILENAME
    render_path = knowledge_dir / FINAL_RENDER_FILENAME
    wrapper_path = knowledge_dir / FINALIZATION_FILENAME

    enriched_artifact = load_json(enriched_path)
    if enriched_artifact is None:
        raise FileNotFoundError(
            f"Missing M4-04 enriched candidates artifact: {enriched_path}"
        )
    candidates_artifact = load_json(candidates_path)
    if candidates_artifact is None:
        raise FileNotFoundError(
            f"Missing M4-02 candidates artifact (for provenance): {candidates_path}"
        )
    provenance = candidates_artifact.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("M4-02 candidates artifact has no provenance block")

    enriched_fingerprint = compute_enriched_artifact_fingerprint(enriched_artifact)
    expected_fingerprint = compute_finalization_fingerprint(
        enriched_fingerprint,
        KNOWLEDGE_SCHEMA_VERSION,
        config,
    )

    if not config.force and wrapper_path.is_file() and units_path.is_file():
        cached_wrapper = load_json(wrapper_path)
        if (
            cached_wrapper
            and cached_wrapper.get("finalization_fingerprint") == expected_fingerprint
            and render_path.is_file()
        ):
            return {**cached_wrapper, "cache_hit": True}

    document = build_final_document(enriched_artifact, provenance, config)
    audit = audit_finalization_identity(enriched_artifact, document)
    if not audit["valid"]:
        raise RuntimeError(
            "M4-05 finalization identity audit failed: "
            f"{audit['identity_violation_count']} violation(s)"
        )

    finalization_metadata = {
        "schema_version": FINALIZATION_SCHEMA_VERSION,
        "canonical_id": document["canonical_id"],
        "knowledge_schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source_enriched_artifact_fingerprint": enriched_fingerprint,
        "finalization_policy_version": config.policy_version,
        "finalization_fingerprint": expected_fingerprint,
        "generated_at": document["generated_at"],
        "input_unit_count": audit["input_unit_count"],
        "output_unit_count": audit["output_unit_count"],
        "identity_violation_count": audit["identity_violation_count"],
        "render": {
            "policy_version": config.policy_version,
            "filename": FINAL_RENDER_FILENAME,
        },
        "audit": audit,
        "fingerprint": expected_fingerprint,
    }

    markdown = render_audit_markdown(document, finalization_metadata)
    atomic_write_json(wrapper_path, finalization_metadata)
    atomic_write_json(units_path, document)
    atomic_write_text(render_path, markdown)
    return {**finalization_metadata, "cache_hit": False}