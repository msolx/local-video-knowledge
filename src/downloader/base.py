from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DownloadResult:
    source_url: str
    output_directory: Path
    files: tuple[Path, ...]
    command: tuple[str, ...]


class Downloader(Protocol):
    def download(self, source_url: str, output_directory: Path) -> DownloadResult:
        """Download media only. Caller passes result.files to intake.prepare_assets()."""
