from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config


def report(name: str, ok: bool, detail: str) -> bool:
    print(f"{'OK ' if ok else 'FAIL'} {name}: {detail}")
    return ok


def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/config.json")
    config = load_config(config_path)
    results = []
    results.append(report("Python", sys.version_info >= (3, 10), sys.version.split()[0]))
    results.append(report("FFmpeg", config.ffmpeg.exists(), str(config.ffmpeg)))
    results.append(report("Metadata probe", config.ffprobe.exists() or config.ffmpeg.exists(), str(config.ffprobe) if config.ffprobe.exists() else "FFmpeg fallback"))
    results.append(report("Data root", config.data_root.is_absolute(), str(config.data_root)))
    if config.raw["llm"].get("backend") == "ollama":
        results.append(report("Ollama command", shutil.which("ollama") is not None, shutil.which("ollama") or "not on PATH"))
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], text=True, stderr=subprocess.STDOUT
        ).strip()
        results.append(report("NVIDIA GPU", bool(output), output))
    except (OSError, subprocess.CalledProcessError) as error:
        results.append(report("NVIDIA GPU", False, str(error)))
    if config.raw["asr"]["backend"] == "faster_whisper":
        try:
            import faster_whisper  # noqa: F401
            results.append(report("faster-whisper", True, config.raw["asr"]["faster_whisper"]["model"]))
        except ImportError:
            results.append(report("faster-whisper", False, "Run scripts\\setup.ps1"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
