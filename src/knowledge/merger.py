"""Deterministic M4-03 merge of M4-02 knowledge candidate artifacts.

This module intentionally performs no inference and no semantic/fuzzy matching.
It only coalesces candidates that have the same frozen knowledge-unit identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import (
    KNOWLEDGE_SCHEMA_VERSION,
    CanonicalKnowledgeUnit,
    ExtractionLineage,
    normalize_statement,
)
from ..storage import atomic_write_json, load_json


MERGED_CANDIDATES_SCHEMA_VERSION = "m4-merged-candidates-v1"
MERGE_POLICY_VERSION = "m4-exact-ku-id-merge-v1"
MERGED_CANDIDATES_FILENAME = "merged_knowledge_candidates.json"


@dataclass(frozen=True)
class MergeConfig:
    """M4-03 merge configuration; all fields participate in cache identity."""

    policy_version: str = MERGE_POLICY_VERSION
    force: bool = False


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_candidates_artifact_fingerprint(candidates_artifact: dict[str, Any]) -> str:
    """Content fingerprint of the complete authoritative M4-02 input artifact."""
    return _sha256_json(candidates_artifact)


def compute_merge_fingerprint(
    candidates_artifact_fingerprint: str,
    knowledge_schema_version: str,
    config: MergeConfig,
) -> str:
    """Identity of a deterministic M4-03 merge result, separate from extraction runs."""
    return _sha256_json(
        {
            "candidates_artifact_fingerprint": candidates_artifact_fingerprint,
            "knowledge_schema_version": knowledge_schema_version,
            "merge_policy_version": config.policy_version,
        }
    )


def _ordered_union(values: Iterable[Iterable[str]]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in values:
        for value in group:
            if value not in seen:
                seen.add(value)
                merged.append(value)
    return merged


def _canonical_conflict_reasons(
    baseline: CanonicalKnowledgeUnit,
    other: CanonicalKnowledgeUnit,
) -> list[str]:
    """Return all frozen-canonical-field mismatches for a same-ID group."""
    reasons: list[str] = []
    if baseline.canonical_id != other.canonical_id:
        reasons.append("same_id_canonical_id_mismatch")
    if baseline.unit_type != other.unit_type:
        reasons.append("same_id_unit_type_mismatch")
    if normalize_statement(baseline.statement) != normalize_statement(other.statement):
        reasons.append("same_id_statement_mismatch")
    if baseline.evidence_refs != other.evidence_refs:
        reasons.append("same_id_evidence_mismatch")
    if baseline.attribution != other.attribution:
        reasons.append("same_id_attribution_mismatch")
    if baseline.verification_status != other.verification_status:
        reasons.append("same_id_verification_status_mismatch")
    if baseline.entities != other.entities:
        reasons.append("same_id_entities_mismatch")
    if baseline.topics != other.topics:
        reasons.append("same_id_topics_mismatch")
    if (
        baseline.extraction_lineage.extraction_run_id
        != other.extraction_lineage.extraction_run_id
    ):
        reasons.append("same_id_extraction_run_id_mismatch")
    return reasons


def _load_and_validate_candidates(
    candidates_artifact: dict[str, Any],
) -> tuple[str, str, list[CanonicalKnowledgeUnit]]:
    if not isinstance(candidates_artifact, dict):
        raise TypeError("knowledge_candidates artifact must be a dict")
    canonical_id = candidates_artifact.get("canonical_id")
    if not isinstance(canonical_id, str) or not canonical_id:
        raise ValueError("knowledge_candidates artifact has no canonical_id")
    schema_version = candidates_artifact.get("knowledge_schema_version")
    if schema_version != KNOWLEDGE_SCHEMA_VERSION:
        raise ValueError(
            "knowledge schema version mismatch: "
            f"expected {KNOWLEDGE_SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    raw_candidates = candidates_artifact.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("knowledge_candidates artifact candidates must be a list")
    candidates = [CanonicalKnowledgeUnit.from_dict(item) for item in raw_candidates]
    identity_to_ids: dict[tuple[Any, ...], set[str]] = {}
    for candidate in candidates:
        if candidate.canonical_id != canonical_id:
            raise ValueError(
                f"candidate {candidate.knowledge_unit_id} canonical_id does not match artifact"
            )
        identity = (
            candidate.canonical_id,
            candidate.unit_type,
            tuple(candidate.evidence_refs),
            normalize_statement(candidate.statement),
        )
        identity_to_ids.setdefault(identity, set()).add(candidate.knowledge_unit_id)
    for knowledge_unit_ids in identity_to_ids.values():
        if len(knowledge_unit_ids) != 1:
            raise ValueError(
                "invariant violation: identical canonical identity has multiple "
                f"knowledge_unit_ids: {sorted(knowledge_unit_ids)}"
            )
    return canonical_id, schema_version, candidates


def merge_candidates_artifact(
    candidates_artifact: dict[str, Any],
    config: Optional[MergeConfig] = None,
) -> dict[str, Any]:
    """Merge a loaded M4-02 artifact without filesystem effects or model calls.

    Same knowledge_unit_id is the sole merge key.  Conflicted same-ID groups are
    deliberately excluded from ``units`` and retained in ``merge_audit`` rather
    than selecting one incompatible canonical representation.
    """
    config = config or MergeConfig()
    canonical_id, schema_version, candidates = _load_and_validate_candidates(
        candidates_artifact
    )
    source_fingerprint = compute_candidates_artifact_fingerprint(candidates_artifact)
    merge_fingerprint = compute_merge_fingerprint(
        source_fingerprint, schema_version, config
    )

    groups: dict[str, list[CanonicalKnowledgeUnit]] = {}
    group_order: list[str] = []
    for candidate in candidates:
        key = candidate.knowledge_unit_id
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(candidate)

    units: list[CanonicalKnowledgeUnit] = []
    exact_deduplications: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    deduplicated_count = 0
    conflicted_candidate_count = 0

    for knowledge_unit_id in group_order:
        group = groups[knowledge_unit_id]
        baseline = group[0]
        reasons = sorted(
            {
                reason
                for candidate in group[1:]
                for reason in _canonical_conflict_reasons(baseline, candidate)
            }
        )
        if reasons:
            conflicted_candidate_count += len(group)
            conflicts.append(
                {
                    "knowledge_unit_id": knowledge_unit_id,
                    "reason_codes": reasons,
                    "candidate_ids": _ordered_union(
                        candidate.extraction_lineage.source_candidate_ids
                        for candidate in group
                    ),
                    "input_chunk_ids": _ordered_union(
                        candidate.extraction_lineage.input_chunk_ids
                        for candidate in group
                    ),
                }
            )
            continue

        if len(group) == 1:
            units.append(baseline)
            continue

        lineage = ExtractionLineage(
            extraction_run_id=baseline.extraction_lineage.extraction_run_id,
            input_chunk_ids=_ordered_union(
                candidate.extraction_lineage.input_chunk_ids for candidate in group
            ),
            candidate_id=knowledge_unit_id,
            source_candidate_ids=_ordered_union(
                candidate.extraction_lineage.source_candidate_ids
                for candidate in group
            ),
            merge_strategy="dedup_exact",
        )
        merged = replace(
            baseline,
            extraction_confidence=max(
                candidate.extraction_confidence for candidate in group
            ),
            extraction_lineage=lineage,
        )
        units.append(merged)
        deduplicated_count += len(group) - 1
        exact_deduplications.append(
            {
                "knowledge_unit_id": knowledge_unit_id,
                "input_chunk_ids": lineage.input_chunk_ids,
                "source_candidate_ids": lineage.source_candidate_ids,
                "merged_candidate_count": len(group),
            }
        )

    return {
        "schema_version": MERGED_CANDIDATES_SCHEMA_VERSION,
        "canonical_id": canonical_id,
        "knowledge_schema_version": schema_version,
        "source_candidates_artifact_fingerprint": source_fingerprint,
        "merge_policy_version": config.policy_version,
        "merge_fingerprint": merge_fingerprint,
        "input_candidate_count": len(candidates),
        "output_unit_count": len(units),
        "deduplicated_count": deduplicated_count,
        "conflict_count": len(conflicts),
        "conflicted_candidate_count": conflicted_candidate_count,
        "units": [unit.to_dict() for unit in units],
        "merge_audit": {
            "exact_deduplications": exact_deduplications,
            "conflicts": conflicts,
        },
    }


def merge_knowledge_candidates(
    processed_dir: Path,
    config: Optional[MergeConfig] = None,
) -> dict[str, Any]:
    """Read M4-02 candidates and persist deterministic M4-03 merged candidates."""
    config = config or MergeConfig()
    processed_dir = Path(processed_dir)
    knowledge_dir = processed_dir / "knowledge"
    input_path = knowledge_dir / "knowledge_candidates.json"
    output_path = knowledge_dir / MERGED_CANDIDATES_FILENAME
    candidates_artifact = load_json(input_path)
    if candidates_artifact is None:
        raise FileNotFoundError(f"Missing M4-02 candidates artifact: {input_path}")

    source_fingerprint = compute_candidates_artifact_fingerprint(candidates_artifact)
    schema_version = candidates_artifact.get("knowledge_schema_version")
    expected_fingerprint = compute_merge_fingerprint(
        source_fingerprint, schema_version, config
    )
    if not config.force and output_path.is_file():
        cached = load_json(output_path)
        if cached and cached.get("merge_fingerprint") == expected_fingerprint:
            return {**cached, "cache_hit": True}

    merged = merge_candidates_artifact(candidates_artifact, config)
    atomic_write_json(output_path, merged)
    return {**merged, "cache_hit": False}
