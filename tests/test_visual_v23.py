import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.knowledge.service import _attach_visual_evidence
from src.visual.service import build_visual_evidence, detect_requests, visual_pipeline_fingerprint


class VisualV23Tests(unittest.TestCase):
    def test_detects_visual_reference_with_program_owned_window(self):
        transcript = [
            {"id": "seg_000001", "start": 10.0, "end": 12.0, "text": "直接看这张图"},
            {"id": "seg_000002", "start": 12.1, "end": 14.0, "text": "这里显示显卡配置"},
        ]
        requests = detect_requests(transcript, {"pre_roll_seconds": 3, "post_roll_seconds": 4})
        self.assertEqual(2, len(requests))
        self.assertEqual(["seg_000001", "seg_000002"], requests[0]["trigger_segment_ids"])
        self.assertEqual(7.0, requests[0]["start"])
        self.assertEqual(18.0, requests[0]["end"])

    def test_visual_fingerprint_changes_with_visual_settings(self):
        transcript = [{"id": "seg_000001", "start": 0, "end": 1, "text": "如图"}]
        self.assertNotEqual(
            visual_pipeline_fingerprint(transcript, {"sample_interval_seconds": 1}),
            visual_pipeline_fingerprint(transcript, {"sample_interval_seconds": 2}),
        )

    def test_hydrates_completed_visual_only_and_keeps_audio(self):
        knowledge = {"one_sentence_conclusion": {"evidence_segment_ids": ["seg_000001"], "evidence": [{"segment_id": "seg_000001", "start": 1, "end": 2, "text": "x"}], "visual_evidence_ids": ["ve_001", "ve_002"]}, "knowledge_points": []}
        visual = [
            {"id": "ve_001", "status": "completed", "source_type": "visual_ocr", "frame_ids": ["frame_1"], "start": 1, "end": 2, "text": "按钮"},
            {"id": "ve_002", "status": "unresolved_visual_reference", "start": 3, "end": 4},
        ]
        hydrated = _attach_visual_evidence(knowledge, visual)["one_sentence_conclusion"]
        self.assertEqual(["ve_001"], hydrated["visual_evidence_ids"])
        self.assertEqual(["audio_asr", "visual_ocr"], [item["source_type"] for item in hydrated["unified_evidence"]])

    def test_insufficient_ocr_becomes_explicit_unresolved_without_vlm(self):
        class FakeOCR:
            name = "fake"

            def __init__(self, _config):
                pass

            def read(self, _frame):
                return [], []

            def warmup(self, _frame):
                return [], []

            def timing(self):
                return {"backend": self.name, "initialization_seconds": 0, "warmup_seconds": 0, "inference_seconds": 0, "inference_calls": 0}

        request = {"id": "vr_001", "trigger_segment_ids": ["seg_000001"], "start": 1.0, "end": 2.0, "reason": "screen_reference", "requested_information": ["value"], "status": "pending"}
        frame = {"frame_id": "frame_000001000", "timestamp": 1.0, "path": "unused.jpg"}
        with TemporaryDirectory() as directory, patch("src.visual.service.detect_requests", return_value=[request]), patch("src.visual.service.extract_keyframes", return_value=[frame]), patch("src.visual.service.PaddleOCRBackend", FakeOCR):
            result = build_visual_evidence(Path("unused.mp4"), [{"id": "seg_000001", "start": 1, "end": 2, "text": "如图"}], Path("ffmpeg"), Path(directory), {"ocr": {}, "vlm": {"backend": "disabled"}})
        item = result["visual_evidence"][0]
        self.assertEqual("unresolved_visual_reference", item["status"])
        self.assertEqual("visual_vlm", item["source_type"])


if __name__ == "__main__":
    unittest.main()
