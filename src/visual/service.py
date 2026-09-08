from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..storage import atomic_write_json, utc_now
from .vlm import LMStudioVLMBackend, OpenAICompatibleVLMBackend, ocr_is_sufficient


RULES: dict[str, tuple[str, ...]] = {
    "screen_reference": ("如图", "图中", "这张图", "大家看", "直接看", "参考我这个页面", "可参考", "屏幕上", "右边", "左边", "上面", "下面", "这边"),
    "configuration": ("配置", "参数", "这个选项", "这个按钮", "这里填", "这里选择", "改成这个", "按照这个", "按照这里", "如下所示"),
    "path_or_ui": ("目录", "路径", "页面", "工作流", "节点", "选择", "性能界面", "填在"),
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _matching_reasons(text: str) -> list[str]:
    return [kind for kind, phrases in RULES.items() if any(phrase in text for phrase in phrases)]


def visual_pipeline_fingerprint(transcript: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """Identity of all inputs that can change selected visual evidence."""
    return _hash({"transcript": transcript, "rules": RULES, "config": config})


def detect_requests(transcript: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect only direct, high-signal visual references; no full-video scan."""
    pre_roll = float(config.get("pre_roll_seconds", 3))
    post_roll = float(config.get("post_roll_seconds", 4))
    candidates: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript):
        reasons = _matching_reasons(segment["text"])
        if not reasons:
            continue
        # Avoid generic pronouns alone: require a visual/config/UI cue, except
        # explicit "看图/参考页面" phrases which the rules already capture.
        if reasons == ["path_or_ui"] and not any(word in segment["text"] for word in ("目录", "路径", "页面", "工作流", "节点", "性能界面")):
            continue
        adjacent = [segment]
        if index + 1 < len(transcript) and transcript[index + 1]["start"] - segment["end"] <= 1.0:
            adjacent.append(transcript[index + 1])
        start = max(0.0, adjacent[0]["start"] - pre_roll)
        end = adjacent[-1]["end"] + post_roll
        candidates.append({
            "trigger_segment_ids": [item["id"] for item in adjacent], "start": round(start, 3), "end": round(end, 3),
            "reason": ", ".join(reasons), "requested_information": ["画面中与当前语音直接相关的文字、配置、路径或 UI 信息"], "status": "pending",
        })
    # Nearby narration often says “this page / this button” in several consecutive
    # segments. Treat that as one visual question rather than re-reading the same screen.
    merge_gap = float(config.get("request_merge_gap_seconds", 12))
    max_window = float(config.get("max_request_duration_seconds", 24))
    merged: list[dict[str, Any]] = []
    def cue_score(request: dict[str, Any]) -> int:
        reasons = set(request["reason"].split(", "))
        return 3 if "screen_reference" in reasons else 2 if "configuration" in reasons else 1

    for candidate in candidates:
        proposed_end = max(merged[-1]["end"], candidate["end"]) if merged else candidate["end"]
        if merged and cue_score(candidate) == cue_score(merged[-1]) and candidate["start"] - merged[-1]["end"] <= merge_gap and proposed_end - merged[-1]["start"] <= max_window:
            current = merged[-1]
            current["end"] = max(current["end"], candidate["end"])
            current["trigger_segment_ids"] = list(dict.fromkeys(current["trigger_segment_ids"] + candidate["trigger_segment_ids"]))
            current["reason"] = ", ".join(dict.fromkeys((current["reason"] + ", " + candidate["reason"]).split(", ")))
        else:
            merged.append(candidate)
    maximum = int(config.get("max_requests_per_video", 12))
    # When a tutorial contains many generic “page/path” mentions, retain explicit
    # “look at this / reference” and configuration cues first, then restore timeline order.
    def priority(request: dict[str, Any]) -> tuple[int, float]:
        return (-cue_score(request), float(request["start"]))
    result = sorted(sorted(merged, key=priority)[:maximum], key=lambda request: float(request["start"]))
    for index, request in enumerate(result, start=1):
        request["id"] = f"vr_{index:03d}"
    return result


def _extract_frame(ffmpeg: Path, source: Path, timestamp: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "2", "-update", "1", str(output)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1000:])


def _fingerprint(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (9, 8))
    dhash = gray[:, 1:] > gray[:, :-1]
    hist = cv2.calcHist([cv2.resize(image, (64, 36))], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
    return dhash, cv2.normalize(hist, hist).flatten()


def extract_keyframes(request: dict[str, Any], source: Path, ffmpeg: Path, visual_dir: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Low-rate sampling plus dHash/histogram scene dedup; never all frames."""
    interval = float(config.get("sample_interval_seconds", 1.0))
    max_frames = int(config.get("max_frames_per_request", 5))
    dhash_limit = int(config.get("dhash_distance_threshold", 7))
    scene_limit = float(config.get("scene_histogram_threshold", 0.18))
    frame_dir = visual_dir / "requests" / request["id"]
    candidates = []
    current = request["start"]
    number = 0
    previous_hash: np.ndarray | None = None
    previous_hist: np.ndarray | None = None
    while current <= request["end"] + 0.001:
        temp = frame_dir / f"candidate_{number:03d}.jpg"
        _extract_frame(ffmpeg, source, current, temp)
        image = cv2.imread(str(temp))
        if image is None:
            temp.unlink(missing_ok=True)
            current += interval; number += 1; continue
        dhash, hist = _fingerprint(image)
        distance = int(np.count_nonzero(dhash != previous_hash)) if previous_hash is not None else 999
        scene_delta = float(cv2.compareHist(previous_hist.astype("float32"), hist.astype("float32"), cv2.HISTCMP_BHATTACHARYYA)) if previous_hist is not None else 1.0
        keep = previous_hash is None or distance > dhash_limit or scene_delta > scene_limit
        if keep and len(candidates) < max_frames:
            frame_id = f"frame_{int(round(current * 1000)):09d}"
            final = frame_dir / f"{frame_id}.jpg"
            os.replace(temp, final)
            candidates.append({"frame_id": frame_id, "timestamp": round(current, 3), "path": str(final), "dhash_distance": distance, "scene_delta": round(scene_delta, 4)})
        else:
            temp.unlink(missing_ok=True)
        previous_hash, previous_hist = dhash, hist
        current += interval; number += 1
    return candidates


class PaddleOCRBackend:
    name = "paddleocr_ppocrv6"

    def __init__(self, config: dict[str, Any]) -> None:
        started = time.perf_counter()
        os.environ.setdefault("FLAGS_use_mkldnn", "false")
        from paddleocr import PaddleOCR
        model_root = Path(config.get("model_root", Path.home() / ".paddlex" / "official_models"))
        self.detector_model = config.get("detector_model", "PP-OCRv6_medium_det")
        self.recognizer_model = config.get("recognizer_model", "PP-OCRv6_medium_rec")
        self.device = config.get("device", "cpu")
        self.ocr = PaddleOCR(text_detection_model_name=self.detector_model, text_recognition_model_name=self.recognizer_model,
                             text_detection_model_dir=str(model_root / self.detector_model), text_recognition_model_dir=str(model_root / self.recognizer_model),
                             use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, enable_mkldnn=False, device=self.device)
        self.initialization_seconds = round(time.perf_counter() - started, 3)
        self.warmup_seconds = 0.0
        self.inference_seconds = 0.0
        self.inference_calls = 0

    def read(self, frame: dict[str, Any]) -> tuple[list[str], list[float]]:
        started = time.perf_counter()
        result = next(iter(self.ocr.predict(frame["path"])))
        self.inference_seconds += time.perf_counter() - started
        self.inference_calls += 1
        value = result.json.get("res", {})
        return value.get("rec_texts", []), value.get("rec_scores", [])

    def read_detail(self, frame: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        result = next(iter(self.ocr.predict(frame["path"])))
        elapsed = time.perf_counter() - started
        self.inference_seconds += elapsed
        self.inference_calls += 1
        value = result.json.get("res", {})
        dt_polys = value.get("dt_polys", [])
        rec_boxes = value.get("rec_boxes", [])

        def _to_list(obj: Any) -> Any:
            if hasattr(obj, "tolist"):
                return obj.tolist()
            if isinstance(obj, (list, tuple)):
                return [_to_list(x) for x in obj]
            if isinstance(obj, (int, float, str, bool)) or obj is None:
                return obj
            return str(obj)

        return {
            "texts": [str(t) for t in value.get("rec_texts", [])],
            "scores": [float(s) for s in value.get("rec_scores", [])],
            "polygons": _to_list(dt_polys),
            "boxes": _to_list(rec_boxes),
            "inference_seconds": round(elapsed, 4),
        }

    def warmup(self, frame: dict[str, Any]) -> tuple[list[str], list[float]]:
        """Pay first-use cost on a real frame; caller reuses this result."""
        before = self.inference_seconds
        result = self.read(frame)
        self.warmup_seconds += self.inference_seconds - before
        return result

    def timing(self) -> dict[str, Any]:
        return {
            "backend": self.name, "device": self.device, "detector_model": self.detector_model, "recognizer_model": self.recognizer_model,
            "initialization_seconds": round(self.initialization_seconds, 3), "warmup_seconds": round(self.warmup_seconds, 3),
            "inference_seconds": round(self.inference_seconds, 3), "post_warmup_inference_seconds": round(self.inference_seconds - self.warmup_seconds, 3),
            "inference_calls": self.inference_calls,
        }


def _run_gpu_worker(frames: list[dict[str, Any]], config: dict[str, Any]) -> tuple[dict[str, tuple[list[str], list[float]]], dict[str, Any]]:
    """Run one isolated Paddle GPU process for every selected frame of one video."""
    worker = Path(__file__).resolve().parents[2] / "scripts" / "ocr_gpu_worker.py"
    executable = config.get("python_executable")
    if not executable or not Path(executable).is_file():
        raise RuntimeError(f"GPU OCR Python executable was not found: {executable}")
    worker_config = {key: value for key, value in config.items() if key not in {"backend", "fallback", "python_executable", "timeout_seconds"}}
    command = [str(executable), str(worker)]
    started = time.perf_counter()
    input_bytes = json.dumps({"frames": frames, "config": worker_config}, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(command, input=input_bytes,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(config.get("timeout_seconds", 600)))
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"GPU OCR worker failed ({result.returncode}): {stderr[-2000:]}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GPU OCR worker returned invalid JSON: {stdout[-1000:]}") from error
    if payload.get("status") != "completed":
        raise RuntimeError(f"GPU OCR worker did not complete: {payload}")
    reads = {frame_id: (value.get("texts", []), value.get("scores", [])) for frame_id, value in payload.get("reads", {}).items()}
    if set(reads) != {frame["frame_id"] for frame in frames}:
        raise RuntimeError("GPU OCR worker returned an incomplete frame result set.")
    timing = {**payload.get("timing", {}), "worker_total_seconds": round(time.perf_counter() - started, 3), "worker_stderr": stderr[-1000:]}
    return reads, timing


def _ocr_request(request: dict[str, Any], frames: list[dict[str, Any]], backend: PaddleOCRBackend | None, config: dict[str, Any], prefetched: dict[str, tuple[list[str], list[float]]] | None = None) -> dict[str, Any]:
    minimum = float(config.get("minimum_confidence", 0.65))
    lines: list[str] = []
    scores: list[float] = []
    for frame in frames:
        if prefetched and frame["frame_id"] in prefetched:
            texts, values = prefetched[frame["frame_id"]]
        elif backend is None:
            raise RuntimeError(f"No OCR result was returned for {frame['frame_id']}.")
        else:
            texts, values = backend.read(frame)
        for text, score in zip(texts, values):
            if text.strip() and float(score) >= minimum:
                lines.append(text.strip()); scores.append(float(score))
    deduped = list(dict.fromkeys(lines))
    confidence = round(sum(scores) / len(scores), 4) if scores else 0.0
    if not deduped or confidence < minimum:
        return {"id": f"ve_{request['id'][3:]}", "visual_request_id": request["id"], "source_type": "visual_ocr", "frame_ids": [frame["frame_id"] for frame in frames],
                "start": request["start"], "end": request["end"], "text": "", "confidence": confidence, "status": "insufficient_ocr"}
    return {"id": f"ve_{request['id'][3:]}", "visual_request_id": request["id"], "source_type": "visual_ocr", "frame_ids": [frame["frame_id"] for frame in frames],
            "start": request["start"], "end": request["end"], "text": "\n".join(deduped), "confidence": confidence, "status": "completed"}


def build_visual_evidence(source: Path, transcript: list[dict[str, Any]], ffmpeg: Path, visual_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Build selective visual evidence; VLM is loaded only after OCR is insufficient."""
    pipeline_fp = visual_pipeline_fingerprint(transcript, config)
    detection_fp = _hash({"transcript": transcript, "rules": RULES, "config": {key: config.get(key) for key in ("pre_roll_seconds", "post_roll_seconds", "request_merge_gap_seconds", "max_request_duration_seconds", "max_requests_per_video")}})
    requests = detect_requests(transcript, config)
    visual_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(visual_dir / "requests.json", {"schema_version": "visual-requests-v1", "fingerprint": detection_fp, "requests": requests})
    if not requests:
        document = {"schema_version": "visual-evidence-v1", "pipeline_fingerprint": pipeline_fp, "fingerprints": {"visual_detection": detection_fp, "frame_extraction": None, "ocr": None, "vlm": None}, "requests": [], "visual_evidence": [], "generated_at": utc_now()}
        atomic_write_json(visual_dir / "visual_transcript.json", document)
        return document
    started = time.perf_counter()
    ocr_config = config.get("ocr", {})
    vlm_config = config.get("vlm", {})
    evidence: list[dict[str, Any]] = []
    frame_fp_inputs = []
    prepared: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for request in requests:
        frames = extract_keyframes(request, source, ffmpeg, visual_dir, config)
        request["frames"] = frames
        frame_fp_inputs.append({"request": request["id"], "frames": frames})
        prepared.append((request, frames))

    backend: PaddleOCRBackend | None = None
    worker_timing: dict[str, Any] | None = None
    prefetched_all: dict[str, tuple[list[str], list[float]]] = {}
    if ocr_config.get("backend") == "paddleocr_gpu_worker":
        try:
            prefetched_all, worker_timing = _run_gpu_worker([frame for _, frames in prepared for frame in frames], ocr_config)
        except Exception as error:
            fallback = ocr_config.get("fallback")
            if not isinstance(fallback, dict) or fallback.get("backend") != "paddleocr_cpu":
                raise
            logging.warning("GPU OCR unavailable (%s); using configured CPU fallback.", error)
            backend = PaddleOCRBackend(fallback)
            ocr_config = fallback
    else:
        backend = PaddleOCRBackend(ocr_config)

    transcript_index = {segment["id"]: segment for segment in transcript}
    vlm_backend: LMStudioVLMBackend | OpenAICompatibleVLMBackend | None = None
    vlm_timing: dict[str, Any] = {"calls": 0}
    warmup_pending = bool(ocr_config.get("warmup", True)) and not prefetched_all
    for request, frames in prepared:
        prefetched = None
        if warmup_pending and frames:
            assert backend is not None
            prefetched = {frames[0]["frame_id"]: backend.warmup(frames[0])}
            warmup_pending = False
        if prefetched_all:
            prefetched = {frame["frame_id"]: prefetched_all[frame["frame_id"]] for frame in frames}
        item = _ocr_request(request, frames, backend, ocr_config, prefetched)
        if ocr_is_sufficient(request, item):
            request["status"] = "completed"
            evidence.append(item)
            continue
        if vlm_config.get("backend") not in {"lm_studio", "openai_compatible"}:
            request["status"] = "unresolved_visual_reference"
            evidence.append({"id": f"ve_vlm_{request['id'][3:]}", "visual_request_id": request["id"], "source_type": "visual_vlm",
                             "frame_ids": [frame["frame_id"] for frame in frames], "start": request["start"], "end": request["end"],
                             "confidence": 0.0, "status": "unresolved_visual_reference",
                             "reason": "OCR could not answer this visual request and no VLM backend is configured."})
            continue
        # Preserve the OCR result (even when insufficient) separately from any
        # VLM semantic answer. This prevents the VLM from replacing OCR text.
        evidence.append(item)
        try:
            if vlm_backend is None:
                vlm_backend = LMStudioVLMBackend(vlm_config) if vlm_config.get("backend") == "lm_studio" else OpenAICompatibleVLMBackend(vlm_config)
                vlm_backend.load()
            trigger_text = "\n".join(str(transcript_index[segment_id].get("text", "")) for segment_id in request["trigger_segment_ids"] if segment_id in transcript_index)
            # Count an attempted model invocation even if the transport/model
            # fails afterwards.  The final visual evidence still records the
            # unresolved result, while usage reporting can truthfully say VLM
            # was invoked rather than confusing it with an OCR-only request.
            vlm_timing["calls"] += 1
            answer, seconds = vlm_backend.answer(request, frames, item, trigger_text)
            vlm_timing[f"{request['id']}_seconds"] = seconds
            vlm_item = {"id": f"ve_vlm_{request['id'][3:]}", "visual_request_id": request["id"], "source_type": "visual_vlm",
                        "frame_ids": [frame["frame_id"] for frame in frames], "start": request["start"], "end": request["end"], **answer}
            evidence.append(vlm_item); request["status"] = answer["status"]
        except Exception as error:
            logging.warning("Selective VLM fallback failed for %s: %s", request["id"], error)
            request["status"] = "unresolved_visual_reference"
            evidence.append({"id": f"ve_vlm_{request['id'][3:]}", "visual_request_id": request["id"], "source_type": "visual_vlm",
                             "frame_ids": [frame["frame_id"] for frame in frames], "start": request["start"], "end": request["end"], "confidence": 0.0,
                             "status": "unresolved_visual_reference", "reason": "VLM fallback failed; no visual conclusion was generated."})
    if vlm_backend is not None:
        vlm_timing.update(vlm_backend.unload())
    backend_name = "paddleocr_gpu_worker" if worker_timing else (backend.name if backend else "unknown")
    fingerprints = {"visual_detection": detection_fp, "frame_extraction": _hash({"frames": frame_fp_inputs, "config": config}),
                    "ocr": _hash({"backend": backend_name, "config": config.get("ocr", {})}),
                    "vlm": _hash(config.get("vlm", {}))}
    atomic_write_json(visual_dir / "requests.json", {"schema_version": "visual-requests-v1", "fingerprint": detection_fp, "requests": requests})
    timing = {**(worker_timing or (backend.timing() if backend else {})), "vlm": vlm_timing}
    document = {"schema_version": "visual-evidence-v1", "pipeline_fingerprint": pipeline_fp, "fingerprints": fingerprints, "requests": requests, "visual_evidence": evidence,
                "timing": {**timing, "backend": backend_name, "total_seconds": round(time.perf_counter() - started, 3)}, "generated_at": utc_now()}
    atomic_write_json(visual_dir / "visual_transcript.json", document)
    return document
