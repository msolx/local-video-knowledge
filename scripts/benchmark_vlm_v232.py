"""Standalone Qwen3-VL POC: one frame, three frames, JSON stability and unload."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.visual.vlm import LMStudioVLMBackend, gpu_memory


ROOT = Path(__file__).resolve().parents[1]
FRAMES = sorted((ROOT / "data/test_runs/visual_v23_real/requests/vr_001").glob("*.jpg"))
PNG_FRAME = ROOT / "data/test_runs/vlm_v232_png_input.png"
SETTINGS = {"model_key": "qwen3-vl-8b-instruct", "identifier": "qwen3-vl-8b-v232", "gpu": "max", "context_length": 16384,
            "ttl_seconds": 1800, "lm_studio_cli": os.environ.get("LM_STUDIO_CLI", "lms"), "base_url": "http://127.0.0.1:1234/v1", "max_tokens": 400}


def request(frames: list[Path]) -> dict:
    return {"id": "vr_benchmark", "reason": "path_or_ui", "requested_information": ["identify the displayed hardware configuration"]}, [{"frame_id": f"benchmark_{i:03d}", "path": str(path)} for i, path in enumerate(frames, 1)]


def main() -> int:
    backend = LMStudioVLMBackend(SETTINGS)
    report = {"model": SETTINGS["model_key"], "gpu_before": gpu_memory("nvidia-smi"), "runs": []}
    backend.load(); report["gpu_after_load"] = gpu_memory("nvidia-smi")
    try:
        for label, paths in (("one_jpeg", FRAMES[:1]), ("one_png", [PNG_FRAME]), ("three_images", FRAMES[:3])):
            visual_request, payload_frames = request(paths)
            response, seconds = backend.answer(visual_request, payload_frames, {"text": "Windows 10; RTX 3060; 12GB; 32GB; 100GB", "confidence": .9882}, "直接看这张图。")
            report["runs"].append({"case": label, "frame_count": len(paths), "seconds": seconds, "response": response, "gpu_after": gpu_memory("nvidia-smi")})
    finally:
        report.update(backend.unload())
    out = ROOT / "data/test_runs/vlm_v232_benchmark.json"; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__": raise SystemExit(main())
