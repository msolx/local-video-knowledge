"""Data boundary entities for the collector subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RawArchiveBatch:
    """Represents a persisted raw API response page."""
    run_id: str
    page_index: int
    cursor: int
    item_count: int
    raw_payload: dict[str, Any]
    archived_at: str = field(default_factory=utcnow_iso)


from .download_models import DownloadTask


@dataclass
class Watermark:
    """Represents the incremental collection watermark for a user account."""
    platform: str
    user_id: str
    watermark_cursor: int
    last_updated_at: str = field(default_factory=utcnow_iso)
