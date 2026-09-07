"""Platform-agnostic Base Collector abstraction and execution result models.

Generic core containing no Douyin-specific terms (e.g. aweme_id, listcollection, a_bogus).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .config import CollectorConfig


class CollectorMode(str, Enum):
    PROBE = "probe"
    SYNC = "sync"
    BACKFILL = "backfill"


class CollectorStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CollectorRunResult:
    """Standardized structured output contract for all collector operations."""
    run_id: str
    platform: str
    mode: CollectorMode
    status: CollectorStatus
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = field(default_factory=utcnow_iso)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "platform": self.platform,
            "mode": self.mode.value,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metrics": self.metrics,
            "error": self.error,
        }


class BaseCollector(ABC):
    """Abstract base class for all platform collection adapters."""

    def __init__(self, config: CollectorConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def platform(self) -> str:
        """Name of the platform, e.g. 'douyin', 'bilibili', 'youtube'."""
        ...

    def initialize(self, run_id: str) -> None:
        """Hook called before any collection operation to initialize dependencies."""
        pass

    def preflight_auth_check(self, run_id: str) -> bool:
        """Hook to verify authentication status prior to initiating sync."""
        return True

    @abstractmethod
    def probe(self, run_id: str) -> CollectorRunResult:
        """Lightweight health and authentication inspection."""
        ...

    @abstractmethod
    def sync(self, run_id: str) -> CollectorRunResult:
        """Incremental synchronization starting from newest items down to watermark."""
        ...

    @abstractmethod
    def backfill(self, run_id: str, limit: int | None = None) -> CollectorRunResult:
        """Historical backfill synchronization traversing pagination to boundary."""
        ...

    def shutdown(self, run_id: str) -> None:
        """Hook called after collection operation finishes to clean up resources."""
        pass
