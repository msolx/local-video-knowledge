"""Download one permitted Douyin URL with F2 and stage it with source provenance."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.downloader.douyin_f2 import F2DouyinDownloader
from src.provenance import write_source_sidecar
from src.storage import utc_now


MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def content_id_from_url(url: str) -> str | None:
    match = re.search(r"/(?:video|note)/(\d+)", url)
    if match:
        return match.group(1)
    return (parse_qs(urlparse(url).query).get("modal_id") or [None])[0]


def canonical_work_url(url: str) -> str:
    """F2 mode=one needs a work URL, not a user-page modal URL."""
    work_id = content_id_from_url(url)
    return f"https://www.douyin.com/video/{work_id}" if work_id else url


def unique_destination(directory: Path, source: Path) -> Path:
    candidate = directory / source.name
    index = 2
    while candidate.exists():
        candidate = directory / f"{source.stem}_{index}{source.suffix}"
        index += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Download one Douyin video with F2 and stage provenance-aware input.")
    parser.add_argument("--url", required=True, help="A Douyin share/work URL you are permitted to download.")
    cookie_group = parser.add_mutually_exclusive_group()
    cookie_group.add_argument("--cookie", help="Optional browser Cookie; do not store it in project files.")
    cookie_group.add_argument("--auto-cookie", choices=("edge", "chrome"), help="Read the current Douyin login Cookie from a closed local browser.")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.json"))
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    staging = config.data_root / "downloads" / "douyin" / utc_now().replace(":", "-")
    executable = ROOT / ".venv-f2" / "Scripts" / "f2.exe"
    if not executable.is_file():
        raise SystemExit("F2 is not installed. Run .\\scripts\\setup_f2_poc.ps1 first.")
    source_url = canonical_work_url(arguments.url)
    if source_url != arguments.url:
        print(f"Detected modal_id; using canonical work URL: {source_url}")
    result = F2DouyinDownloader(executable, arguments.cookie, arguments.auto_cookie).download(source_url, staging)
    incoming = config.data_root / "incoming" / "manual"
    incoming.mkdir(parents=True, exist_ok=True)
    source_base = {
        "platform": "douyin", "source_type": "online_video", "source_url": source_url,
        "platform_content_id": content_id_from_url(source_url), "author_name": None, "author_id": None,
        "title": None, "published_at": None, "collected_at": utc_now(), "original_filename": None,
    }
    staged: list[Path] = []
    for downloaded in result.files:
        if downloaded.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        destination = unique_destination(incoming, downloaded)
        shutil.copy2(downloaded, destination)
        source = {**source_base, "original_filename": downloaded.name}
        write_source_sidecar(destination, source)
        staged.append(destination)
    if not staged:
        raise SystemExit(f"F2 completed but no supported media was found. Inspect {staging / 'f2.log'}.")
    print("Staged provenance-aware input:")
    for item in staged:
        print(item)
    print("Next: .\\.venv\\Scripts\\python.exe main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
