from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class ChunkingPolicy:
    """Configurable thresholds for deterministic evidence windowing."""
    max_duration_seconds: float = 120.0
    max_tokens: int = 1000
    max_segments: int = 50
    overlap_segments: int = 2
    album_images_per_chunk: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_duration_seconds": self.max_duration_seconds,
            "max_tokens": self.max_tokens,
            "max_segments": self.max_segments,
            "overlap_segments": self.overlap_segments,
            "album_images_per_chunk": self.album_images_per_chunk,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkingPolicy:
        return cls(
            max_duration_seconds=float(data.get("max_duration_seconds", 120.0)),
            max_tokens=int(data.get("max_tokens", 1000)),
            max_segments=int(data.get("max_segments", 50)),
            overlap_segments=int(data.get("overlap_segments", 2)),
            album_images_per_chunk=int(data.get("album_images_per_chunk", 5)),
        )

    @classmethod
    def from_config(cls, config: Any | None = None) -> ChunkingPolicy:
        if not config or not hasattr(config, "raw"):
            return cls()
        raw_ec = config.raw.get("evidence_chunking")
        if isinstance(raw_ec, dict):
            return cls.from_dict(raw_ec)
        raw_kc = config.raw.get("knowledge", {}).get("chunking")
        if isinstance(raw_kc, dict):
            tokens = int(raw_kc.get("max_input_tokens", 10000))
            max_tok = 1000 if tokens >= 5000 else tokens
            return cls(max_tokens=max_tok)
        return cls()


@dataclass(frozen=True)
class TemporalRange:
    """Temporal bounding envelope for time-based media (speech/video)."""
    start: float
    end: float
    duration: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TemporalRange:
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            duration=float(d["duration"]),
        )


@dataclass(frozen=True)
class ImageSequenceRange:
    """1-indexed sequential index range for image album batching."""
    start_index: int
    end_index: int
    image_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "image_count": self.image_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImageSequenceRange:
        return cls(
            start_index=int(d["start_index"]),
            end_index=int(d["end_index"]),
            image_count=int(d["image_count"]),
        )


@dataclass
class EvidenceChunk:
    """Deterministic, grounded evidence chunk referencing underlying EvidenceItems."""
    chunk_id: str
    canonical_id: str
    content_type: str
    sequence_order: int
    modalities: list[str]
    evidence_ids: list[str]
    overlap_evidence_ids: list[str] = field(default_factory=list)
    temporal_range: TemporalRange | None = None
    image_sequence_range: ImageSequenceRange | None = None
    source_artifacts: list[dict[str, Any]] = field(default_factory=list)
    verification_status: str = "not_checked"
    chunk_fingerprint: str = ""

    def compute_fingerprint(self) -> str:
        """Computes a deterministic SHA-256 fingerprint over chunk structure and references."""
        payload = {
            "chunk_id": self.chunk_id,
            "canonical_id": self.canonical_id,
            "content_type": self.content_type,
            "sequence_order": self.sequence_order,
            "modalities": sorted(self.modalities),
            "evidence_ids": self.evidence_ids,
            "overlap_evidence_ids": self.overlap_evidence_ids,
            "temporal_range": self.temporal_range.to_dict() if self.temporal_range else None,
            "image_sequence_range": self.image_sequence_range.to_dict() if self.image_sequence_range else None,
            "source_artifacts": sorted(self.source_artifacts, key=lambda x: str(x.get("file_name", ""))),
            "verification_status": self.verification_status,
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        fp = self.chunk_fingerprint or self.compute_fingerprint()
        return {
            "chunk_id": self.chunk_id,
            "canonical_id": self.canonical_id,
            "content_type": self.content_type,
            "sequence_order": self.sequence_order,
            "modalities": self.modalities,
            "evidence_ids": self.evidence_ids,
            "overlap_evidence_ids": self.overlap_evidence_ids,
            "temporal_range": self.temporal_range.to_dict() if self.temporal_range else None,
            "image_sequence_range": self.image_sequence_range.to_dict() if self.image_sequence_range else None,
            "source_artifacts": self.source_artifacts,
            "verification_status": self.verification_status,
            "chunk_fingerprint": fp,
        }
