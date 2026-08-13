"""Three bounded acceptance checks for selective VLM fallback."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.visual.vlm import LMStudioVLMBackend, gpu_memory, ocr_is_sufficient

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {"model_key": "qwen3-vl-8b-instruct", "identifier": "qwen3-vl-8b-v232", "gpu": "max", "context_length": 16384,
            "ttl_seconds": 1800, "lm_studio_cli": os.environ.get("LM_STUDIO_CLI", "lms"), "base_url": "http://127.0.0.1:1234/v1", "max_tokens": 450}


def frame(frame_id: str, path: Path) -> dict:
    return {"frame_id": frame_id, "path": str(path)}


def main() -> int:
    config_request = {"id": "vr_001", "reason": "path_or_ui, screen_reference", "requested_information": ["画面中与当前语音直接相关的文字、配置、路径或 UI 信息"]}
    config_ocr = {"status": "completed", "confidence": 0.9882, "text": "Windows 10 及以上\nNVIDIA RTX 3060 及以上\n12GB 及以上\n32GB 及以上\nNVMe 固态硬盘 100GB"}
    report = {"test_a": {"request_id": "vr_001", "ocr_sufficient": ocr_is_sufficient(config_request, config_ocr), "vlm_calls": 0}, "gpu_before": gpu_memory("nvidia-smi")}
    backend = LMStudioVLMBackend(SETTINGS)
    backend.load(); report["gpu_after_load"] = gpu_memory("nvidia-smi")
    try:
        b_request = {"id": "vr_test_b", "reason": "screen_reference", "requested_information": ["确认画面中 Use Image Size 与 获取图像尺寸 两个 ComfyUI 节点的上下空间关系。"]}
        b_ocr = {"status": "insufficient_ocr", "confidence": 0.91, "text": "Use Image Size\n获取图像尺寸\n图像\n宽度\n缩放图像"}
        b_answer, b_seconds = backend.answer(b_request, [frame("frame_000300000", ROOT / "data/test_runs/vlm_v232_test_b.jpg")], b_ocr, "这里可以看到两个节点的上下关系。")
        report["test_b"] = {"ocr_sufficient": ocr_is_sufficient(b_request, b_ocr), "vlm_calls": 1, "seconds": b_seconds, "answer": b_answer}
        c_request = {"id": "vr_test_c", "reason": "screen_reference", "requested_information": ["确认画面中两个节点的连接关系。"]}
        c_answer, c_seconds = backend.answer(c_request, [frame("frame_blurred", ROOT / "data/test_runs/vlm_v232_test_c_blurred.jpg")], {"status": "insufficient_ocr", "confidence": 0.0, "text": ""}, "这里可以看到两个节点的连接关系。")
        report["test_c"] = {"ocr_sufficient": False, "vlm_calls": 1, "seconds": c_seconds, "answer": c_answer}
        report["gpu_peak_observed"] = gpu_memory("nvidia-smi")
    finally:
        report.update(backend.unload())
    out = ROOT / "data/test_runs/vlm_v232_acceptance.json"; out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0

if __name__ == "__main__": raise SystemExit(main())
