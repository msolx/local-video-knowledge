"""Run a simulated long-transcript Knowledge v2.2 POC; not a content-quality benchmark."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.knowledge import build_knowledge
from src.knowledge.lifecycle import unload_lm_studio
from src.render import render_markdown
from src.storage import atomic_write_json, atomic_write_text, utc_now


def main() -> None:
    config = load_config(ROOT / "config" / "config.json")
    source_id = "1a397e03df8c9b95"
    original = json.loads((config.data_root / "processed" / source_id / "transcript.json").read_text(encoding="utf-8"))["segments"]
    duration = original[-1]["end"]
    transcript = []
    for copy_index in range(3):
        for segment in original:
            transcript.append({"id": f"seg_{len(transcript) + 1:06d}", "start": round(segment["start"] + copy_index * duration, 3),
                               "end": round(segment["end"] + copy_index * duration, 3), "text": segment["text"]})
    work_dir = config.data_root / "test_runs" / "knowledge_v2_2_long_transcript_poc"
    metadata = {
        "video_id": "knowledge_v2_2_long_transcript_poc", "title": "模拟长 transcript POC（重复内容，仅验证分块链路）",
        "duration": round(transcript[-1]["end"], 3),
        "source": {"platform": "other", "source_type": "synthetic_test", "source_url": None, "platform_content_id": None,
                   "author_name": None, "author_id": None, "title": "模拟长 transcript POC", "published_at": None,
                   "collected_at": utc_now(), "original_filename": None},
    }
    generated, provenance = build_knowledge(metadata, transcript, config.raw["llm"], config.raw["knowledge"], work_dir)
    document = {"schema_version": provenance["schema_version"], "video_id": metadata["video_id"], "generated_at": utc_now(),
                "provenance": provenance, "knowledge": generated, "test_note": "Synthetic repeated transcript; validates chunking/merge mechanics only."}
    atomic_write_json(work_dir / "knowledge.json", document)
    atomic_write_text(work_dir / "knowledge.md", render_markdown(metadata, generated))
    lifecycle = unload_lm_studio(config.raw["llm"], config.raw["knowledge"]["lifecycle"])
    atomic_write_json(work_dir / "lifecycle.json", lifecycle)
    print(json.dumps({"work_dir": str(work_dir), "execution": provenance["execution"], "lifecycle": lifecycle}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
