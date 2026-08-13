import unittest

from src.render import evidence_text, render_markdown, timestamp


class RenderTests(unittest.TestCase):
    def test_timestamp(self) -> None:
        self.assertEqual(timestamp(201.4), "00:03:21")

    def test_markdown_keeps_evidence(self) -> None:
        metadata = {"video_id": "abc", "duration": 20, "title": "测试"}
        knowledge = {
            "one_sentence_conclusion": {"summary_type": "llm_synthesis", "content": "结论", "evidence_segment_ids": ["seg_000001"], "evidence": [{"segment_id": "seg_000001", "start": 2, "end": 5, "text": "原文"}]},
            "knowledge_points": [{"id": "k_001", "type": "author_claim", "verification_status": "not_checked", "title": "要点", "content": "细节", "evidence_segment_ids": ["seg_000001"], "evidence": [{"segment_id": "seg_000001", "start": 2, "end": 5, "text": "原文"}]}],
            "keywords": ["测试"],
        }
        rendered = render_markdown(metadata, knowledge)
        self.assertIn("00:00:02–00:00:05", rendered)
        self.assertIn("video_id: abc", rendered)
        self.assertIn("模型综合摘要", rendered)

    def test_evidence_groups_nearby_segments(self) -> None:
        evidence = [
            {"segment_id": "seg_000001", "start": 234.2, "end": 235.7},
            {"segment_id": "seg_000002", "start": 241.1, "end": 242.5},
            {"segment_id": "seg_000003", "start": 262.0, "end": 263.9},
        ]
        rendered = evidence_text(evidence)
        self.assertIn("00:03:54–00:04:02 (seg_000001, seg_000002)", rendered)
        self.assertIn("00:04:22–00:04:24 (seg_000003)", rendered)


if __name__ == "__main__":
    unittest.main()
