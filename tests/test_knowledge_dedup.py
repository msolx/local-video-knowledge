from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.knowledge.merger import (
    MERGE_POLICY_VERSION,
    MERGED_CANDIDATES_FILENAME,
    MergeConfig,
    compute_candidates_artifact_fingerprint,
    merge_candidates_artifact,
    merge_knowledge_candidates,
)
from src.knowledge.models import (
    AttributionInfo,
    AttributionStatus,
    EvidenceRef,
    ExtractionLineage,
    SequenceRange,
    TemporalRange,
    UnitType,
    create_knowledge_unit,
)


def _candidate(
    *,
    chunk_id: str = "chk_000001",
    candidate_id: str = "cand_001",
    statement: str = "The device has a USB-C port.",
    unit_type: UnitType = UnitType.CLAIM,
    evidence_id: str = "ev_001",
    excerpt: str = "The device has a USB-C port.",
    confidence: float = 0.4,
) -> dict:
    unit = create_knowledge_unit(
        canonical_id="douyin_test",
        unit_type=unit_type,
        statement=statement,
        evidence_refs=[
            EvidenceRef(
                evidence_id=evidence_id,
                source_excerpt=excerpt,
                temporal_range=TemporalRange(start=1.0, end=2.0, duration=9.0),
                sequence_range=SequenceRange(sequence_index=4),
            )
        ],
        attribution=AttributionInfo(
            source_actor_name="Author",
            attribution_status=AttributionStatus.UNVERIFIED_SPEAKER,
        ),
        extraction_confidence=confidence,
        extraction_lineage=ExtractionLineage(
            extraction_run_id="run_test",
            input_chunk_ids=[chunk_id],
            candidate_id=candidate_id,
            source_candidate_ids=[candidate_id],
        ),
    )
    return unit.to_dict()


def _artifact(candidates: list[dict]) -> dict:
    return {
        "canonical_id": "douyin_test",
        "knowledge_schema_version": "knowledge-units-v1",
        "extraction_schema_version": "m4-candidates-v1",
        "extraction_run_id": "run_test",
        "candidates": candidates,
    }


def test_exact_duplicate_merges_lineage_and_preserves_canonical_fields():
    first = _candidate(chunk_id="chk_000001", candidate_id="cand_a", confidence=0.3)
    second = _candidate(chunk_id="chk_000002", candidate_id="cand_b", confidence=0.9)
    result = merge_candidates_artifact(_artifact([first, second]))

    assert result["input_candidate_count"] == 2
    assert result["output_unit_count"] == 1
    assert result["deduplicated_count"] == 1
    unit = result["units"][0]
    assert unit["knowledge_unit_id"] == first["knowledge_unit_id"]
    assert unit["extraction_confidence"] == 0.9
    assert unit["verification_status"] == "not_checked"
    assert unit["entities"] == []
    assert unit["topics"] == []
    assert unit["evidence_refs"] == first["evidence_refs"]
    assert unit["extraction_lineage"] == {
        "extraction_run_id": "run_test",
        "input_chunk_ids": ["chk_000001", "chk_000002"],
        "candidate_id": first["knowledge_unit_id"],
        "source_candidate_ids": ["cand_a", "cand_b"],
        "merge_strategy": "dedup_exact",
    }


def test_different_ku_ids_stay_separate_and_first_appearance_order_is_stable():
    first = _candidate(statement="First statement.", candidate_id="cand_a")
    second = _candidate(statement="Second statement.", candidate_id="cand_b")
    result = merge_candidates_artifact(_artifact([second, first]))
    assert result["output_unit_count"] == 2
    assert [unit["knowledge_unit_id"] for unit in result["units"]] == [
        second["knowledge_unit_id"],
        first["knowledge_unit_id"],
    ]
    assert result["deduplicated_count"] == 0


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda value: value.__setitem__("statement", "Conflicting statement."), "same_id_statement_mismatch"),
        (lambda value: value.__setitem__("unit_type", "opinion"), "same_id_unit_type_mismatch"),
        (lambda value: value["evidence_refs"][0].__setitem__("source_excerpt", "different"), "same_id_evidence_mismatch"),
        (lambda value: value["attribution"].__setitem__("source_actor_name", "Other"), "same_id_attribution_mismatch"),
    ],
)
def test_same_id_canonical_conflicts_are_audited_not_silently_merged(mutator, reason):
    first = _candidate(candidate_id="cand_a")
    second = copy.deepcopy(first)
    second["extraction_lineage"]["candidate_id"] = "cand_b"
    second["extraction_lineage"]["source_candidate_ids"] = ["cand_b"]
    second["extraction_lineage"]["input_chunk_ids"] = ["chk_000002"]
    mutator(second)
    result = merge_candidates_artifact(_artifact([first, second]))
    assert result["output_unit_count"] == 0
    assert result["conflict_count"] == 1
    assert reason in result["merge_audit"]["conflicts"][0]["reason_codes"]


def test_evidence_ordering_is_preserved():
    first = _candidate(candidate_id="cand_a")
    first["evidence_refs"].append(copy.deepcopy(first["evidence_refs"][0]))
    first["evidence_refs"][1]["evidence_id"] = "ev_002"
    # Recompute the frozen ID through a valid factory-created equivalent unit.
    from src.knowledge.models import CanonicalKnowledgeUnit
    rebuilt = CanonicalKnowledgeUnit.from_dict(first)
    second = rebuilt.to_dict()
    second["extraction_lineage"]["candidate_id"] = "cand_b"
    second["extraction_lineage"]["source_candidate_ids"] = ["cand_b"]
    second["extraction_lineage"]["input_chunk_ids"] = ["chk_000002"]
    result = merge_candidates_artifact(_artifact([first, second]))
    assert [ref["evidence_id"] for ref in result["units"][0]["evidence_refs"]] == ["ev_001", "ev_002"]


def test_same_id_coordinate_or_run_conflicts_are_audited():
    first = _candidate(candidate_id="cand_a")
    coordinate_conflict = copy.deepcopy(first)
    coordinate_conflict["extraction_lineage"].update(
        candidate_id="cand_b",
        source_candidate_ids=["cand_b"],
        input_chunk_ids=["chk_000002"],
    )
    coordinate_conflict["evidence_refs"][0]["sequence_range"]["sequence_index"] = 99
    coordinate_result = merge_candidates_artifact(_artifact([first, coordinate_conflict]))
    assert "same_id_evidence_mismatch" in coordinate_result["merge_audit"]["conflicts"][0]["reason_codes"]

    run_conflict = copy.deepcopy(first)
    run_conflict["extraction_lineage"].update(
        extraction_run_id="run_other",
        candidate_id="cand_c",
        source_candidate_ids=["cand_c"],
        input_chunk_ids=["chk_000003"],
    )
    run_result = merge_candidates_artifact(_artifact([first, run_conflict]))
    assert "same_id_extraction_run_id_mismatch" in run_result["merge_audit"]["conflicts"][0]["reason_codes"]


def test_identical_canonical_identity_with_different_id_fails_invariant():
    first = _candidate(candidate_id="cand_a")
    second = copy.deepcopy(first)
    second["knowledge_unit_id"] = "ku_0123456789abcdef"
    second["extraction_lineage"].update(
        candidate_id="cand_b",
        source_candidate_ids=["cand_b"],
        input_chunk_ids=["chk_000002"],
    )
    with pytest.raises(ValueError, match="identical canonical identity"):
        merge_candidates_artifact(_artifact([first, second]))


def test_repeat_is_idempotent_and_no_backend_is_involved():
    first = _candidate(candidate_id="cand_a")
    second = _candidate(chunk_id="chk_000002", candidate_id="cand_b")
    artifact = _artifact([first, second])
    assert merge_candidates_artifact(artifact) == merge_candidates_artifact(artifact)


def test_disk_cache_hit_and_input_or_policy_change_invalidates(tmp_path: Path):
    processed = tmp_path / "douyin_test"
    knowledge = processed / "knowledge"
    knowledge.mkdir(parents=True)
    source = _artifact([_candidate()])
    (knowledge / "knowledge_candidates.json").write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8"
    )
    first = merge_knowledge_candidates(processed)
    second = merge_knowledge_candidates(processed)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    source["candidates"].append(_candidate(candidate_id="cand_second", statement="Other."))
    (knowledge / "knowledge_candidates.json").write_text(json.dumps(source), encoding="utf-8")
    changed_input = merge_knowledge_candidates(processed)
    assert changed_input["cache_hit"] is False
    changed_policy = merge_knowledge_candidates(
        processed, MergeConfig(policy_version="m4-exact-ku-id-merge-test-v2")
    )
    assert changed_policy["cache_hit"] is False
    assert (knowledge / MERGED_CANDIDATES_FILENAME).is_file()


def test_zero_and_one_candidate_artifacts():
    assert merge_candidates_artifact(_artifact([]))["output_unit_count"] == 0
    one = merge_candidates_artifact(_artifact([_candidate()]))
    assert one["output_unit_count"] == 1
    assert one["deduplicated_count"] == 0


@pytest.mark.parametrize(
    "asset_id",
    ["douyin_7681603850364521734", "douyin_7682038498466993905"],
)
def test_real_c10_artifacts_merge_offline(asset_id: str):
    root = Path(__file__).resolve().parents[1]
    source = json.loads(
        (root / "data" / "processed" / asset_id / "knowledge" / "knowledge_candidates.json").read_text(encoding="utf-8")
    )
    result = merge_candidates_artifact(source)
    assert result["input_candidate_count"] == len(source["candidates"])
    assert result["conflict_count"] == 0
    assert all(unit["knowledge_unit_id"].startswith("ku_") for unit in result["units"])


def test_candidates_artifact_fingerprint_is_content_addressed():
    artifact = _artifact([_candidate()])
    assert compute_candidates_artifact_fingerprint(artifact) == compute_candidates_artifact_fingerprint(copy.deepcopy(artifact))
    changed = copy.deepcopy(artifact)
    changed["candidates"][0]["statement"] = "Changed."
    assert compute_candidates_artifact_fingerprint(artifact) != compute_candidates_artifact_fingerprint(changed)
