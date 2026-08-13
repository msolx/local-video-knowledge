from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import DownloadResult


class F2DouyinDownloader:
    """Isolated F2 proof-of-concept adapter; not wired into the automatic pipeline."""

    def __init__(self, executable: Path | None = None, cookie: str | None = None, auto_cookie: str | None = None) -> None:
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
