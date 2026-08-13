from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path


_DLL_DIRECTORY_HANDLES: list[object] = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--cuda-runtime-dir")
    arguments = parser.parse_args()
    settings = json.loads(arguments.settings)
    if arguments.cuda_runtime_dir:
        runtime = Path(arguments.cuda_runtime_dir)
        if not runtime.is_dir():
            raise RuntimeError(f"CUDA runtime directory does not exist: {runtime}")
        # Keep the handle alive: its destructor removes the directory from the DLL search path.
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(runtime)))
        os.environ["PATH"] = f"{runtime}{os.pathsep}{os.environ.get('PATH', '')}"
    peak_vram_mib = 0
    monitoring = threading.Event()

    def monitor_gpu() -> None:
        nonlocal peak_vram_mib
        while not monitoring.is_set():
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                value = line.strip().split()[0]
                if value.isdigit():
                    peak_vram_mib = max(peak_vram_mib, int(value))
            time.sleep(0.25)

    thread = threading.Thread(target=monitor_gpu, daemon=True)
    thread.start()
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    try:
        model = WhisperModel(settings["model"], device=settings.get("device", "cuda"), compute_type=settings.get("compute_type", "float16"))
        segments, _info = model.transcribe(arguments.audio, language=settings.get("language") or None, vad_filter=bool(settings.get("vad_filter", True)))
        payload = [{"start": segment.start, "end": segment.end, "text": segment.text} for segment in segments]
    finally:
        monitoring.set()
        thread.join(timeout=1)
    arguments.output.write_text(json.dumps({"segments": payload, "metrics": {"peak_vram_mib": peak_vram_mib, "transcription_seconds": round(time.perf_counter() - started, 3)}}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
