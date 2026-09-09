from __future__ import annotations

import re
from typing import Any

from .models import (
    ChunkingPolicy,
    EvidenceChunk,
    ImageSequenceRange,
    TemporalRange,
)


def estimate_tokens(value: str) -> int:
    """Conservative dependency-free token estimator for CJK/multilingual evidence.
    
    CJK characters and punctuation count as 1 token each.
    ASCII alphanumeric runs count as approx 1 token per 4 characters.
    100% offline and deterministic, requires zero ML runtime.
    """
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", value))
    ascii_runs = re.findall(r"[A-Za-z0-9_./:+#-]+", value)
    ascii_tokens = sum((len(run) + 3) // 4 for run in ascii_runs)
    punctuation = len(re.findall(r"[^\s\w\u3400-\u9fff\uf900-\ufaff]", value))
    return max(1, cjk + ascii_tokens + punctuation)


def window_video_speech_evidence(
    evidence_items: list[dict[str, Any]],
    canonical_id: str,
    policy: ChunkingPolicy,
) -> list[EvidenceChunk]:
    """Partitions video speech evidence into deterministic, bounded-overlap chunks.
    
    Invariants:
    - Never cuts an individual EvidenceItem or text in half.
    - Full unique evidence coverage: every source speech segment is covered.
    - Deterministic ordering and stable IDs (chk_000001, chk_000002...).
    - Overlap is bounded by overlap_segments and explicitly recorded in overlap_evidence_ids.
    - Zero fake timestamps: temporal envelope strictly matches contained evidence.
    """
    speech_items = [it for it in evidence_items if it.get("modality") == "speech"]
    if not speech_items:
        return []

    # Deterministic sorting by temporal start, segment order, and evidence_id
    sorted_items = sorted(
        speech_items,
        key=lambda it: (
            float(it.get("temporal", {}).get("start", 0.0)),
            int(it.get("temporal", {}).get("segment_order", 0)),
            str(it.get("evidence_id", "")),
        ),
    )

    chunks: list[EvidenceChunk] = []
    start_idx = 0
    total_segments = len(sorted_items)
    prev_overlap_count = 0

    while start_idx < total_segments:
        end_idx = start_idx
        accum_tokens = 0
        chunk_start_time = float(sorted_items[start_idx]["temporal"]["start"])

        while end_idx < total_segments:
            item = sorted_items[end_idx]
            text = item.get("payload", {}).get("text", "")
            seg_tokens = estimate_tokens(text)
            curr_duration = float(item["temporal"]["end"]) - chunk_start_time
            seg_count = (end_idx - start_idx) + 1

            if end_idx > start_idx:
                if (
                    curr_duration > policy.max_duration_seconds
                    or (accum_tokens + seg_tokens) > policy.max_tokens
                    or seg_count > policy.max_segments
                ):
                    break

            accum_tokens += seg_tokens
            end_idx += 1

        chunk_items = sorted_items[start_idx:end_idx]
        overlap_ids = [it["evidence_id"] for it in chunk_items[:prev_overlap_count]]

        c_start = float(chunk_items[0]["temporal"]["start"])
        c_end = float(chunk_items[-1]["temporal"]["end"])
        temporal = TemporalRange(
            start=c_start,
            end=c_end,
            duration=round(c_end - c_start, 3),
        )

        # Unique source artifacts referenced by these segments
        seen_artifacts: set[str] = set()
        source_artifacts: list[dict[str, Any]] = []
        for it in chunk_items:
            art = it.get("source_artifact")
            if art and isinstance(art, dict):
                key = (art.get("file_name"), art.get("sha256"))
                if key not in seen_artifacts:
                    seen_artifacts.add(key)
                    source_artifacts.append(art)

        order = len(chunks) + 1
        chunk = EvidenceChunk(
            chunk_id=f"chk_{order:06d}",
            canonical_id=canonical_id,
            content_type="video",
            sequence_order=order,
            modalities=["speech"],
            evidence_ids=[it["evidence_id"] for it in chunk_items],
            overlap_evidence_ids=overlap_ids,
            temporal_range=temporal,
            image_sequence_range=None,
            source_artifacts=source_artifacts,
            verification_status="not_checked",
        )
        chunk.chunk_fingerprint = chunk.compute_fingerprint()
        chunks.append(chunk)

        if end_idx >= total_segments:
            break

        prev_overlap_count = (
            min(policy.overlap_segments, len(chunk_items) - 1)
            if policy.overlap_segments > 0
            else 0
        )
        next_start = end_idx - prev_overlap_count
        if next_start <= start_idx:
            next_start = start_idx + 1  # enforce strict forward progress
        start_idx = next_start

    # Verify 100% unique coverage invariant
    covered_ids = {eid for c in chunks for eid in c.evidence_ids}
    source_ids = {it["evidence_id"] for it in speech_items}
    if covered_ids != source_ids:
        missing = source_ids - covered_ids
        raise RuntimeError(f"Chunking windowing invariant broken: {len(missing)} speech evidence items missing.")

    return chunks


def window_album_visual_evidence(
    evidence_items: list[dict[str, Any]],
    canonical_id: str,
    policy: ChunkingPolicy,
) -> list[EvidenceChunk]:
    """Batches image album visual evidence into deterministic chunks by image sequence.
    
    Invariants:
    - Same-image evidence (OCR and VLM) strictly remains in the same image group.
    - 1-indexed image sequence ordering strictly preserved.
    - No fake temporal ranges (temporal_range is None).
    - Unresolved/partial evidence preserved with verification_status='not_checked'.
    """
    album_items = [
        it for it in evidence_items
        if it.get("modality") in ("visual_text", "visual_description")
    ]
    if not album_items:
        # Fallback if other visual modalities are used
        album_items = list(evidence_items)
    if not album_items:
        return []

    # Group evidence items by image sequence index
    groups: dict[int, list[dict[str, Any]]] = {}
    for it in album_items:
        seq = (
            it.get("sequence", {}).get("sequence_index")
            if it.get("sequence") and isinstance(it["sequence"], dict)
            else (
                it.get("source_artifact", {}).get("sequence_index")
                if it.get("source_artifact") and isinstance(it["source_artifact"], dict)
                else 1
            )
        )
        seq_idx = int(seq) if seq is not None else 1
        groups.setdefault(seq_idx, []).append(it)

    sorted_seqs = sorted(groups.keys())
    batch_size = max(1, policy.album_images_per_chunk)
    chunks: list[EvidenceChunk] = []

    for b_start in range(0, len(sorted_seqs), batch_size):
        b_seqs = sorted_seqs[b_start : b_start + batch_size]
        b_items: list[dict[str, Any]] = []

        for s in b_seqs:
            # Sort items within image group: OCR (visual_text) first, then VLM (visual_description)
            img_items = sorted(
                groups[s],
                key=lambda x: (
                    0 if x.get("modality") == "visual_text" else 1,
                    str(x.get("evidence_id", "")),
                ),
            )
            b_items.extend(img_items)

        seen_artifacts: set[str] = set()
        source_artifacts: list[dict[str, Any]] = []
        modalities_set: set[str] = set()

        for it in b_items:
            mod = it.get("modality")
            if mod:
                modalities_set.add(mod)
            art = it.get("source_artifact")
            if art and isinstance(art, dict):
                key = (art.get("file_name"), art.get("sha256"))
                if key not in seen_artifacts:
                    seen_artifacts.add(key)
                    source_artifacts.append(art)

        seq_range = ImageSequenceRange(
            start_index=min(b_seqs),
            end_index=max(b_seqs),
            image_count=len(b_seqs),
        )

        order = len(chunks) + 1
        chunk = EvidenceChunk(
            chunk_id=f"chk_{order:06d}",
            canonical_id=canonical_id,
            content_type="image_album",
            sequence_order=order,
            modalities=sorted(modalities_set),
            evidence_ids=[it["evidence_id"] for it in b_items],
            overlap_evidence_ids=[],
            temporal_range=None,
            image_sequence_range=seq_range,
            source_artifacts=source_artifacts,
            verification_status="not_checked",
        )
        chunk.chunk_fingerprint = chunk.compute_fingerprint()
        chunks.append(chunk)

    # Verify 100% unique coverage invariant
    covered_ids = {eid for c in chunks for eid in c.evidence_ids}
    source_ids = {it["evidence_id"] for it in album_items}
    if covered_ids != source_ids:
        missing = source_ids - covered_ids
        raise RuntimeError(f"Album chunking invariant broken: {len(missing)} album evidence items missing.")

    return chunks
