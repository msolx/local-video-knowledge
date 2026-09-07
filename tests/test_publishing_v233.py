import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.intake.media_probe import MediaInfo
from src.pipeline import STAGES, _stage
from src.publishing import PublishMediaError, publish_completed_media


def complete_info(path: Path) -> MediaInfo:
    stat = path.stat()
    return MediaInfo(path, "audio_video", 12.0, stat.st_size, stat.st_mtime, stat.st_ctime, "mp4",
                     {"codec_name": "h264", "width": 1280, "height": 720}, {"codec_name": "aac"})


class CompletedMediaPublishingTests(unittest.TestCase):
    def settings(self) -> dict:
        return {"completed_media_root": "./data/completed_media", "prefer_hardlink": True, "copy_fallback": True}

    def test_hardlink_publish_is_valid_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "processed" / "video" / "normalized" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"complete-av")
            data_root = root / "data"
            with patch("src.publishing.probe_media", side_effect=lambda _ffprobe, path: complete_info(path)):
                result = publish_completed_media(source, "video", data_root, self.settings(), Path("ffprobe"))
                repeat = publish_completed_media(source, "video", data_root, self.settings(), Path("ffprobe"))
            self.assertEqual("hardlink", result.publish_mode)
            self.assertFalse(result.skipped)
            self.assertTrue(result.path.samefile(source))
            self.assertEqual("completed_media/video/source.mp4", result.relative_path)
            self.assertTrue(repeat.skipped)
            self.assertEqual("hardlink", repeat.publish_mode)

    def test_copy_fallback_is_atomic_and_validated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"complete-av-copy")
            data_root = root / "data"
            with patch("src.publishing.probe_media", side_effect=lambda _ffprobe, path: complete_info(path)), patch("src.publishing.os.link", side_effect=OSError("hardlink unavailable")):
                result = publish_completed_media(source, "video", data_root, self.settings(), Path("ffprobe"))
            self.assertEqual("copy", result.publish_mode)
            self.assertEqual(source.read_bytes(), result.path.read_bytes())
            self.assertFalse(any(result.path.parent.glob("*.copying")))

    def test_existing_invalid_target_is_not_overwritten(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"complete-av")
            target = root / "data" / "completed_media" / "video" / "source.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"wrong-media")

            def probe(_ffprobe: Path, path: Path) -> MediaInfo:
                if path == target:
                    stat = path.stat()
                    return MediaInfo(path, "invalid", 0.0, stat.st_size, stat.st_mtime, stat.st_ctime, None, None, None)
                return complete_info(path)

            with patch("src.publishing.probe_media", side_effect=probe):
                with self.assertRaises(PublishMediaError):
                    publish_completed_media(source, "video", root / "data", self.settings(), Path("ffprobe"))
            self.assertEqual(b"wrong-media", target.read_bytes())

    def test_resume_after_publish_failure_runs_only_publish_stage(self) -> None:
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "processing.json"
            state = {"video_id": "video", "stages": {stage: {"status": "completed"} for stage in STAGES}, "status": "FAILED", "error": {}}
            state["stages"]["publish_media"] = {"status": "failed", "error": "permission denied"}
            calls: list[str] = []
            for stage in STAGES[:-1]:
                _stage(state, state_path, stage, False, lambda stage=stage: calls.append(stage) or {})
            _stage(state, state_path, "publish_media", False, lambda: calls.append("publish_media") or {"publish_mode": "hardlink"})
            self.assertEqual(["publish_media"], calls)
            self.assertEqual("completed", state["stages"]["publish_media"]["status"])


if __name__ == "__main__":
    unittest.main()
