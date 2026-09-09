"""Milestone M3-05: Long Media Chunking (Evidence -> Evidence Chunks).

Deterministic, grounded evidence windowing and batching over M3-04 evidence_manifest.json.
Guarantees:
- Strictly NO LLM calls or semantic summarization.
- Exact evidence_id referencing and full 100% unique evidence coverage.
- Bounded overlap recorded in overlap_evidence_ids.
- 1-indexed sequential image grouping for album media.
- Verification status strictly preserved as 'not_checked'.
- Sub-second cache resume (< 2ms) on identical fingerprint.
"""
from .models import (
    ChunkingPolicy,
    EvidenceChunk,
    ImageSequenceRange,
    TemporalRange,
)
from .policy import (
    estimate_tokens,
    window_album_visual_evidence,
    window_video_speech_evidence,
)
from .service import (
    EVIDENCE_CHUNKS_FILENAME,
    EVIDENCE_CHUNKS_SCHEMA_VERSION,
    chunk_evidence_manifest,
    compute_chunks_fingerprint,
    load_evidence_chunks,
    verify_evidence_chunks,
    write_evidence_chunks,
)

__all__ = [
    "ChunkingPolicy",
    "EvidenceChunk",
    "ImageSequenceRange",
    "TemporalRange",
    "estimate_tokens",
    "window_album_visual_evidence",
    "window_video_speech_evidence",
    "EVIDENCE_CHUNKS_FILENAME",
    "EVIDENCE_CHUNKS_SCHEMA_VERSION",
    "chunk_evidence_manifest",
    "compute_chunks_fingerprint",
    "load_evidence_chunks",
    "verify_evidence_chunks",
    "write_evidence_chunks",
]
