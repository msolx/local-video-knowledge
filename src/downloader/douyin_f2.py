"""LEGACY PROOF-OF-CONCEPT ADAPTER - DO NOT USE IN PRODUCTION PIPELINE.

AUDIT REPORT (DY-D01):
This adapter was developed during early exploration (QW-09..QW-14) and exhibits
multiple production-unsafe patterns:
1. Argument credential leakage: Passing credentials/cookies directly in command line arguments (--cookie),
   exposing plaintext secrets in process listings (ps / Task Manager).
2. Brittle error detection: Naive substring search ("ERROR" in message) susceptible to false alarms or missed crashes.
3. Lack of sandbox isolation: Writes logs and temp outputs directly into the target output directory.
4. Unvalidated output: Returns raw files without container verification (ffprobe / decode smoke testing).
5. Direct un-isolated process execution: Runs without subprocess sandboxing or worker IPC.

Production orchestration MUST use SafeDouyinDownloader (src.downloader.safe_downloader).
"""

from __future__ import annotations

import os
import subprocess
import warnings
from pathlib import Path

from .base import DownloadResult


class F2DouyinDownloader:
    """Isolated F2 proof-of-concept adapter (DEPRECATED: Use SafeDouyinDownloader for production)."""

    def __init__(self, executable: Path | None = None, cookie: str | None = None, auto_cookie: str | None = None) -> None:
        warnings.warn(
            "F2DouyinDownloader is a legacy POC adapter and is deprecated for production. "
            "Use SafeDouyinDownloader instead.",
            category=DeprecationWarning,
            stacklevel=2,
        )
        self.executable = executable or Path("f2")
        self.cookie = cookie
        self.auto_cookie = auto_cookie

    def download(self, source_url: str, output_directory: Path) -> DownloadResult:
        output_directory.mkdir(parents=True, exist_ok=True)
        command = [str(self.executable), "dy", "--url", source_url, "--mode", "one", "--path", str(output_directory),
                   "--folderize", "true", "--music", "false", "--cover", "false", "--desc", "true"]
        if self.cookie:
            command.extend(["--cookie", self.cookie])
        elif self.auto_cookie:
            command.extend(["--auto-cookie", self.auto_cookie])
        environment = os.environ.copy()
        environment.setdefault("PYTHONUTF8", "1")
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        (output_directory / "f2.log").write_bytes(result.stdout)
        message = result.stdout.decode("utf-8", errors="replace")
        # F2 can report an application error yet return process status 0 (for
        # example browser Cookie decryption failures). Treat that as a failure
        # rather than hiding the real reason behind "no supported media".
        if result.returncode or "ERROR" in message:
            message = message[-2000:]
            raise RuntimeError(f"F2 download failed ({result.returncode}). See {output_directory / 'f2.log'}\n{message}")
        files = tuple(item for item in output_directory.rglob("*") if item.is_file() and item.name != "f2.log")
        return DownloadResult(source_url, output_directory, files, tuple(command))
