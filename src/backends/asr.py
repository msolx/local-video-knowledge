from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _normalise_segments(value: Any) -> list[dict[str, Any]]:
    """Accept the small common subset emitted by command-line ASR wrappers."""
    raw_segments = value.get("segments", value) if isinstance(value, dict) else value
    if not isinstance(raw_segments, list):
        raise ValueError("ASR output must be a JSON list or an object containing 'segments'.")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw_segments, start=1):
        if not isinstance(item, dict) or not {"start", "end", "text"}.issubset(item):
            raise ValueError(f"ASR segment {index} must contain start, end, and text.")
        start, end, text = float(item["start"]), float(item["end"]), str(item["text"]).strip()
        if end < start or not text:
            raise ValueError(f"ASR segment {index} has an invalid range or empty text.")
        result.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    if not result:
        raise ValueError("ASR returned no speech segments.")
    return result


def _faster_whisper(audio_path: Path, settings: dict[str, Any], cuda_runtime_dir: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # CTranslate2 is native code. Isolating it means a DLL/access-violation failure is recorded
    # as a resumable ASR-stage error instead of terminating the whole durable pipeline.
    worker_output = audio_path.with_suffix(".faster-whisper.json")
    worker_output.unlink(missing_ok=True)
    command = [sys.executable, "-m", "src.backends.faster_whisper_worker", "--audio", str(audio_path),
               "--output", str(worker_output), "--settings", json.dumps(settings)]
    if cuda_runtime_dir:
        command.extend(["--cuda-runtime-dir", cuda_runtime_dir])
    environment = os.environ.copy()
    if cuda_runtime_dir:
        environment["PATH"] = f"{cuda_runtime_dir}{os.pathsep}{environment.get('PATH', '')}"
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"faster-whisper worker failed with exit code {result.returncode}.\n{error}")
    if not worker_output.exists():
        raise RuntimeError("faster-whisper worker returned without writing transcript JSON.")
    try:
        payload = json.loads(worker_output.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return _normalise_segments(payload["segments"]), payload.get("metrics", {})
        return _normalise_segments(payload), {}
    finally:
        worker_output.unlink(missing_ok=True)


def _command(audio_path: Path, output_path: Path, settings: dict[str, Any]) -> list[dict[str, Any]]:
    template = settings.get("command_template", [])
    if not template:
        raise RuntimeError(
            "The command ASR backend has no command_template. Configure a Qwen3-ASR wrapper that writes JSON "
            "to {output_path}."
        )
    command = [str(piece).format(audio_path=str(audio_path), output_path=str(output_path)) for piece in template]
    subprocess.run(command, check=True)
    if not output_path.exists():
        raise RuntimeError("The ASR command completed without writing its requested output JSON.")
    return _normalise_segments(json.loads(output_path.read_text(encoding="utf-8")))


def transcribe(audio_path: Path, asr_config: dict[str, Any], temporary_output: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    backend = asr_config.get("backend")
    if backend == "faster_whisper":
        settings = asr_config["faster_whisper"]
        segments, metrics = _faster_whisper(audio_path, settings, asr_config.get("cuda_runtime_dir"))
        return segments, {"backend": backend, "model": settings["model"], "settings": settings, "metrics": metrics}
    if backend == "command":
        settings = asr_config["command"]
        return _command(audio_path, temporary_output, settings), {
            "backend": backend, "model": "external-command", "settings": {"command_template": settings.get("command_template", [])}
        }
    raise ValueError(f"Unsupported ASR backend: {backend!r}")
