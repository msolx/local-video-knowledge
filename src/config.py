from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    path: Path

    @property
    def data_root(self) -> Path:
        value = Path(self.raw["paths"]["data_root"])
        return value if value.is_absolute() else (self.path.parent.parent / value).resolve()

    @property
    def ffmpeg(self) -> Path:
        value = Path(self.raw["paths"]["ffmpeg"])
        if value.is_absolute():
            return value
        # A portable config can simply use "ffmpeg" when it is on PATH.
        discovered = shutil.which(str(value))
        if discovered:
            return Path(discovered)
        return (self.path.parent.parent / value).resolve() if value.parent != Path(".") else value

    @property
    def ffprobe(self) -> Path:
        candidate = self.ffmpeg.with_name("ffprobe.exe")
        return candidate if candidate.exists() else self.ffmpeg.with_name("ffprobe")

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    for key in ("paths", "pipeline", "asr", "llm"):
        if key not in raw:
            raise ValueError(f"Configuration is missing '{key}'.")
    return AppConfig(raw=raw, path=config_path)
