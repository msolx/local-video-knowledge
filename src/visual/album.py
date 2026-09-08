from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from ..storage import atomic_write_json, atomic_write_text, load_json, utc_now
from .service import PaddleOCRBackend
from .vlm import LMStudioVLMBackend, OpenAICompatibleVLMBackend


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def album_visual_pipeline_fingerprint(album_images: list[Any], config: dict[str, Any]) -> str:
    """Deterministic hash of album image commitments, ordering, and visual configuration."""
    images_repr = [
        {
            "sequence_index": getattr(img, "sequence_index", index),
            "sha256": getattr(img, "sha256", ""),
            "file_name": getattr(img, "file_name", Path(getattr(img, "path", "")).name),
            "size_bytes": getattr(img, "size_bytes", 0),
        }
        for index, img in enumerate(album_images, start=1)
    ]
    ocr_config = config.get("ocr", {})
    vlm_config = config.get("vlm", {})
    return _hash({"album_images": images_repr, "ocr": ocr_config, "vlm": vlm_config})


def _run_album_gpu_worker(
    frames: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Execute isolated Paddle GPU worker process for album images."""
    worker = Path(__file__).resolve().parents[2] / "scripts" / "ocr_gpu_worker.py"
    executable = config.get("python_executable")
    if not executable or not Path(executable).is_file():
        raise RuntimeError(f"GPU OCR Python executable was not found: {executable}")
    worker_config = {
        key: value
        for key, value in config.items()
        if key not in {"backend", "fallback", "python_executable", "timeout_seconds"}
    }
    command = [str(executable), str(worker)]
    input_bytes = json.dumps({"frames": frames, "config": worker_config}, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=int(config.get("timeout_seconds", 600)),
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"GPU OCR worker failed ({result.returncode}): {stderr[-2000:]}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GPU OCR worker returned invalid JSON: {stdout[-1000:]}") from error
    if payload.get("status") != "completed":
        raise RuntimeError(f"GPU OCR worker did not complete: {payload}")
    reads = payload.get("reads", {})
    timing = {
        **payload.get("timing", {}),
        "worker_total_seconds": round(time.perf_counter() - started, 3),
        "worker_stderr": stderr[-1000:],
    }
    return reads, timing


def _run_album_ocr(
    images: list[Any], ocr_config: dict[str, Any]
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Execute OCR across ordered album images with failure isolation."""
    frames = [
        {"frame_id": f"img_{getattr(img, 'sequence_index', i):03d}", "path": str(img.path)}
        for i, img in enumerate(images, start=1)
    ]
    frame_to_seq = {
        f"img_{getattr(img, 'sequence_index', i):03d}": getattr(img, "sequence_index", i)
        for i, img in enumerate(images, start=1)
    }

    backend_name = ocr_config.get("backend", "paddleocr_gpu_worker")
    reads_by_frame: dict[str, dict[str, Any]] = {}
    timing: dict[str, Any] = {}

    if backend_name == "paddleocr_gpu_worker":
        try:
            reads_by_frame, timing = _run_album_gpu_worker(frames, ocr_config)
            timing["backend"] = "paddleocr_gpu_worker"
        except Exception as error:
            fallback = ocr_config.get("fallback")
            if not isinstance(fallback, dict):
                raise
            logging.warning("GPU OCR worker unavailable (%s); using configured CPU fallback.", error)
            backend = PaddleOCRBackend(fallback)
            started = time.perf_counter()
            for index, frame in enumerate(frames):
                fid = frame["frame_id"]
                try:
                    detail = backend.read_detail(frame)
                    reads_by_frame[fid] = {"status": "completed", **detail}
                except Exception as err:
                    reads_by_frame[fid] = {
                        "status": "failed",
                        "error": f"{type(err).__name__}: {err}",
                        "texts": [],
                        "scores": [],
                        "polygons": [],
                        "boxes": [],
                    }
            timing = {**backend.timing(), "worker_total_seconds": round(time.perf_counter() - started, 3)}
    else:
        backend = PaddleOCRBackend(ocr_config)
        started = time.perf_counter()
        for index, frame in enumerate(frames):
            fid = frame["frame_id"]
            try:
                detail = backend.read_detail(frame)
                reads_by_frame[fid] = {"status": "completed", **detail}
            except Exception as err:
                reads_by_frame[fid] = {
                    "status": "failed",
                    "error": f"{type(err).__name__}: {err}",
                    "texts": [],
                    "scores": [],
                    "polygons": [],
                    "boxes": [],
                }
        timing = {**backend.timing(), "worker_total_seconds": round(time.perf_counter() - started, 3)}

    # Map back to sequence_index
    results_by_seq: dict[int, dict[str, Any]] = {}
    for frame in frames:
        fid = frame["frame_id"]
        seq = frame_to_seq[fid]
        results_by_seq[seq] = reads_by_frame.get(
            fid,
            {
                "status": "failed",
                "error": "Image result missing from worker output",
                "texts": [],
                "scores": [],
                "polygons": [],
                "boxes": [],
            },
        )
    return results_by_seq, timing


def render_album_visual_markdown(
    canonical_id: str,
    platform: str,
    platform_content_id: str,
    evidence: list[dict[str, Any]],
    ocr_summary: dict[str, Any],
) -> str:
    """Render a human-readable visual inspection Markdown document for an image album."""
    lines = [
        f"# Image Album Visual & OCR Report",
        f"",
        f"- **Canonical ID**: `{canonical_id}`",
        f"- **Platform**: `{platform}`",
        f"- **Platform Content ID**: `{platform_content_id}`",
        f"- **Total Images**: {ocr_summary.get('total_images', len(evidence))}",
        f"- **OCR Status**: **`{ocr_summary.get('overall_status', 'unknown').upper()}`** "
        f"(Completed: {ocr_summary.get('completed', 0)}, "
        f"Insufficient: {ocr_summary.get('insufficient_or_empty', 0)}, "
        f"Failed: {ocr_summary.get('failed', 0)})",
        f"",
        f"---",
        f"",
    ]
    ocr_items = [e for e in evidence if e.get("source_type") == "visual_ocr"]
    for item in ocr_items:
        seq = item.get("sequence_index", 0)
        file_name = item.get("source_image_file", "unknown")
        status = item.get("status", "unknown")
        confidence = item.get("confidence", 0.0)
        sha = item.get("source_image_sha256", "unknown")
        text = item.get("text", "").strip()

        lines.append(f"### Image {seq}: `{file_name}`")
        lines.append(f"- **Status**: `{status}`")
        lines.append(f"- **Confidence**: `{confidence:.4f}`")
        lines.append(f"- **SHA-256**: `{sha}`")
        if item.get("error"):
            lines.append(f"- **Error**: `{item['error']}`")
        lines.append(f"")
        if text:
            lines.append(f"```text")
            lines.append(text)
            lines.append(f"```")
        else:
            lines.append(f"_No high-confidence text detected._")
        lines.append(f"")

    vlm_items = [e for e in evidence if e.get("source_type") == "visual_vlm"]
    if vlm_items:
        lines.append(f"---")
        lines.append(f"## Visual Language Model (VLM) Semantic Insights")
        lines.append(f"")
        for item in vlm_items:
            seq = item.get("sequence_index", 0)
            status = item.get("status", "unknown")
            answer = item.get("answer", "")
            reason = item.get("reason", "")
            lines.append(f"### Image {seq} VLM Analysis (`{status}`)")
            if answer:
                lines.append(answer)
            elif reason:
                lines.append(f"_Reason: {reason}_")
            lines.append(f"")

    return "\n".join(lines)


def build_album_visual_evidence(
    canonical_asset: Any,
    visual_dir: Path,
    config: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Process ordered formal album images through OCR and optional VLM pipelines.

    Adheres strictly to the following invariants:
    1. CanonicalMediaAsset.album_images are sorted by sequence_index (1..N).
    2. Formal archive source files remain strictly read-only.
    3. Outputs are written exclusively into visual_dir under data/processed/<canonical_id>/visual/.
    4. Deterministic resume / cache skip when inputs and configurations match.
    5. Failure isolation: error on image K does not abort processing of images 1..K-1, K+1..N.
    6. Produces visual-evidence-v1 compliant visual_transcript.json, ocr.json, requests.json, visual.md.
    """
    if not getattr(canonical_asset, "is_album", False) or not getattr(canonical_asset, "album_images", None):
        from ..media_adapter.models import UnsupportedContentTypeError

        raise UnsupportedContentTypeError(
            f"Cannot run album visual pipeline on non-album asset: {getattr(canonical_asset, 'canonical_id', 'unknown')}"
        )

    # Invariant: Sort strictly by 1-indexed sequence_index
    sorted_images = sorted(canonical_asset.album_images, key=lambda x: x.sequence_index)
    total_images = len(sorted_images)

    visual_dir.mkdir(parents=True, exist_ok=True)
    visual_transcript_path = visual_dir / "visual_transcript.json"
    ocr_json_path = visual_dir / "ocr.json"
    requests_json_path = visual_dir / "requests.json"
    visual_md_path = visual_dir / "visual.md"

    ocr_config = config.get("ocr", {})
    vlm_config = config.get("vlm", {})
    pipeline_fp = album_visual_pipeline_fingerprint(sorted_images, config)

    # Idempotency / Cache Hit Check
    if not force and visual_transcript_path.is_file():
        cached_doc = load_json(visual_transcript_path, {})
        if cached_doc.get("pipeline_fingerprint") == pipeline_fp:
            logging.info("Album visual evidence cache hit for %s; skipping inference.", canonical_asset.canonical_id)
            return cached_doc

    started = time.perf_counter()
    minimum_confidence = float(ocr_config.get("minimum_confidence", 0.65))

    # Execute OCR across all images
    ocr_results, ocr_timing = _run_album_ocr(sorted_images, ocr_config)

    evidence_list: list[dict[str, Any]] = []
    ocr_detailed_list: list[dict[str, Any]] = []
    requests_list: list[dict[str, Any]] = []

    completed_count = 0
    insufficient_count = 0
    failed_count = 0

    vlm_calls = 0
    vlm_timing: dict[str, Any] = {"calls": 0}
    vlm_backend: LMStudioVLMBackend | OpenAICompatibleVLMBackend | None = None
    vlm_policy = config.get("vlm_policy", "on_demand")

    for img in sorted_images:
        seq = img.sequence_index
        read_data = ocr_results.get(seq, {})
        status = read_data.get("status", "completed")
        raw_texts: list[str] = read_data.get("texts", [])
        raw_scores: list[float] = [float(s) for s in read_data.get("scores", [])]
        raw_polygons: list[Any] = read_data.get("polygons", [])
        raw_boxes: list[Any] = read_data.get("boxes", [])
        error_msg = read_data.get("error")

        # Build lines structure
        line_items = []
        filtered_texts = []
        filtered_scores = []
        for index, (text, score) in enumerate(zip(raw_texts, raw_scores)):
            poly = raw_polygons[index] if index < len(raw_polygons) else []
            box = raw_boxes[index] if index < len(raw_boxes) else []
            line_item = {"text": text, "confidence": score, "polygon": poly, "box": box}
            line_items.append(line_item)
            if text.strip() and score >= minimum_confidence:
                filtered_texts.append(text.strip())
                filtered_scores.append(score)

        if status == "failed":
            image_status = "failed"
            final_text = ""
            final_confidence = 0.0
            failed_count += 1
        elif filtered_texts:
            image_status = "completed"
            final_text = "\n".join(filtered_texts)
            final_confidence = round(sum(filtered_scores) / len(filtered_scores), 4)
            completed_count += 1
        else:
            image_status = "insufficient_ocr"
            final_text = ""
            final_confidence = round(sum(raw_scores) / len(raw_scores), 4) if raw_scores else 0.0
            insufficient_count += 1

        req_id = f"vr_img_{seq:03d}"
        ev_id = f"ve_img_{seq:03d}"

        # Request entry
        req_entry = {
            "id": req_id,
            "sequence_index": seq,
            "file_name": img.file_name,
            "sha256": img.sha256,
            "status": image_status,
            "requested_information": ["图集单页画面中的文本内容、标题、UI 标注及关键视觉信息"],
        }
        requests_list.append(req_entry)

        # OCR evidence entry (standard visual-evidence-v1 item)
        ocr_evidence_entry = {
            "id": ev_id,
            "visual_request_id": req_id,
            "source_type": "visual_ocr",
            "status": image_status,
            "canonical_id": canonical_asset.canonical_id,
            "platform": canonical_asset.platform,
            "platform_content_id": canonical_asset.platform_content_id,
            "sequence_index": seq,
            "source_image_file": img.file_name,
            "source_image_sha256": img.sha256,
            "source_image_path": str(img.path),
            "byte_size": img.size_bytes,
            "text": final_text,
            "confidence": final_confidence,
            "lines": line_items,
            "ocr_engine": ocr_timing.get("backend", "paddleocr_ppocrv6"),
            "error": error_msg,
        }
        evidence_list.append(ocr_evidence_entry)

        # Detailed OCR record for ocr.json
        ocr_detailed_list.append(
            {
                "sequence_index": seq,
                "file_name": img.file_name,
                "sha256": img.sha256,
                "byte_size": img.size_bytes,
                "status": image_status,
                "confidence": final_confidence,
                "text": final_text,
                "lines": line_items,
                "error": error_msg,
            }
        )

        # Optional VLM handling
        vlm_backend_type = vlm_config.get("backend")
        should_trigger_vlm = False
        if vlm_backend_type in {"lm_studio", "openai_compatible"}:
            if vlm_policy == "always":
                should_trigger_vlm = True
            elif vlm_policy == "on_demand" and image_status in {"insufficient_ocr", "failed"}:
                should_trigger_vlm = True

        if should_trigger_vlm:
            vlm_req_id = f"ve_vlm_img_{seq:03d}"
            try:
                if vlm_backend is None:
                    vlm_backend = (
                        LMStudioVLMBackend(vlm_config)
                        if vlm_backend_type == "lm_studio"
                        else OpenAICompatibleVLMBackend(vlm_config)
                    )
                    vlm_backend.load()
                vlm_calls += 1
                vlm_request = {
                    "id": req_id,
                    "requested_information": ["理解本图片的视觉主题、主体对象、图文关系与核心内容"],
                }
                vlm_frames = [{"frame_id": f"img_{seq:03d}", "path": str(img.path)}]
                answer, vlm_sec = vlm_backend.answer(vlm_request, vlm_frames, ocr_evidence_entry, "")
                vlm_timing[f"img_{seq:03d}_seconds"] = vlm_sec
                evidence_list.append(
                    {
                        "id": vlm_req_id,
                        "visual_request_id": req_id,
                        "source_type": "visual_vlm",
                        "sequence_index": seq,
                        "source_image_file": img.file_name,
                        "source_image_sha256": img.sha256,
                        "status": answer.get("status", "resolved"),
                        "confidence": answer.get("confidence", 1.0),
                        "answer": answer.get("answer", ""),
                        "reason": answer.get("reason", ""),
                    }
                )
            except Exception as vlm_err:
                logging.warning("Optional VLM execution failed for image %d: %s", seq, vlm_err)
                evidence_list.append(
                    {
                        "id": vlm_req_id,
                        "visual_request_id": req_id,
                        "source_type": "visual_vlm",
                        "sequence_index": seq,
                        "source_image_file": img.file_name,
                        "source_image_sha256": img.sha256,
                        "status": "unresolved_visual_reference",
                        "confidence": 0.0,
                        "answer": "",
                        "reason": f"VLM execution failed or offline: {vlm_err}",
                    }
                )

    if vlm_backend is not None:
        vlm_timing.update(vlm_backend.unload())
    vlm_timing["calls"] = vlm_calls

    if failed_count == 0:
        overall_status = "completed"
    elif completed_count == 0 and insufficient_count == 0:
        overall_status = "failed"
    else:
        overall_status = "partial"

    ocr_summary = {
        "total_images": total_images,
        "completed": completed_count,
        "insufficient_or_empty": insufficient_count,
        "failed": failed_count,
        "overall_status": overall_status,
    }

    album_fp = _hash(
        [{"seq": img.sequence_index, "sha": img.sha256, "name": img.file_name} for img in sorted_images]
    )
    ocr_fp = _hash(ocr_config)
    vlm_fp = _hash(vlm_config)

    timing_summary = {
        **ocr_timing,
        "vlm": vlm_timing,
        "total_seconds": round(time.perf_counter() - started, 3),
    }

    # Primary document adhering to visual-evidence-v1
    document = {
        "schema_version": "visual-evidence-v1",
        "content_type": "image_album",
        "canonical_id": canonical_asset.canonical_id,
        "platform": canonical_asset.platform,
        "platform_content_id": canonical_asset.platform_content_id,
        "pipeline_fingerprint": pipeline_fp,
        "fingerprints": {
            "album": album_fp,
            "ocr": ocr_fp,
            "vlm": vlm_fp,
        },
        "image_count": total_images,
        "requests": requests_list,
        "visual_evidence": evidence_list,
        "ocr_summary": ocr_summary,
        "timing": timing_summary,
        "generated_at": utc_now(),
    }

    # Detailed OCR document
    ocr_document = {
        "schema_version": "album-ocr-v1",
        "canonical_id": canonical_asset.canonical_id,
        "platform": canonical_asset.platform,
        "platform_content_id": canonical_asset.platform_content_id,
        "total_images": total_images,
        "ocr_summary": ocr_summary,
        "images": ocr_detailed_list,
        "timing": ocr_timing,
        "generated_at": utc_now(),
    }

    markdown_report = render_album_visual_markdown(
        canonical_id=canonical_asset.canonical_id,
        platform=canonical_asset.platform,
        platform_content_id=canonical_asset.platform_content_id,
        evidence=evidence_list,
        ocr_summary=ocr_summary,
    )

    # Atomic writes
    atomic_write_json(visual_transcript_path, document)
    atomic_write_json(ocr_json_path, ocr_document)
    atomic_write_json(requests_json_path, {"schema_version": "visual-requests-v1", "fingerprint": pipeline_fp, "requests": requests_list})
    atomic_write_text(visual_md_path, markdown_report)

    return document
