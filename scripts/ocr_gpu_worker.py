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
            frame_id = frame["frame_id"]
            try:
                if hasattr(backend, "read_detail"):
                    detail = backend.read_detail(frame)
                    if index == 0 and request.get("warmup", True):
                        backend.warmup_seconds = backend.inference_seconds
                    reads[frame_id] = {"status": "completed", **detail}
                else:
                    texts, scores = backend.warmup(frame) if index == 0 else backend.read(frame)
                    reads[frame_id] = {"status": "completed", "texts": texts, "scores": scores, "polygons": [], "boxes": []}
            except Exception as err:
                reads[frame_id] = {
                    "status": "failed",
                    "error": f"{type(err).__name__}: {err}",
                    "texts": [],
                    "scores": [],
                    "polygons": [],
                    "boxes": [],
                }
    print(json.dumps({"status": "completed", "reads": reads, "timing": backend.timing()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
