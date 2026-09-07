"""Dependency Injection Interfaces (Protocols) for the Collector Subsystem.

Defines the explicit integration boundaries for subsequent implementation tasks:
- C02: BrowserRuntimeProvider
- C03: AuthStateDetector
- C04: SourceClient
- C05: Incremental Watermark Sync Logic
- C06: RawArchiver
- C07: CanonicalTransformer
- C08: MetadataRepository
- C09: DownloadQueueProducer
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import DownloadTask, RawArchiveBatch, Watermark


@runtime_checkable
class BrowserRuntimeProvider(Protocol):
    """C02: Manages dedicated browser process and persistent profile."""
    def is_running(self) -> bool: ...
    def launch(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class AuthStateDetector(Protocol):
    """C03: Detects authentication and risk challenge states."""
    def check_auth(self) -> str: ...  # e.g. LOGIN_OK, AUTH_REQUIRED, AUTH_CHALLENGE


@runtime_checkable
class SourceClient(Protocol):
    """C04: Executes authenticated in-browser signed page requests."""
    def fetch_page(self, cursor: int, count: int) -> dict[str, Any]: ...


@runtime_checkable
class RawArchiver(Protocol):
    """C06: Persists verbatim signed JSON responses to data/raw."""
    def archive_page(
        self,
        sync_run_id: str,
        platform: str,
        page_number: int,
        request_cursor: str,
        response_cursor: str,
        raw_response: dict[str, Any],
        fetched_at: str,
    ) -> Any: ...
    def archive_batch(self, batch: RawArchiveBatch) -> Path: ...


@runtime_checkable
class CanonicalTransformer(Protocol):
    """C07: Transforms raw platform response items into canonical schemas."""
    def transform_item(self, raw_item: dict[str, Any], observed_at: str) -> dict[str, Any]: ...


@runtime_checkable
class MetadataRepository(Protocol):
    """C08: Persists watermark, collection items, observations, and sync runs."""
    def get_watermark(self, platform: str, user_id: str) -> int: ...
    def update_watermark(self, watermark: Watermark) -> None: ...
    def record_sync_run(self, sync_run_data: dict[str, Any]) -> None: ...
    def item_exists(self, platform: str, platform_content_id: str) -> bool: ...
    def save_item(self, item_data: dict[str, Any]) -> None: ...
    def save_observation(self, observation_data: dict[str, Any]) -> None: ...


@runtime_checkable
class DownloadQueueProducer(Protocol):
    """C09: Dispatches new collection items into the Downloader Queue."""
    def enqueue(self, task: DownloadTask) -> bool: ...


# =====================================================================
# Stub Implementations for Skeleton / Offline Unit Testing
# =====================================================================

class StubBrowserRuntime:
    def __init__(self, running: bool = True) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def launch(self) -> None:
        self._running = True

    def close(self) -> None:
        self._running = False


class StubAuthStateDetector:
    def __init__(self, status: str = "LOGIN_OK") -> None:
        self.status = status

    def check_auth(self) -> str:
        return self.status


class StubSourceClient:
    def __init__(self, mock_pages: list[dict[str, Any]] | None = None) -> None:
        self.mock_pages = mock_pages or []
        self._call_count = 0

    def fetch_page(self, cursor: int, count: int) -> dict[str, Any]:
        if self._call_count < len(self.mock_pages):
            page = self.mock_pages[self._call_count]
            self._call_count += 1
            return page
        return {"status_code": 0, "aweme_list": [], "cursor": 0, "has_more": 0}


class StubRawArchiver:
    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir or Path("./data/raw")
        self.archived_batches: list[RawArchiveBatch] = []

    def archive_batch(self, batch: RawArchiveBatch) -> Path:
        self.archived_batches.append(batch)
        return self.output_dir / f"page_{batch.page_index}_{batch.cursor}.json"


class StubCanonicalTransformer:
    def transform_item(self, raw_item: dict[str, Any], observed_at: str) -> dict[str, Any]:
        return {
            "schema_version": "collection-item-v1",
            "platform": "douyin",
            "platform_content_id": raw_item.get("aweme_id", "stub"),
            "canonical_url": f"https://www.douyin.com/video/{raw_item.get('aweme_id', '')}",
            "observed_at": observed_at,
        }


class StubMetadataRepository:
    def __init__(self, initial_watermark: int = 0) -> None:
        self.watermark = initial_watermark
        self.recorded_runs: list[dict[str, Any]] = []
        self.items: dict[str, dict[str, Any]] = {}
        self.observations: list[dict[str, Any]] = []

    def get_watermark(self, platform: str, user_id: str) -> int:
        return self.watermark

    def update_watermark(self, watermark: Watermark) -> None:
        self.watermark = watermark.watermark_cursor

    def record_sync_run(self, sync_run_data: dict[str, Any]) -> None:
        self.recorded_runs.append(sync_run_data)

    def item_exists(self, platform: str, platform_content_id: str) -> bool:
        return platform_content_id in self.items

    def save_item(self, item_data: dict[str, Any]) -> None:
        self.items[item_data.get("platform_content_id", "")] = item_data

    def save_observation(self, observation_data: dict[str, Any]) -> None:
        self.observations.append(observation_data)


class StubDownloadQueueProducer:
    def __init__(self) -> None:
        self.enqueued_tasks: list[DownloadTask] = []

    def enqueue(self, task: DownloadTask) -> bool:
        self.enqueued_tasks.append(task)
        return True
