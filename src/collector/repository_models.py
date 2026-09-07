"""Strongly-typed data models for the Collector Metadata Repository (DY-C08)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .douyin.transform import PriorCollectionState


class SyncRunStatus(str, Enum):
    """Lifecycle state of a collection synchronization session."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class SyncRunMode(str, Enum):
    """Operational synchronization mode."""

    INCREMENTAL = "incremental"
    BACKFILL = "backfill"
    PROBE = "probe"


@dataclass(frozen=True)
class SyncState:
    """Represents the committed watermark, historical coverage, and last successful sync run for a scope.

    Notes on Watermark Fields:
        - `incremental_head_watermark_cursor`: Authoritative runtime field for incremental sync boundaries.
        - `committed_watermark_cursor`: DEPRECATED COMPATIBILITY FIELD (retained as compatibility projection only;
          new business logic in C05/C09 must not read this field directly).
    """

    scope_id: str
    platform: str
    committed_watermark_cursor: str | None  # DEPRECATED COMPATIBILITY FIELD: use incremental_head_watermark_cursor
    last_successful_sync_run_id: str | None
    head_anchor_content_id: str | None
    updated_at: str
    history_complete: bool = False
    incremental_head_watermark_cursor: str | None = None
    backfill_checkpoint_cursor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "platform": self.platform,
            "history_complete": self.history_complete,
            "incremental_head_watermark_cursor": self.incremental_head_watermark_cursor or self.committed_watermark_cursor,
            "committed_watermark_cursor": self.committed_watermark_cursor,
            "backfill_checkpoint_cursor": self.backfill_checkpoint_cursor,
            "last_successful_sync_run_id": self.last_successful_sync_run_id,
            "head_anchor_content_id": self.head_anchor_content_id,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SyncRunRecord:
    """Strongly-typed persistent representation of a sync-run-v1 session."""

    sync_run_id: str
    scope_id: str
    platform: str
    mode: str
    status: str
    started_at: str
    finished_at: str | None = None
    previous_watermark_cursor: str | None = None
    candidate_watermark_cursor: str | None = None
    stop_reason: str | None = None
    stop_cursor: str | None = None
    has_more: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    execution_context: dict[str, Any] = field(default_factory=dict)
    canonical_json: str = "{}"

    @property
    def is_running(self) -> bool:
        return self.status == SyncRunStatus.RUNNING.value

    @property
    def is_completed(self) -> bool:
        return self.status == SyncRunStatus.COMPLETED.value

    @property
    def is_failed(self) -> bool:
        return self.status == SyncRunStatus.FAILED.value

    @property
    def is_aborted(self) -> bool:
        return self.status == SyncRunStatus.ABORTED.value


@dataclass(frozen=True)
class CollectionItemRecord:
    """Current collection membership state and canonical entity representation."""

    scope_id: str
    platform: str
    platform_content_id: str
    content_type: str
    active: bool
    first_seen_at: str
    last_seen_at: str
    published_at: str
    reappeared_at: str | None
    observed_count: int
    last_seen_position: int | None
    canonical_json: str
    updated_at: str


@dataclass(frozen=True)
class CollectionObservationRecord:
    """Discrete observation event record representing a historical encounter."""

    observation_id: str
    sync_run_id: str
    scope_id: str
    platform: str
    platform_content_id: str
    observed_at: str
    page_number: int
    position_in_page: int
    global_rank_seen: int
    page_request_cursor: str
    page_response_cursor: str
    is_first_observation: bool
    is_reappearance: bool
    raw_ref: dict[str, Any]
    canonical_json: str


@dataclass(frozen=True)
class StagedItemRecord:
    """Run-scoped staging record for an item candidate awaiting finalize_success."""

    sync_run_id: str
    scope_id: str
    platform: str
    platform_content_id: str
    content_type: str
    canonical_item_json: str
    is_reappearance: bool
    staged_at: str


__all__ = [
    "PriorCollectionState",
    "SyncRunStatus",
    "SyncRunMode",
    "SyncState",
    "SyncRunRecord",
    "CollectionItemRecord",
    "CollectionObservationRecord",
    "StagedItemRecord",
]
