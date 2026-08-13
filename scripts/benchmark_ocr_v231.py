"""Fixed-frame OCR benchmark for Knowledge v2.3.1; it never touches ASR or Knowledge outputs."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.visual.service import PaddleOCRBackend


def gpu_memory_mib() -> list[int] | None:
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True)
    if result.returncode:
        return None
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def summarize(texts: list[list[str]], scores: list[list[float]]) -> dict[str, Any]:
    retained = [(text, float(score)) for frame_texts, frame_scores in zip(texts, scores) for text, score in zip(frame_texts, frame_scores) if text.strip() and float(score) >= 0.65]
    unique = list(dict.fromkeys(text for text, _ in retained))
    return {"line_count": len(unique), "confidence": round(sum(score for _, score in retained) / len(retained), 4) if retained else 0.0,
            "text": "\n".join(unique)}


def run(label: str, frames: list[dict[str, Any]], config: dict[str, Any], warmup: bool) -> dict[str, Any]:
    started = time.perf_counter()
    before = gpu_memory_mib()
    backend = PaddleOCRBackend(config)
    if warmup:
        backend.warmup(frames[0])
    inference_started = time.perf_counter()
    reads = [backend.read(frame) for frame in frames]
    inference_seconds = time.perf_counter() - inference_started
    after = gpu_memory_mib()
    texts, scores = zip(*reads)
    return {
        "label": label, "initialization_seconds": backend.initialization_seconds,
        "warmup_seconds": backend.warmup_seconds, "inference_3_frames_seconds": round(inference_seconds, 3),
        "total_seconds": round(time.perf_counter() - started, 3), "gpu_memory_before_mib": before,
        "gpu_memory_after_mib": after, "result": summarize(list(texts), list(scores)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=Path, default=Path("data/test_runs/visual_v23_real/requests/vr_001"))
    parser.add_argument("--output", type=Path, default=Path("data/test_runs/ocr_v231_benchmark.json"))
    parser.add_argument("--config", type=Path, default=Path("config/config.json"))
    parser.add_argument("--device", help="Override OCR device, e.g. gpu:0 in the isolated Paddle GPU environment.")
    parser.add_argument("--detector-model", help="Override the local detector model directory name.")
    parser.add_argument("--recognizer-model", help="Override the local recognizer model directory name.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))["visual_evidence"]["ocr"]
    if args.device:
        config["device"] = args.device
    if args.detector_model:
        config["detector_model"] = args.detector_model
    if args.recognizer_model:
        config["recognizer_model"] = args.recognizer_model
    frames = [{"frame_id": path.stem, "path": str(path)} for path in sorted(args.frames.glob("*.jpg"))]
    if len(frames) != 3:
        raise SystemExit(f"Expected exactly three fixed benchmark frames, got {len(frames)} from {args.frames}")
    payload = {"schema_version": "ocr-v2.3.1-benchmark-v1", "frames": frames, "backend_config": config,
               "results": [run("cpu_cold_current_path", frames, config, warmup=False), run("cpu_single_worker_warm_reuse", frames, config, warmup=True)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
