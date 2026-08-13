from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..storage import atomic_write_json, load_json, utc_now


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def ensure_lm_studio_loaded(llm_config: dict[str, Any], lifecycle_config: dict[str, Any]) -> dict[str, Any]:
    """Load the configured model only when Knowledge is about to start.

    ASR runs before this call, so the 27B model never competes with Whisper for
    RTX 4090 VRAM. Batch mode keeps the loaded model until its configured unload.
    """
    settings = llm_config.get("lm_studio", {})
    cli = lifecycle_config.get("lm_studio_cli", "lms")
    identifier = settings.get("model")
    result: dict[str, Any] = {"attempted_at": utc_now(), "model": identifier, "status": "not_applicable"}
    if llm_config.get("backend") != "lm_studio":
        return result
    try:
        current = subprocess.run([str(cli), "ps"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if current.returncode == 0 and str(identifier) in _decode(current.stdout):
            return {**result, "status": "already_loaded"}
        model_key = lifecycle_config.get("model_key")
        if not model_key:
            raise ValueError("knowledge.lifecycle.model_key is required to load the configured LM Studio model.")
        command = [str(cli), "load", str(model_key), "--gpu", str(lifecycle_config.get("gpu", "max")),
                   "--context-length", str(lifecycle_config.get("context_length", 32768)),
                   "--identifier", str(identifier), "--ttl", str(lifecycle_config.get("ttl_seconds", 3600)), "--yes"]
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        detail = {**result, "command": command, "returncode": process.returncode, "stdout": _decode(process.stdout), "stderr": _decode(process.stderr),
                  "status": "loaded" if process.returncode == 0 else "load_failed"}
        if process.returncode:
            raise RuntimeError(f"LM Studio model load failed: {detail['stderr'] or detail['stdout']}")
        return detail
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"Could not prepare LM Studio model for Knowledge: {type(error).__name__}: {error}") from error


def unload_lm_studio(llm_config: dict[str, Any], lifecycle_config: dict[str, Any]) -> dict[str, Any]:
    """Unload the configured LM Studio model and report post-unload VRAM usage."""
    settings = llm_config.get("lm_studio", {})
    cli = lifecycle_config.get("lm_studio_cli", "lms")
    model = settings.get("model")
    result: dict[str, Any] = {"attempted_at": utc_now(), "policy": lifecycle_config.get("unload_policy", "after_task"),
                              "model": model, "status": "not_applicable"}
    if llm_config.get("backend") != "lm_studio":
        return result
    command = [str(cli), "unload", str(model)]
    try:
        loaded = subprocess.run([str(cli), "ps"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if loaded.returncode == 0 and str(model) not in _decode(loaded.stdout):
            result.update({"status": "already_unloaded", "model_check": "not listed by lms ps"})
        else:
            process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            result.update({"command": command, "returncode": process.returncode, "stdout": _decode(process.stdout), "stderr": _decode(process.stderr),
                           "status": "unloaded" if process.returncode == 0 else "unload_failed"})
    except (OSError, subprocess.SubprocessError) as error:
        result.update({"status": "unload_failed", "error": f"{type(error).__name__}: {error}"})
    if lifecycle_config.get("verify_nvidia_smi", True):
        try:
            probe = subprocess.run([str(lifecycle_config.get("nvidia_smi", "nvidia-smi")), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            values = [int(line.strip()) for line in _decode(probe.stdout).splitlines() if line.strip().isdigit()]
            result["gpu_memory_after_unload_mib"] = values
            result["nvidia_smi_returncode"] = probe.returncode
            if probe.returncode:
                result["nvidia_smi_stderr"] = _decode(probe.stderr)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            result["nvidia_smi_error"] = f"{type(error).__name__}: {error}"
    return result


def record_lifecycle(video_dir: Path, detail: dict[str, Any]) -> None:
    path = video_dir / "processing.json"
    state = load_json(path, {})
    history = state.setdefault("llm_lifecycle", [])
    history.append(detail)
    atomic_write_json(path, state)
