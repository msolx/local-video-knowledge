from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Any, Optional

KNOWLEDGE_SCHEMA_VERSION = "knowledge-units-v1"


# ----------------------------------------------------------------------
# Canonical Enums (M4-00 Sealed)
# ----------------------------------------------------------------------

class UnitType(str, Enum):
    """Epistemic classification of knowledge units.
    
    Orthogonal to speaker identity or publication channel.
    """
    CLAIM = "claim"
    OPINION = "opinion"
    OBSERVATION = "observation"
    PROCEDURE_STEP = "procedure_step"
    VERIFICATION_QUESTION = "verification_question"


class VerificationStatus(str, Enum):
    """Factual truth/verification status against external reality.
    
    Defaults to 'not_checked'.
    """
    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    CONTESTED = "contested"
    UNSUPPORTED = "unsupported"


class AttributionStatus(str, Enum):
    """Source-neutral attribution certainty state.
    
    Standard speech ASR without diarization defaults to 'unverified_speaker'.
    """
    SOURCE_ACTOR_EXPLICIT_SPEAKER = "source_actor_explicit_speaker"
    NAMED_SPEAKER = "named_speaker"
    QUOTED_THIRD_PARTY = "quoted_third_party"
    UNVERIFIED_SPEAKER = "unverified_speaker"
    VISUAL_MEDIA = "visual_media"
    SYSTEM_DERIVED = "system_derived"


# ----------------------------------------------------------------------
# Value Objects: Temporal & Sequence Bounds
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class TemporalRange:
    """Temporal bounding envelope for time-based media (speech/video)."""
    start: float
    end: float
    duration: float

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"Temporal start cannot be negative: {self.start}")
        if self.end < self.start:
            raise ValueError(f"Temporal end ({self.end}) cannot be less than start ({self.start})")
        if self.duration < 0:
            raise ValueError(f"Temporal duration cannot be negative: {self.duration}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TemporalRange:
        if not isinstance(d, dict):
            raise TypeError("TemporalRange payload must be a dict")
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            duration=float(d["duration"]),
        )


@dataclass(frozen=True)
class SequenceRange:
    """Sequential index for page or image-based media."""
    sequence_index: int

    def __post_init__(self) -> None:
        if self.sequence_index < 0:
            raise ValueError(f"sequence_index cannot be negative: {self.sequence_index}")

    def to_dict(self) -> dict[str, Any]:
        return {"sequence_index": int(self.sequence_index)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SequenceRange:
        if not isinstance(d, dict):
            raise TypeError("SequenceRange payload must be a dict")
        return cls(sequence_index=int(d["sequence_index"]))


# ----------------------------------------------------------------------
# Evidence Reference: Grounded citation to atomic EvidenceItem
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceRef:
    """Immutable citation reference to an atomic EvidenceItem in evidence_manifest.json.

    INVARIANT: Does NOT include chunk_id. Evidence Chunk is a processing window,
    whereas EvidenceItem is the durable provenance identity.
    """
    evidence_id: str
    source_excerpt: str
    temporal_range: Optional[TemporalRange] = None
    sequence_range: Optional[SequenceRange] = None

    def __post_init__(self) -> None:
        if not self.evidence_id or not isinstance(self.evidence_id, str):
            raise ValueError("evidence_id must be a non-empty string")
        if not isinstance(self.source_excerpt, str):
            raise ValueError("source_excerpt must be a string")
        if self.temporal_range is not None and self.sequence_range is not None:
            raise ValueError("EvidenceRef cannot have both temporal_range and sequence_range")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_excerpt": self.source_excerpt,
            "temporal_range": self.temporal_range.to_dict() if self.temporal_range else None,
            "sequence_range": self.sequence_range.to_dict() if self.sequence_range else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvidenceRef:
        if not isinstance(d, dict):
            raise TypeError("EvidenceRef payload must be a dict")
        if "chunk_id" in d:
            raise ValueError("chunk_id is strictly removed from canonical EvidenceRef")
        tr = TemporalRange.from_dict(d["temporal_range"]) if d.get("temporal_range") else None
        sr = SequenceRange.from_dict(d["sequence_range"]) if d.get("sequence_range") else None
        return cls(
            evidence_id=str(d["evidence_id"]),
            source_excerpt=str(d.get("source_excerpt", "")),
            temporal_range=tr,
            sequence_range=sr,
        )


# ----------------------------------------------------------------------
# Attribution: Source-Neutral Speaker & Actor Attribution
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class AttributionInfo:
    """Source-neutral attribution structure.

    INVARIANT: Unverified speech from ordinary ASR defaults to
    speaker_name=None, speaker_id=None, attribution_status=unverified_speaker.
    Even if source_actor_name is known, it cannot be automatically assumed
    to be the speaker.
    """
    source_actor_name: Optional[str] = None
    source_actor_id: Optional[str] = None
    speaker_name: Optional[str] = None
    speaker_id: Optional[str] = None
    attribution_status: AttributionStatus = AttributionStatus.UNVERIFIED_SPEAKER

    def __post_init__(self) -> None:
        if isinstance(self.attribution_status, str):
            object.__setattr__(self, "attribution_status", AttributionStatus(self.attribution_status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_actor_name": self.source_actor_name,
            "source_actor_id": self.source_actor_id,
            "speaker_name": self.speaker_name,
            "speaker_id": self.speaker_id,
            "attribution_status": self.attribution_status.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AttributionInfo:
        if not isinstance(d, dict):
            raise TypeError("AttributionInfo payload must be a dict")
        status_val = d.get("attribution_status", AttributionStatus.UNVERIFIED_SPEAKER.value)
        return cls(
            source_actor_name=d.get("source_actor_name"),
            source_actor_id=d.get("source_actor_id"),
            speaker_name=d.get("speaker_name"),
            speaker_id=d.get("speaker_id"),
            attribution_status=AttributionStatus(status_val),
        )


# ----------------------------------------------------------------------
# Entity Mention
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class EntityMention:
    """Structured mention of an entity inside a knowledge unit statement."""
    entity_name: str
    category: str

    def __post_init__(self) -> None:
        if not self.entity_name or not isinstance(self.entity_name, str):
            raise ValueError("entity_name must be a non-empty string")
        if not self.category or not isinstance(self.category, str):
            raise ValueError("category must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_name": self.entity_name,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityMention:
        if not isinstance(d, dict):
            raise TypeError("EntityMention payload must be a dict")
        return cls(
            entity_name=str(d["entity_name"]),
            category=str(d["category"]),
        )


# ----------------------------------------------------------------------
# Provenance & Lineage Layers
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractionProvenance:
    """Document-level shared metadata for LLM extraction run."""
    backend: str
    model: str
    prompt_version: str
    temperature: float
    generated_at: str
    evidence_manifest_fingerprint: str
    evidence_chunks_fingerprint: str
    knowledge_schema_version: str = KNOWLEDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.knowledge_schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ValueError(
                f"knowledge_schema_version must be '{KNOWLEDGE_SCHEMA_VERSION}', got '{self.knowledge_schema_version}'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "knowledge_schema_version": self.knowledge_schema_version,
            "temperature": self.temperature,
            "generated_at": self.generated_at,
            "evidence_manifest_fingerprint": self.evidence_manifest_fingerprint,
            "evidence_chunks_fingerprint": self.evidence_chunks_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExtractionProvenance:
        if not isinstance(d, dict):
            raise TypeError("ExtractionProvenance payload must be a dict")
        return cls(
            backend=str(d["backend"]),
            model=str(d["model"]),
            prompt_version=str(d["prompt_version"]),
            temperature=float(d["temperature"]),
            generated_at=str(d["generated_at"]),
            evidence_manifest_fingerprint=str(d["evidence_manifest_fingerprint"]),
            evidence_chunks_fingerprint=str(d["evidence_chunks_fingerprint"]),
            knowledge_schema_version=str(d.get("knowledge_schema_version", KNOWLEDGE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class ExtractionLineage:
    """Unit-level lineage tracking origin chunks, run, and merge history."""
    extraction_run_id: str
    input_chunk_ids: list[str]
    source_candidate_ids: list[str]
    candidate_id: Optional[str] = None
    merge_strategy: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.extraction_run_id:
            raise ValueError("extraction_run_id cannot be empty")
        if not self.input_chunk_ids or len(self.input_chunk_ids) < 1:
            raise ValueError("input_chunk_ids cannot be empty")
        if not self.source_candidate_ids or len(self.source_candidate_ids) < 1:
            raise ValueError("source_candidate_ids cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "extraction_run_id": self.extraction_run_id,
            "input_chunk_ids": list(self.input_chunk_ids),
            "candidate_id": self.candidate_id,
            "source_candidate_ids": list(self.source_candidate_ids),
            "merge_strategy": self.merge_strategy,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExtractionLineage:
        if not isinstance(d, dict):
            raise TypeError("ExtractionLineage payload must be a dict")
        cid = d.get("candidate_id")
        scids = d.get("source_candidate_ids")
        if scids is None:
            scids = [cid] if cid else []
        return cls(
            extraction_run_id=str(d["extraction_run_id"]),
            input_chunk_ids=[str(x) for x in d.get("input_chunk_ids", [])],
            candidate_id=str(cid) if cid else None,
            source_candidate_ids=[str(x) for x in scids],
            merge_strategy=d.get("merge_strategy"),
        )


# ----------------------------------------------------------------------
# Normalization & Deterministic Identity Computation
# ----------------------------------------------------------------------

def normalize_statement(statement: str) -> str:
    """Normalizes statement string according to M4 contract:
    - strip leading/trailing whitespace
    - collapse internal whitespace sequences to a single space
    """
    if not isinstance(statement, str):
        raise TypeError("statement must be a string")
    return re.sub(r"\s+", " ", statement.strip())


def compute_knowledge_unit_id(
    schema_version: str,
    canonical_id: str,
    unit_type: UnitType | str,
    evidence_refs: list[EvidenceRef] | list[str],
    normalized_statement: str,
) -> str:
    """Deterministically computes a canonical knowledge_unit_id.

    FORMULA:
      raw = f"{schema_version}|{canonical_id}|{type_str}|{canonical_ordered_eids}|{norm_statement}"
      hash = sha256(raw.encode("utf-8")).hexdigest()[:16]
      return f"ku_{hash}"

    INVARIANT:
      - canonical_ordered_eids strictly follows the input sequence of evidence_refs.
      - NEVER sort evidence IDs alphabetically.
      - Processing chunk IDs are NEVER included.
    """
    type_str = unit_type.value if isinstance(unit_type, UnitType) else str(unit_type)

    if evidence_refs and isinstance(evidence_refs[0], EvidenceRef):
        eids = [ref.evidence_id for ref in evidence_refs]  # type: ignore[union-attr]
    else:
        eids = [str(e) for e in evidence_refs]

    canonical_ordered_eids = ",".join(eids)
    norm_statement = normalize_statement(normalized_statement)

    raw_str = f"{schema_version}|{canonical_id}|{type_str}|{canonical_ordered_eids}|{norm_statement}"
    digest = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:16]
    return f"ku_{digest}"


# ----------------------------------------------------------------------
# Canonical Knowledge Unit
# ----------------------------------------------------------------------

@dataclass
class CanonicalKnowledgeUnit:
    """Atomic, typed, grounded knowledge unit adhering to knowledge-units-v1."""
    knowledge_unit_id: str
    canonical_id: str
    unit_type: UnitType
    statement: str
    evidence_refs: list[EvidenceRef]
    attribution: AttributionInfo
    extraction_confidence: float
    verification_status: VerificationStatus = VerificationStatus.NOT_CHECKED
    entities: list[EntityMention] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    extraction_lineage: ExtractionLineage = field(default_factory=lambda: ExtractionLineage(
        extraction_run_id="unspecified",
        input_chunk_ids=["unspecified"],
        source_candidate_ids=["unspecified"],
    ))

    def __post_init__(self) -> None:
        if isinstance(self.unit_type, str):
            self.unit_type = UnitType(self.unit_type)
        if isinstance(self.verification_status, str):
            self.verification_status = VerificationStatus(self.verification_status)

        self.validate()

    def validate(self) -> None:
        if not self.knowledge_unit_id or not re.match(r"^ku_[a-f0-9]{16}$", self.knowledge_unit_id):
            raise ValueError(f"Invalid knowledge_unit_id format: '{self.knowledge_unit_id}'")
        if not self.canonical_id:
            raise ValueError("canonical_id cannot be empty")
        if not self.statement or not self.statement.strip():
            raise ValueError("statement cannot be empty")
        if not self.evidence_refs or len(self.evidence_refs) < 1:
            raise ValueError("CanonicalKnowledgeUnit must have at least 1 evidence_ref")
        if not (0.0 <= self.extraction_confidence <= 1.0):
            raise ValueError(f"extraction_confidence must be in [0.0, 1.0], got {self.extraction_confidence}")

        # Verification question structural invariant
        if self.unit_type == UnitType.VERIFICATION_QUESTION:
            if self.attribution.attribution_status != AttributionStatus.SYSTEM_DERIVED:
                raise ValueError(
                    f"verification_question must have attribution_status == 'system_derived', "
                    f"got '{self.attribution.attribution_status.value}'"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_unit_id": self.knowledge_unit_id,
            "canonical_id": self.canonical_id,
            "unit_type": self.unit_type.value,
            "statement": self.statement,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "attribution": self.attribution.to_dict(),
            "extraction_confidence": round(self.extraction_confidence, 4),
            "verification_status": self.verification_status.value,
            "entities": [e.to_dict() for e in self.entities],
            "topics": list(self.topics),
            "extraction_lineage": self.extraction_lineage.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanonicalKnowledgeUnit:
        if not isinstance(d, dict):
            raise TypeError("CanonicalKnowledgeUnit payload must be a dict")
        if "relationships" in d:
            raise ValueError("relationships field is deferred and not accepted in knowledge-units-v1")

        refs = [EvidenceRef.from_dict(r) for r in d.get("evidence_refs", [])]
        attr = AttributionInfo.from_dict(d.get("attribution", {}))
        entities = [EntityMention.from_dict(e) for e in d.get("entities", [])]
        topics = [str(t) for t in d.get("topics", [])]
        lineage = ExtractionLineage.from_dict(d.get("extraction_lineage", {}))

        return cls(
            knowledge_unit_id=str(d["knowledge_unit_id"]),
            canonical_id=str(d["canonical_id"]),
            unit_type=UnitType(d["unit_type"]),
            statement=str(d["statement"]),
            evidence_refs=refs,
            attribution=attr,
            extraction_confidence=float(d["extraction_confidence"]),
            verification_status=VerificationStatus(d.get("verification_status", VerificationStatus.NOT_CHECKED.value)),
            entities=entities,
            topics=topics,
            extraction_lineage=lineage,
        )


# ----------------------------------------------------------------------
# Top-Level Canonical Document Container
# ----------------------------------------------------------------------

@dataclass
class CanonicalKnowledgeUnitsDocument:
    """Top-level container document serialized to knowledge_units.json."""
    canonical_id: str
    generated_at: str
    unit_count: int
    units: list[CanonicalKnowledgeUnit]
    extraction_provenance: ExtractionProvenance
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be '{KNOWLEDGE_SCHEMA_VERSION}', got '{self.schema_version}'"
            )
        if self.extraction_provenance.knowledge_schema_version != self.schema_version:
            raise ValueError(
                f"extraction_provenance.knowledge_schema_version "
                f"('{self.extraction_provenance.knowledge_schema_version}') "
                f"must match document schema_version ('{self.schema_version}')"
            )
        if self.unit_count != len(self.units):
            raise ValueError(
                f"unit_count ({self.unit_count}) does not match len(units) ({len(self.units)})"
            )
        for idx, u in enumerate(self.units):
            if u.canonical_id != self.canonical_id:
                raise ValueError(
                    f"Unit at index {idx} has canonical_id '{u.canonical_id}' "
                    f"mismatching document canonical_id '{self.canonical_id}'"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_id": self.canonical_id,
            "generated_at": self.generated_at,
            "unit_count": self.unit_count,
            "units": [u.to_dict() for u in self.units],
            "extraction_provenance": self.extraction_provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanonicalKnowledgeUnitsDocument:
        if not isinstance(d, dict):
            raise TypeError("CanonicalKnowledgeUnitsDocument payload must be a dict")
        units = [CanonicalKnowledgeUnit.from_dict(u) for u in d.get("units", [])]
        prov = ExtractionProvenance.from_dict(d.get("extraction_provenance", {}))
        return cls(
            schema_version=str(d.get("schema_version", KNOWLEDGE_SCHEMA_VERSION)),
            canonical_id=str(d["canonical_id"]),
            generated_at=str(d["generated_at"]),
            unit_count=int(d["unit_count"]),
            units=units,
            extraction_provenance=prov,
        )


# ----------------------------------------------------------------------
# Factory & Contextual Helpers
# ----------------------------------------------------------------------

def create_knowledge_unit(
    canonical_id: str,
    unit_type: UnitType | str,
    statement: str,
    evidence_refs: list[EvidenceRef],
    attribution: AttributionInfo,
    extraction_confidence: float,
    extraction_lineage: ExtractionLineage,
    verification_status: VerificationStatus | str = VerificationStatus.NOT_CHECKED,
    entities: list[EntityMention] | None = None,
    topics: list[str] | None = None,
    schema_version: str = KNOWLEDGE_SCHEMA_VERSION,
) -> CanonicalKnowledgeUnit:
    """Factory helper to construct a CanonicalKnowledgeUnit with deterministic ID computation."""
    norm_stmt = normalize_statement(statement)
    u_type = UnitType(unit_type) if isinstance(unit_type, str) else unit_type
    v_status = VerificationStatus(verification_status) if isinstance(verification_status, str) else verification_status
    ku_id = compute_knowledge_unit_id(
        schema_version=schema_version,
        canonical_id=canonical_id,
        unit_type=u_type,
        evidence_refs=evidence_refs,
        normalized_statement=norm_stmt,
    )
    return CanonicalKnowledgeUnit(
        knowledge_unit_id=ku_id,
        canonical_id=canonical_id,
        unit_type=u_type,
        statement=norm_stmt,
        evidence_refs=evidence_refs,
        attribution=attribution,
        extraction_confidence=extraction_confidence,
        verification_status=v_status,
        entities=entities or [],
        topics=topics or [],
        extraction_lineage=extraction_lineage,
    )


def validate_observation_grounding(
    unit: CanonicalKnowledgeUnit,
    manifest_modalities_by_eid: dict[str, str],
) -> bool:
    """Validates that an observation unit is grounded by at least one
    direct machine-perceptual evidence modality (visual_text, visual_description, perceptual_metric).
    Returns True if valid, raises ValueError if violated.
    """
    if unit.unit_type != UnitType.OBSERVATION:
        return True

    perceptual_modalities = {"visual_text", "visual_description", "perceptual_metric"}
    for ref in unit.evidence_refs:
        modality = manifest_modalities_by_eid.get(ref.evidence_id)
        if modality in perceptual_modalities:
            return True

    raise ValueError(
        f"Observation unit '{unit.knowledge_unit_id}' must cite at least one direct "
        f"machine-observed evidence item with modality in {perceptual_modalities}, "
        f"got: {[manifest_modalities_by_eid.get(r.evidence_id, 'unknown') for r in unit.evidence_refs]}"
    )


def adapt_legacy_point(
    point: dict[str, Any],
    canonical_id: str,
    evidence_refs: list[EvidenceRef],
    source_actor_name: Optional[str] = None,
    source_actor_id: Optional[str] = None,
    extraction_run_id: str = "legacy_migration",
    input_chunk_id: str = "chunk_legacy",
) -> CanonicalKnowledgeUnit:
    """Compatibility helper to adapt legacy knowledge points (author_claim, author_opinion)
    into CanonicalKnowledgeUnit without guessing unverified speaker identity.
    """
    raw_type = point.get("type", "claim")
    if raw_type == "author_claim":
        canonical_type = UnitType.CLAIM
    elif raw_type == "author_opinion":
        canonical_type = UnitType.OPINION
    elif raw_type == "verification_question":
        canonical_type = UnitType.VERIFICATION_QUESTION
    else:
        canonical_type = UnitType(raw_type)

    statement = point.get("content") or point.get("title") or ""
    conf = float(point.get("confidence", point.get("extraction_confidence", 0.9)))

    if canonical_type == UnitType.VERIFICATION_QUESTION:
        attr_status = AttributionStatus.SYSTEM_DERIVED
    else:
        attr_status = AttributionStatus.UNVERIFIED_SPEAKER

    attr = AttributionInfo(
        source_actor_name=source_actor_name,
        source_actor_id=source_actor_id,
        speaker_name=None,
        speaker_id=None,
        attribution_status=attr_status,
    )

    norm_stmt = normalize_statement(statement)
    ku_id = compute_knowledge_unit_id(
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
        canonical_id=canonical_id,
        unit_type=canonical_type,
        evidence_refs=evidence_refs,
        normalized_statement=norm_stmt,
    )

    lineage = ExtractionLineage(
        extraction_run_id=extraction_run_id,
        input_chunk_ids=[input_chunk_id],
        candidate_id=point.get("id") or point.get("local_id") or ku_id,
        source_candidate_ids=[point.get("id") or point.get("local_id") or ku_id],
        merge_strategy=None,
    )

    return CanonicalKnowledgeUnit(
        knowledge_unit_id=ku_id,
        canonical_id=canonical_id,
        unit_type=canonical_type,
        statement=norm_stmt,
        evidence_refs=evidence_refs,
        attribution=attr,
        extraction_confidence=conf,
        verification_status=VerificationStatus(point.get("verification_status", VerificationStatus.NOT_CHECKED.value)),
        entities=[],
        topics=[],
        extraction_lineage=lineage,
    )
