"""One-video isolated Paddle GPU OCR worker; JSON stdin/stdout protocol."""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    request = json.loads(sys.stdin.read())
    # PaddleOCR emits informational logs during construction. Keep stdout reserved
    # for this worker's machine-readable response.
    with contextlib.redirect_stdout(sys.stderr):
        from src.visual.service import PaddleOCRBackend
        backend = PaddleOCRBackend(request["config"])
        reads = {}
        for index, frame in enumerate(request["frames"]):
            texts, scores = backend.warmup(frame) if index == 0 else backend.read(frame)
            reads[frame["frame_id"]] = {"texts": texts, "scores": scores}
    print(json.dumps({"status": "completed", "reads": reads, "timing": backend.timing()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
