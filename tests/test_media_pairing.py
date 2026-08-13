import unittest
from pathlib import Path

from src.intake.media_pairing import pair_streams
from src.intake.media_probe import MediaInfo


def media(name: str, kind: str, duration: float, modified: float) -> MediaInfo:
    return MediaInfo(Path(name), kind, duration, 1, modified, modified, "mp4", {"codec_name": "h264"} if kind != "audio_only" else None,
                     {"codec_name": "aac"} if kind != "video_only" else None)


class MediaPairingTests(unittest.TestCase):
    def test_pairs_matching_duration_and_time(self) -> None:
        decisions = pair_streams([media("video.mp4", "video_only", 60, 100), media("audio_2.mp4", "audio_only", 60.08, 105)])
        self.assertEqual(decisions[0].status, "pair_found")
        self.assertEqual(decisions[0].selected.audio.path.name, "audio_2.mp4")

    def test_marks_equally_plausible_candidates_ambiguous(self) -> None:
        decisions = pair_streams([
            media("video.mp4", "video_only", 60, 100), media("a.mp4", "audio_only", 60, 105), media("b.mp4", "audio_only", 60, 105),
        ])
        self.assertEqual(decisions[0].status, "pair_ambiguous")

    def test_marks_missing_audio_without_failure(self) -> None:
        decisions = pair_streams([media("video.mp4", "video_only", 60, 100)])
        self.assertEqual(decisions[0].status, "missing_audio")
