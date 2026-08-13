"""Downloader adapters. They only emit files for Intake; they never call ASR."""

from .base import DownloadResult, Downloader
from .douyin_f2 import F2DouyinDownloader

__all__ = ["DownloadResult", "Downloader", "F2DouyinDownloader"]
