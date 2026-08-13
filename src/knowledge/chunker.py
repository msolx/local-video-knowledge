from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: str
    segments: list[dict[str, Any]]
    estimated_input_tokens: int
    overlap_segment_ids: list[str]


def estimate_tokens(value: str) -> int:
    """Conservative dependency-free estimate for CJK/Qwen transcript input.

    The project deliberately does not require a second tokenizer runtime. CJK
    characters and punctuation are charged individually; ASCII runs are charged
    at roughly one token per four characters. The estimate is stored as such and
    is used with a substantial context margin, never presented as exact usage.
    """
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", value))
    ascii_runs = re.findall(r"[A-Za-z0-9_./:+#-]+", value)
    ascii = sum((len(run) + 3) // 4 for run in ascii_runs)
    punctuation = len(re.findall(r"[^\s\w\u3400-\u9fff\uf900-\ufaff]", value))
    return max(1, cjk + ascii + punctuation)


def segment_token_estimate(segment: dict[str, Any]) -> int:
    # Include stable ID/timestamps and JSON framing overhead, not merely speech text.
    return estimate_tokens(str(segment.get("text", ""))) + 12


def chunk_transcript(
    transcript: list[dict[str, Any]], *, max_input_tokens: int, overlap_tokens: int
) -> list[TranscriptChunk]:
    """Build overlapping transcript-only chunks while preserving global IDs."""
    if max_input_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= max_input_tokens:
        raise ValueError("Chunking requires max_input_tokens > overlap_tokens >= 0.")
    if not transcript:
        return []
    sizes = [segment_token_estimate(segment) for segment in transcript]
    chunks: list[TranscriptChunk] = []
    start = 0
    previous_end = 0
    while start < len(transcript):
        end, used = start, 0
        while end < len(transcript) and (used + sizes[end] <= max_input_tokens or end == start):
            used += sizes[end]
            end += 1
        current = transcript[start:end]
        overlap_count = max(0, previous_end - start)
        overlap_ids = [segment["id"] for segment in current[:overlap_count]]
        chunks.append(TranscriptChunk(
            chunk_id=f"chunk_{len(chunks) + 1:03d}", segments=current,
            estimated_input_tokens=used, overlap_segment_ids=overlap_ids,
        ))
        previous_end = end
        if end >= len(transcript):
            break
        next_start, carried = end - 1, sizes[end - 1]
        while next_start > start and carried < overlap_tokens:
            next_start -= 1
            carried += sizes[next_start]
        start = next_start
    return chunks
