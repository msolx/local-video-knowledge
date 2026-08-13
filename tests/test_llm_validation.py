import unittest

from src.backends.llm import _validate_and_enrich, ensure_segment_ids


class KnowledgeValidationTests(unittest.TestCase):
    transcript, _ = ensure_segment_ids([{"start": 1.0, "end": 4.0, "text": "原始内容"}])

    def test_rejects_unknown_segment_id(self) -> None:
        payload = {"one_sentence_conclusion": {"content": "结论", "evidence_segment_ids": ["seg_999999"]}, "knowledge_points": [], "keywords": []}
        with self.assertRaises(ValueError):
            _validate_and_enrich(payload, self.transcript)

    def test_program_enriches_evidence_from_segment(self) -> None:
        payload = {"one_sentence_conclusion": {"content": "结论", "evidence_segment_ids": ["seg_000001"]}, "knowledge_points": [{"type": "author_claim", "title": "标题", "content": "内容", "evidence_segment_ids": ["seg_000001"]}], "keywords": []}
        result = _validate_and_enrich(payload, self.transcript)
        self.assertEqual(result["knowledge_points"][0]["evidence"][0]["start"], 1.0)
        self.assertEqual(result["knowledge_points"][0]["verification_status"], "not_checked")
        self.assertEqual(result["one_sentence_conclusion"]["summary_type"], "llm_synthesis")
