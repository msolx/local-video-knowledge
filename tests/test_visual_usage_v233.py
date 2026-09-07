import unittest

from src.knowledge.service import visual_usage_summary
from src.render import render_markdown


def completed_evidence(identifier: str, source_type: str) -> dict:
    return {"id": identifier, "source_type": source_type, "status": "completed", "start": 10, "end": 12,
            "frame_ids": ["frame_001"], "text": "screen evidence"}


class VisualUsageV233Tests(unittest.TestCase):
    def test_ocr_only_summary_and_markdown_label(self) -> None:
        knowledge = {
            "one_sentence_conclusion": {"content": "summary", "evidence": [], "visual_evidence_ids": []},
            "knowledge_points": [{"id": "k_001", "type": "author_claim", "title": "OCR point", "content": "content", "evidence": [],
                                  "visual_evidence_ids": ["ve_001"], "visual_evidence": [completed_evidence("ve_001", "visual_ocr")] }],
            "keywords": [],
        }
        visual = {"visual_evidence": [completed_evidence("ve_001", "visual_ocr")], "timing": {"vlm": {"calls": 0}}}
        knowledge["visual_usage"] = visual_usage_summary(knowledge, visual)
        rendered = render_markdown({"video_id": "video", "title": "test", "duration": 12}, knowledge)
        self.assertEqual(["k_001"], knowledge["visual_usage"]["ocr"]["used_by_knowledge_ids"])
        self.assertFalse(knowledge["visual_usage"]["vlm"]["invoked"])
        self.assertIn("OCR：已使用", rendered)
        self.assertIn("VLM：未使用", rendered)
        self.assertIn("[OCR] 00:00:10–00:00:12 [ve_001]", rendered)

    def test_ocr_and_vlm_summary_tracks_only_final_point_usage(self) -> None:
        ocr, vlm = completed_evidence("ve_001", "visual_ocr"), completed_evidence("ve_vlm_001", "visual_vlm")
        knowledge = {
            "one_sentence_conclusion": {"content": "summary", "evidence": [], "visual_evidence_ids": []},
            "knowledge_points": [
                {"id": "k_001", "visual_evidence_ids": []},
                {"id": "k_002", "visual_evidence_ids": ["ve_001"]},
                {"id": "k_003", "visual_evidence_ids": ["ve_001", "ve_vlm_001"]},
            ],
        }
        summary = visual_usage_summary(knowledge, {"visual_evidence": [ocr, vlm], "timing": {"vlm": {"calls": 1}}})
        self.assertEqual(["k_002", "k_003"], summary["ocr"]["used_by_knowledge_ids"])
        self.assertEqual(["k_003"], summary["vlm"]["used_by_knowledge_ids"])
        self.assertEqual(1, summary["vlm"]["call_count"])

    def test_ocr_execution_is_distinct_from_knowledge_usage(self) -> None:
        knowledge = {"knowledge_points": [{"id": "k_001", "visual_evidence_ids": []}]}
        summary = visual_usage_summary(knowledge, {"visual_evidence": [completed_evidence("ve_001", "visual_ocr")], "timing": {"vlm": {"calls": 0}}})
        self.assertTrue(summary["ocr"]["executed"])
        self.assertEqual([], summary["ocr"]["used_by_knowledge_ids"])


if __name__ == "__main__":
    unittest.main()
