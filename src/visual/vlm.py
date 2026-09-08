"""Selective LM Studio VLM fallback for visual requests OCR cannot answer.

The VLM receives program-owned frame IDs as input context only.  It is never
allowed to create timestamps, provenance, segment IDs, or frame IDs.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..backends.openai_compatible import chat_completion


VLM_SCHEMA = {
    "name": "selective_visual_evidence_v1",
    "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["status", "answer", "confidence", "reason"],
        "properties": {
            "status": {"type": "string", "enum": ["resolved", "unresolved_visual_reference"]},
            "answer": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    },
}


def ocr_is_sufficient(request: dict[str, Any], ocr: dict[str, Any]) -> bool:
    """Small, explainable v1 gate.  OCR text UI/configuration is not VLM work."""
    if ocr.get("status") != "completed" or float(ocr.get("confidence", 0)) < 0.65:
        return False
    reason = set(str(request.get("reason", "")).split(", "))
    return bool(ocr.get("text", "").strip()) and bool(reason & {"configuration", "path_or_ui"})


def _image_part(path: str) -> dict[str, Any]:
    source = Path(path)
    suffix = source.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    value = base64.b64encode(source.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{value}"}}


class LMStudioVLMBackend:
    """One explicitly loaded LM Studio visual model, used only when needed."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.cli = settings.get("lm_studio_cli", "lms")
        self.identifier = settings["identifier"]
        self.loaded_here = False

    def load(self) -> None:
        command = [self.cli, "load", settings_model_key(self.settings), "--gpu", self.settings.get("gpu", "max"),
                   "--context-length", str(self.settings.get("context_length", 8192)), "--parallel", "1",
                   "--ttl", str(self.settings.get("ttl_seconds", 1800)), "--identifier", self.identifier, "-y"]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode:
            detail = stderr + stdout
            # A previous independent validation may have left precisely this
            # identifier loaded.  Treat it as owned for this task so its final
            # unload is still guaranteed.
            if "identifier" not in detail or "already exists" not in detail:
                raise RuntimeError(f"LM Studio VLM load failed: {stderr[-1500:] or stdout[-1500:]}")
        self.loaded_here = True

    def unload(self) -> dict[str, Any]:
        if self.loaded_here:
            subprocess.run([self.cli, "unload", self.identifier], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.loaded_here = False
        return {"nvidia_smi_after_unload": gpu_memory(self.settings.get("nvidia_smi", "nvidia-smi"))}

    def answer(self, request: dict[str, Any], frames: list[dict[str, Any]], ocr: dict[str, Any], trigger_text: str) -> tuple[dict[str, Any], float]:
        prompt = (
            "You are a conservative visual-evidence assistant. Answer in Chinese. "
            "Use only supplied frames and OCR. Do not infer hidden values or outside facts. "
            "Never create timestamps, provenance, segment IDs, or frame IDs.\n\n"
            f"trigger ASR:\n{trigger_text}\n\nrequested_information:\n{request.get('requested_information', [])}\n\n"
            f"OCR evidence:\n{ocr.get('text', '')}\n\n"
            "Task: answer only the requested visual relationship, trend, structure, connection, or comparison. "
            "For resolved, provide answer and a calibrated confidence. For unresolved_visual_reference, provide a brief reason, an empty answer, and confidence 0."
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(_image_part(frame["path"]) for frame in frames[:3])
        started = time.perf_counter()
        raw = chat_completion({"base_url": "http://127.0.0.1:1234/v1", **self.settings, "model": self.identifier},
                              [{"role": "user", "content": content}], response_format={"type": "json_schema", "json_schema": VLM_SCHEMA},
                              timeout_seconds=int(self.settings.get("timeout_seconds", 600)))
        elapsed = round(time.perf_counter() - started, 3)
        return _normalise_vlm_response(raw, "LM Studio VLM"), elapsed


class OpenAICompatibleVLMBackend:
    """Remote/OpenAI-compatible VLM. It never manages a local GPU model."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def load(self) -> None:
        return None

    def unload(self) -> dict[str, Any]:
        return {"lifecycle": "external_api_no_local_model"}

    def answer(self, request: dict[str, Any], frames: list[dict[str, Any]], ocr: dict[str, Any], trigger_text: str) -> tuple[dict[str, Any], float]:
        prompt = _vlm_prompt(request, ocr, trigger_text)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(_image_part(frame["path"]) for frame in frames[:3])
        started = time.perf_counter()
        raw = chat_completion(self.settings, [{"role": "user", "content": content}],
                              response_format={"type": "json_schema", "json_schema": VLM_SCHEMA},
                              timeout_seconds=int(self.settings.get("timeout_seconds", 600)))
        return _normalise_vlm_response(raw, "OpenAI-compatible VLM"), round(time.perf_counter() - started, 3)


def _vlm_prompt(request: dict[str, Any], ocr: dict[str, Any], trigger_text: str) -> str:
    return (
        "You are a conservative visual-evidence assistant. Answer in Chinese. "
        "Use only supplied frames and OCR. Do not infer hidden values or outside facts. "
        "Never create timestamps, provenance, segment IDs, or frame IDs.\n\n"
        f"trigger ASR:\n{trigger_text}\n\nrequested_information:\n{request.get('requested_information', [])}\n\n"
        f"OCR evidence:\n{ocr.get('text', '')}\n\n"
        "Task: answer only the requested visual relationship, trend, structure, connection, or comparison. "
        "For resolved, provide answer and a calibrated confidence. For unresolved_visual_reference, provide a brief reason, an empty answer, and confidence 0."
    )


def _normalise_vlm_response(raw: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        result = json.loads(raw["choices"][0]["message"]["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} returned invalid structured JSON") from error
    if result.get("status") == "resolved" and isinstance(result.get("answer"), str) and result["answer"].strip():
        return {"status": "resolved", "answer": result["answer"].strip(), "confidence": round(float(result.get("confidence", 0)), 3)}
    return {"status": "unresolved_visual_reference", "reason": str(result.get("reason", "visual information is insufficient"))}


def settings_model_key(settings: dict[str, Any]) -> str:
    return str(settings.get("model_key") or settings.get("model") or "qwen3-vl-8b-instruct")


def gpu_memory(nvidia_smi: str) -> str:
    result = subprocess.run([nvidia_smi, "--query-gpu=memory.used,memory.total", "--format=csv,noheader"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    stdout = result.stdout.decode("utf-8", errors="replace")
    return stdout.strip() if result.returncode == 0 else "unavailable"

