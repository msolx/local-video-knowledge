from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..backends.llm import (
    DEFAULT_VERIFICATION_STATUS,
    ensure_segment_ids,
    generate_global_merge,
    generate_knowledge,
    knowledge_fingerprint,
    transcript_fingerprint,
)
from ..storage import atomic_write_json, load_json
from .chunker import TranscriptChunk, chunk_transcript, segment_token_estimate


PLATFORMS = {"douyin", "bilibili", "youtube", "other"}


def _hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_source_schema(metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate legacy/manual metadata without inferring unavailable source facts."""
    result = dict(metadata)
    source = result.get("source")
    if isinstance(source, dict):
        normalized = dict(source)
    else:
        input_path = result.get("original_input_path") or result.get("local_video_path")
        normalized = {
            "platform": "other", "source_type": "manual_file", "source_url": result.get("source_url"),
            "platform_content_id": None, "author_name": result.get("author"), "author_id": None,
            "title": result.get("title"), "published_at": result.get("publish_time"),
            "collected_at": result.get("download_time"),
            "original_filename": Path(input_path).name if input_path else None,
        }
    normalized["platform"] = normalized.get("platform") if normalized.get("platform") in PLATFORMS else "other"
    for key in ("source_type", "source_url", "platform_content_id", "author_name", "author_id", "title", "published_at", "collected_at", "original_filename"):
        normalized.setdefault(key, None)
    changed = result.get("source") != normalized
    result["source"] = normalized
    return result, changed


def _source_provenance(video_id: str, source: dict[str, Any], evidence_segment_ids: list[str]) -> dict[str, Any]:
    return {
        "video_id": video_id, "platform": source["platform"], "source_url": source["source_url"],
        "author_name": source["author_name"], "author_id": source["author_id"],
        "evidence_segment_ids": evidence_segment_ids,
    }


def _attach_provenance(knowledge: dict[str, Any], video_id: str, source: dict[str, Any]) -> dict[str, Any]:
    result = dict(knowledge)
    conclusion = dict(result["one_sentence_conclusion"])
    conclusion["provenance"] = [_source_provenance(video_id, source, conclusion["evidence_segment_ids"])]
    result["one_sentence_conclusion"] = conclusion
    points = []
    for point in result["knowledge_points"]:
        item = dict(point)
        item["verification_status"] = item.get("verification_status", DEFAULT_VERIFICATION_STATUS)
        item["trust"] = None
        item["provenance"] = [_source_provenance(video_id, source, item["evidence_segment_ids"])]
        points.append(item)
    result["knowledge_points"] = points
    result["source"] = source
    return result


def _audio_unified(item: dict[str, Any]) -> dict[str, Any] | None:
    evidence = item.get("evidence", [])
    if not evidence:
        return None
    return {
        "source_type": "audio_asr",
        "segment_ids": list(item.get("evidence_segment_ids", [])),
        "start": min(float(value["start"]) for value in evidence),
        "end": max(float(value["end"]) for value in evidence),
    }


def _hydrate_visual_item(item: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = dict(item)
    ids = [value for value in result.get("visual_evidence_ids", []) if value in index]
    result["visual_evidence_ids"] = ids
    result["visual_evidence"] = [index[value] for value in ids]
    existing = [value for value in result.get("unified_evidence", []) if value.get("source_type") == "audio_asr"]
    audio = _audio_unified(result)
    if audio and not existing:
        existing.append(audio)
    for value in ids:
        visual = index[value]
        existing.append({
            "source_type": visual["source_type"], "visual_evidence_ids": [value],
            "frame_ids": visual.get("frame_ids", []), "start": visual["start"], "end": visual["end"],
        })
    result["unified_evidence"] = existing
    return result


def _attach_visual_evidence(knowledge: dict[str, Any], visual_evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Hydrate visual IDs after local or global extraction; audio remains authoritative."""
    index = {item["id"]: item for item in visual_evidence if item.get("status") == "completed"}
    result = dict(knowledge)
    result["one_sentence_conclusion"] = _hydrate_visual_item(result["one_sentence_conclusion"], index)
    result["knowledge_points"] = [_hydrate_visual_item(point, index) for point in result["knowledge_points"]]
    return result


def _chunk_payload(chunk: TranscriptChunk, chunk_fingerprint: str, local_fingerprint: str, generated: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "knowledge-v2.3-local",
        "chunk": {
            "chunk_id": chunk.chunk_id, "segment_ids": [segment["id"] for segment in chunk.segments],
            "overlap_segment_ids": chunk.overlap_segment_ids, "estimated_input_tokens": chunk.estimated_input_tokens,
            "chunk_fingerprint": chunk_fingerprint,
        },
        "provenance": {**provenance, "local_extraction_fingerprint": local_fingerprint}, "knowledge": generated,
    }


def _local_items(chunks: list[tuple[TranscriptChunk, dict[str, Any]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chunk, generated in chunks:
        for point in generated["knowledge_points"]:
            items.append({
                "local_id": f"{chunk.chunk_id}_{point['id']}", "chunk_id": chunk.chunk_id, "type": point["type"],
                "title": point["title"], "content": point["content"],
                "evidence_segment_ids": point["evidence_segment_ids"],
                "visual_evidence_ids": point.get("visual_evidence_ids", []),
            })
    return items


def _chunk_visual_items(chunk: TranscriptChunk, visual_document: dict[str, Any], visual_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = {segment["id"] for segment in chunk.segments}
    triggers = {request["id"]: set(request.get("trigger_segment_ids", [])) for request in visual_document.get("requests", [])}
    return [item for item in visual_items if triggers.get(item.get("visual_request_id"), set()) & chunk_ids]


def build_knowledge(
    metadata: dict[str, Any], transcript: list[dict[str, Any]], llm_config: dict[str, Any],
    knowledge_config: dict[str, Any], work_dir: Path, visual_document: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create Knowledge v2.3 from fixed ASR text and selected visual evidence."""
    transcript, _ = ensure_segment_ids(transcript)
    metadata, _ = ensure_source_schema(metadata)
    visual_document = visual_document or {"visual_evidence": [], "requests": [], "fingerprints": {}}
    visual_items = list(visual_document.get("visual_evidence", []))
    visual_fingerprint = _hash(visual_document.get("fingerprints", {}))
    source, video_id = metadata["source"], metadata["video_id"]
    chunking = knowledge_config.get("chunking", {})
    enabled = bool(chunking.get("enabled", True))
    max_tokens, overlap_tokens = int(chunking.get("max_input_tokens", 10000)), int(chunking.get("overlap_tokens", 800))
    total_tokens = sum(segment_token_estimate(segment) for segment in transcript)
    chunks = chunk_transcript(transcript, max_input_tokens=max_tokens, overlap_tokens=overlap_tokens) if enabled else []
    mode = "hierarchical" if len(chunks) > 1 else "single_pass"
    identity_config = {**knowledge_config, "visual_fingerprint": visual_fingerprint}
    base_fingerprint = knowledge_fingerprint(transcript, llm_config, identity_config)
    chunking_fingerprint = _hash({"transcript_hash": transcript_fingerprint(transcript), "chunking": chunking})

    if mode == "single_pass":
        generated, local_provenance = generate_knowledge(metadata, transcript, llm_config, identity_config, visual_items)
        generated = _attach_provenance(_attach_visual_evidence(generated, visual_items), video_id, source)
        generated["unresolved_visual_references"] = [item for item in visual_items if item.get("status") == "unresolved_visual_reference"]
        provenance = {
            **local_provenance, "knowledge_fingerprint": base_fingerprint,
            "execution": {"mode": mode, "estimated_transcript_tokens": total_tokens, "chunk_count": 1,
                          "chunking_fingerprint": chunking_fingerprint, "local_extraction_fingerprint": base_fingerprint,
                          "global_merge_fingerprint": None, "visual_fingerprint": visual_fingerprint},
        }
        return generated, provenance

    chunk_dir = work_dir / "knowledge_chunks"
    local_base_fingerprint = _hash({"chunking_fingerprint": chunking_fingerprint, "model": llm_config,
                                    "local_prompt": llm_config.get("prompt_version"), "visual_fingerprint": visual_fingerprint})
    local_results: list[tuple[TranscriptChunk, dict[str, Any]]] = []
    local_seconds = 0.0
    for chunk in chunks:
        path = chunk_dir / f"{chunk.chunk_id}.json"
        chunk_fp = _hash({"local_base": local_base_fingerprint, "segment_ids": [segment["id"] for segment in chunk.segments], "transcript": chunk.segments})
        cached = load_json(path, {})
        if cached.get("chunk", {}).get("chunk_fingerprint") == chunk_fp:
            generated, local_provenance = cached["knowledge"], cached.get("provenance", {})
        else:
            chunk_visual = _chunk_visual_items(chunk, visual_document, visual_items)
            generated, local_provenance = generate_knowledge(metadata, chunk.segments, llm_config, identity_config, chunk_visual)
            generated = _attach_provenance(_attach_visual_evidence(generated, chunk_visual), video_id, source)
            atomic_write_json(path, _chunk_payload(chunk, chunk_fp, local_base_fingerprint, generated, local_provenance))
        local_seconds += float(local_provenance.get("processing_seconds", 0))
        local_results.append((chunk, generated))
    local_items = _local_items(local_results)
    merge_input = {"schema_version": "knowledge-v2.3-merge-input", "video_id": video_id,
                   "chunking_fingerprint": chunking_fingerprint, "local_extraction_fingerprint": local_base_fingerprint,
                   "local_item_count": len(local_items), "local_items": local_items}
    atomic_write_json(chunk_dir / "merge_input.json", merge_input)
    generated, merge_provenance = generate_global_merge(local_items, transcript, llm_config)
    generated = _attach_provenance(_attach_visual_evidence(generated, visual_items), video_id, source)
    generated["unresolved_visual_references"] = [item for item in visual_items if item.get("status") == "unresolved_visual_reference"]
    merge_fingerprint = _hash({"local_extraction_fingerprint": local_base_fingerprint, "merge_prompt": llm_config.get("prompt_version"), "model": llm_config, "visual_fingerprint": visual_fingerprint})
    provenance = {
        "backend": merge_provenance["backend"], "model": merge_provenance["model"], "model_parameters": merge_provenance["model_parameters"],
        "prompt_version": llm_config.get("prompt_version"), "schema_version": llm_config.get("schema_version"),
        "processing_seconds": round(local_seconds + merge_provenance["processing_seconds"], 3),
        "transcript_hash": transcript_fingerprint(transcript), "knowledge_fingerprint": base_fingerprint,
        "execution": {"mode": mode, "estimated_transcript_tokens": total_tokens, "chunk_count": len(chunks),
                      "chunking_fingerprint": chunking_fingerprint, "local_extraction_fingerprint": local_base_fingerprint,
                      "global_merge_fingerprint": merge_fingerprint, "local_processing_seconds": round(local_seconds, 3),
                      "global_merge_seconds": merge_provenance["processing_seconds"], "local_item_count": len(local_items),
                      "final_item_count": len(generated["knowledge_points"]), "visual_fingerprint": visual_fingerprint},
    }
    return generated, provenance
