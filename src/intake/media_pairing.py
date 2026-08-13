from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .media_probe import MediaInfo


@dataclass(frozen=True)
class PairCandidate:
    audio: MediaInfo
    duration_delta_seconds: float
    modified_delta_seconds: float
    filename_similarity: float
    score: float

    def as_dict(self) -> dict[str, object]:
        return {
            "audio_path": str(self.audio.path), "duration_delta_seconds": self.duration_delta_seconds,
            "modified_delta_seconds": self.modified_delta_seconds, "filename_similarity": self.filename_similarity,
            "score": self.score,
        }


@dataclass(frozen=True)
class PairDecision:
    video: MediaInfo
    status: str
    selected: PairCandidate | None
    candidates: tuple[PairCandidate, ...]


def _filename_key(name: str) -> str:
    # IDM adds "_2", "_3"... while downloading separate streams; that suffix is not identity.
    stem = re.sub(r"(?:[_\-\s]+\d+)$", "", name.rsplit(".", 1)[0].lower())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", stem)


def _candidate(video: MediaInfo, audio: MediaInfo, modified_window_seconds: float) -> PairCandidate | None:
    tolerance = max(1.0, video.duration * 0.002)
    duration_delta = abs(video.duration - audio.duration)
    if duration_delta > tolerance:
        return None
    modified_delta = abs(video.modified_at - audio.modified_at)
    filename_similarity = SequenceMatcher(None, _filename_key(video.path.name), _filename_key(audio.path.name)).ratio()
    duration_score = 1.0 - (duration_delta / tolerance)
    time_score = max(0.0, 1.0 - modified_delta / modified_window_seconds)
    score = 0.50 * duration_score + 0.35 * time_score + 0.15 * filename_similarity
    return PairCandidate(audio, round(duration_delta, 3), round(modified_delta, 3), round(filename_similarity, 3), round(score, 5))


def pair_streams(infos: list[MediaInfo], modified_window_seconds: float = 900.0, ambiguity_margin: float = 0.04) -> list[PairDecision]:
    audios = [info for info in infos if info.media_type == "audio_only"]
    decisions: list[PairDecision] = []
    for video in (info for info in infos if info.media_type == "video_only"):
        candidates = [candidate for audio in audios if (candidate := _candidate(video, audio, modified_window_seconds))]
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        if not candidates:
            decisions.append(PairDecision(video, "missing_audio", None, ()))
        elif len(candidates) > 1 and candidates[0].score - candidates[1].score <= ambiguity_margin:
            decisions.append(PairDecision(video, "pair_ambiguous", None, tuple(candidates)))
        else:
            decisions.append(PairDecision(video, "pair_found", candidates[0], tuple(candidates)))
    return decisions
