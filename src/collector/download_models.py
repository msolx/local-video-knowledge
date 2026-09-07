"""DownloadTask Contract and AssetStateProvider Boundary Models (DY-C09).

Defines:
1. DownloadTask contract (immutable, deterministic, credential-free).
2. DownloadTaskStatus, DownloadPriority, DownloadReason enums.
3. Deterministic task_id computation (idempotency key).
4. AssetStateProvider protocol & concrete implementations.
5. OutboxRecord data model.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def utcnow_iso() -> str:
    """Returns the current UTC timestamp formatted as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _deterministic_json(data: Any) -> str:
    """Serializes data to deterministic canonical JSON with sorted keys and no whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DownloadTaskStatus(str, enum.Enum):
    """Lifecycle delivery states of a download intent in the collector outbox."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DownloadPriority(int, enum.Enum):
    """Execution priority levels for downloader scheduling."""

    NEW_COLLECTION = 10
    REPAIR = 5
    BACKFILL = 1


class DownloadReason(str, enum.Enum):
    """Reason code explaining why this download task was generated."""

    NEW_COLLECTION_ITEM = "NEW_COLLECTION_ITEM"
    REAPPEARANCE_ASSET_MISSING = "REAPPEARANCE_ASSET_MISSING"
    ASSET_MISSING_REPAIR = "ASSET_MISSING_REPAIR"
    MANUAL_REPAIR = "MANUAL_REPAIR"
    BACKFILL_INGESTION = "BACKFILL_INGESTION"


def compute_download_task_id(
    platform: str,
    scope_id: str,
    platform_content_id: str,
    content_type: str,
    acquisition_version: str = "v1",
) -> str:
    """Computes a deterministic, collision-resistant task_id (idempotency key).

    Invariant:
    - Independent of run_id or timestamps.
    - Two observations of the same item in the same scope produce the identical task_id.
    """
    raw = f"{platform}|{scope_id}|{platform_content_id}|{content_type}|{acquisition_version}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"dl_{platform}_{platform_content_id}_{digest}"


@dataclass(frozen=True)
class DownloadTask:
    """Production DownloadTask Contract handed off from Collector (E3) to Downloader (E4).

    Security Invariant:
    - Strictly prohibited from containing Cookie, sessionid, sid_guard, a_bogus, msToken.
    - download_input is explicitly marked as volatile acquisition hint.
    """

    task_id: str
    platform: str
    scope_id: str
    platform_content_id: str
    content_type: str
    source_url: str
    download_input: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "download-task-v1"
    canonical_item_version: str = "collection-item-v1"
    collection_sync_run_id: str = ""
    created_at: str = field(default_factory=utcnow_iso)
    priority: int = DownloadPriority.NEW_COLLECTION.value
    reason: str = DownloadReason.NEW_COLLECTION_ITEM.value
    attempt_policy: dict[str, Any] = field(default_factory=dict)
    status: str = DownloadTaskStatus.PENDING.value
    metadata_ref: dict[str, Any] = field(default_factory=dict)

    @property
    def enqueued_at(self) -> str:
        """Backward compatibility alias for created_at."""
        return self.created_at

    def to_dict(self) -> dict[str, Any]:
        """Serializes contract to a pure dictionary."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "platform": self.platform,
            "scope_id": self.scope_id,
            "platform_content_id": self.platform_content_id,
            "content_type": self.content_type,
            "source_url": self.source_url,
            "download_input": self.download_input,
            "canonical_item_version": self.canonical_item_version,
            "collection_sync_run_id": self.collection_sync_run_id,
            "created_at": self.created_at,
            "priority": self.priority,
            "reason": self.reason,
            "attempt_policy": self.attempt_policy,
            "status": self.status,
            "metadata_ref": self.metadata_ref,
        }

    def to_deterministic_json(self) -> str:
        """Serializes contract to deterministic JSON string."""
        return _deterministic_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadTask:
        """Constructs contract instance from dictionary payload."""
        return cls(
            schema_version=data.get("schema_version", "download-task-v1"),
            task_id=data["task_id"],
            platform=data["platform"],
            scope_id=data.get("scope_id", "douyin:default"),
            platform_content_id=data["platform_content_id"],
            content_type=data["content_type"],
            source_url=data["source_url"],
            download_input=data.get("download_input", {}),
            canonical_item_version=data.get("canonical_item_version", "collection-item-v1"),
            collection_sync_run_id=data.get("collection_sync_run_id", ""),
            created_at=data.get("created_at", utcnow_iso()),
            priority=int(data.get("priority", DownloadPriority.NEW_COLLECTION.value)),
            reason=data.get("reason", DownloadReason.NEW_COLLECTION_ITEM.value),
            attempt_policy=data.get("attempt_policy", {}),
            status=data.get("status", DownloadTaskStatus.PENDING.value),
            metadata_ref=data.get("metadata_ref", {}),
        )


@runtime_checkable
class AssetStateProvider(Protocol):
    """Boundary protocol for querying whether physical media asset is already validly acquired.

    Collector domain must NOT hardcode filesystem `os.path.exists` checks.
    """

    def has_valid_asset(
        self,
        platform: str,
        scope_id: str,
        platform_content_id: str,
        content_type: str,
    ) -> bool:
        """Returns True if validated media asset is already present and complete."""
        ...


class NullAssetStateProvider:
    """Default provider: assumes asset is not present (standard for clean initial ingestion)."""

    def has_valid_asset(
        self,
        platform: str,
        scope_id: str,
        platform_content_id: str,
        content_type: str,
    ) -> bool:
        return False


class InMemoryAssetStateProvider:
    """Configurable in-memory provider for unit tests and deterministic simulation."""

    def __init__(self, initial_keys: set[tuple[str, str, str]] | None = None) -> None:
        # Key: (platform, scope_id, platform_content_id)
        self._valid_assets: set[tuple[str, str, str]] = set(initial_keys or [])

    def add_asset(self, platform: str, scope_id: str, platform_content_id: str) -> None:
        self._valid_assets.add((platform, scope_id, platform_content_id))

    def remove_asset(self, platform: str, scope_id: str, platform_content_id: str) -> None:
        self._valid_assets.discard((platform, scope_id, platform_content_id))

    def has_valid_asset(
        self,
        platform: str,
        scope_id: str,
        platform_content_id: str,
        content_type: str,
    ) -> bool:
        return (platform, scope_id, platform_content_id) in self._valid_assets


@dataclass(frozen=True)
class OutboxRecord:
    """Represents a row in the transactional download_outbox table."""

    outbox_id: str
    task_id: str
    scope_id: str
    platform: str
    platform_content_id: str
    content_type: str
    payload_json: str
    status: str
    created_at: str
    available_at: str
    source_sync_run_id: str | None = None
    first_source_sync_run_id: str | None = None
    last_seen_sync_run_id: str | None = None
    dispatched_at: str | None = None
    attempt_count: int = 0
    last_error: str | None = None

    def to_task(self) -> DownloadTask:
        """Reconstructs DownloadTask from outbox payload JSON."""
        return DownloadTask.from_dict(json.loads(self.payload_json))
