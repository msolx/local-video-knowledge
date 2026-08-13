import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.backends.llm import _validate_global_merge, ensure_segment_ids
from src.knowledge.chunker import chunk_transcript
from src.knowledge.service import _attach_provenance, ensure_source_schema
from src.provenance import source_for_media, write_source_sidecar
from scripts.download_douyin import canonical_work_url
from src.downloader.douyin_f2 import F2DouyinDownloader


class KnowledgeV22Tests(unittest.TestCase):
    def test_chunks_preserve_global_ids_and_overlap(self) -> None:
        transcript, _ = ensure_segment_ids([
            {"start": index, "end": index + 1, "text": "知识抽取需要按 token 重叠切分" * 2}
            for index in range(20)
        ])
        chunks = chunk_transcript(transcript, max_input_tokens=150, overlap_tokens=40)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(segment["id"].startswith("seg_") for chunk in chunks for segment in chunk.segments))
        self.assertTrue(chunks[1].overlap_segment_ids)
        self.assertTrue(set(chunks[1].overlap_segment_ids).issubset({item["id"] for item in chunks[0].segments}))

    def test_merge_unions_evidence_and_preserves_uncited_local_item(self) -> None:
        transcript, _ = ensure_segment_ids([
            {"start": 1, "end": 2, "text": "甲"}, {"start": 3, "end": 4, "text": "乙"}, {"start": 5, "end": 6, "text": "丙"},
        ])
        local = [
            {"local_id": "chunk_001_k_001", "type": "author_claim", "title": "甲", "content": "甲", "evidence_segment_ids": ["seg_000001"]},
            {"local_id": "chunk_002_k_001", "type": "author_claim", "title": "乙", "content": "乙", "evidence_segment_ids": ["seg_000002"]},
            {"local_id": "chunk_002_k_002", "type": "author_opinion", "title": "丙", "content": "丙", "evidence_segment_ids": ["seg_000003"]},
        ]
        response = {"one_sentence_conclusion": {"content": "综合", "source_local_ids": ["chunk_001_k_001", "chunk_002_k_001"]},
                    "knowledge_points": [{"type": "author_claim", "title": "合并", "content": "合并", "source_local_ids": ["chunk_001_k_001", "chunk_002_k_001"]}], "keywords": []}
        result = _validate_global_merge(response, local, transcript)
        self.assertEqual(result["knowledge_points"][0]["evidence_segment_ids"], ["seg_000001", "seg_000002"])
        self.assertEqual(result["merge"]["preserved_unmerged_local_ids"], ["chunk_002_k_002"])
        self.assertEqual(len(result["knowledge_points"]), 2)

    def test_legacy_manual_metadata_becomes_objective_other_source(self) -> None:
        metadata, changed = ensure_source_schema({"video_id": "video", "title": "本地文件", "source": "manual", "download_time": "2026-01-01T00:00:00+00:00", "original_input_path": "fixtures/input.mp4"})
        self.assertTrue(changed)
        self.assertEqual(metadata["source"]["platform"], "other")
        self.assertEqual(metadata["source"]["original_filename"], "input.mp4")

    def test_douyin_source_is_copied_to_each_knowledge_item(self) -> None:
        knowledge = {"one_sentence_conclusion": {"content": "摘要", "evidence_segment_ids": ["seg_000001"], "evidence": []},
                     "knowledge_points": [{"id": "k_001", "type": "author_claim", "title": "标题", "content": "内容",
                                           "evidence_segment_ids": ["seg_000001"], "evidence": []}], "keywords": []}
        source = {"platform": "douyin", "source_type": "online_video", "source_url": "https://www.douyin.com/video/example",
                  "platform_content_id": "example", "author_name": "示例作者", "author_id": "author", "title": "示例",
                  "published_at": None, "collected_at": "2026-01-01T00:00:00+00:00", "original_filename": None}
        result = _attach_provenance(knowledge, "video", source)
        provenance = result["knowledge_points"][0]["provenance"][0]
        self.assertEqual(provenance["platform"], "douyin")
        self.assertEqual(provenance["source_url"], source["source_url"])

    def test_media_sidecar_preserves_douyin_provenance(self) -> None:
        with TemporaryDirectory() as temporary:
            media = Path(temporary) / "video.mp4"
            media.touch()
            write_source_sidecar(media, {"platform": "douyin", "source_type": "online_video", "source_url": "https://www.douyin.com/video/123", "platform_content_id": "123"})
            source = source_for_media(media)
            self.assertEqual(source["platform"], "douyin")
            self.assertEqual(source["platform_content_id"], "123")

    def test_douyin_modal_url_becomes_canonical_work_url(self) -> None:
        url = "https://www.douyin.com/user/self?modal_id=7671132034596694854&showTab=like"
        self.assertEqual(canonical_work_url(url), "https://www.douyin.com/video/7671132034596694854")

    def test_f2_auto_cookie_is_selected_without_raw_cookie(self) -> None:
        downloader = F2DouyinDownloader(Path("f2"), auto_cookie="edge")
        self.assertEqual(downloader.auto_cookie, "edge")
        self.assertIsNone(downloader.cookie)
