"""Download Queue Producer and Eligibility Engine (DY-C09).

Coordinates:
- DownloadEligibilityPolicy: Pure business rules determining if an item requires download.
- DownloadQueueProducer: Generates generic DownloadTask contracts from Canonical Items.
- DownloadOutboxDispatcher: Queries and transitions transactional outbox tasks.

Invariants:
1. COLLECTION COMMIT BEFORE DOWNLOAD QUEUE COMMIT: Outbox records are committed in the
   exact same transaction as CollectionItem mutations and watermark advancement.
2. DETERMINISTIC TASK ID: Every task ID is an idempotency key derived from (platform, scope, content_id, type).
3. SECURITY: No cookies, sessionids, or authentication tokens are ever embedded in DownloadTask payloads.
4. AT-LEAST-ONCE DELIVERY: Dispatcher guarantees at-least-once delivery; consumers must be idempotent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .base import utcnow_iso
from .download_models import (
    AssetStateProvider,
    DownloadPriority,
    DownloadReason,
    DownloadTask,
    DownloadTaskStatus,
    NullAssetStateProvider,
    OutboxRecord,
    compute_download_task_id,
)

if TYPE_CHECKING:
    from .douyin.transform import PriorCollectionState
    from .repository import SqliteMetadataRepository

logger = logging.getLogger("collector.download_queue")


@dataclass(frozen=True)
class DownloadEligibilityDecision:
    """Decision output by DownloadEligibilityPolicy."""

    is_eligible: bool
    reason: DownloadReason | None
    priority: int
    details: dict[str, Any] = field(default_factory=dict)


class DownloadEligibilityPolicy:
    """Pure deterministic policy governing whether an item requires a DownloadTask."""

    def evaluate(
        self,
        item: dict[str, Any],
        scope_id: str,
        prior: PriorCollectionState | None = None,
        is_current_reappearance: bool = False,
        asset_provider: AssetStateProvider | None = None,
    ) -> DownloadEligibilityDecision:
        """Evaluates download eligibility for a canonical collection item.

        Rules:
        1. Inactive items (active=False) NEVER trigger download tasks.
        2. Validated existing assets NEVER trigger download tasks (zero redundant enqueue).
        3. Reappearance event with missing asset -> REAPPEARANCE_ASSET_MISSING (priority 5).
           CRITICAL: Triggered ONLY if this sync run's observation is an active reappearance event
           (is_current_reappearance=True). Historical reappeared_at timestamp does NOT trigger this rule.
        4. Known item with missing asset -> ASSET_MISSING_REPAIR (priority 5).
           Applies to all known items whose assets are missing, including items that previously reappeared.
        5. First-seen active item -> NEW_COLLECTION_ITEM (priority 10).
        """
        provider = asset_provider or NullAssetStateProvider()
        platform = item.get("platform", "douyin")
        content_id = str(item.get("platform_content_id") or "").strip()
        content_type = str(item.get("content_type") or "video").strip()

        coll = item.get("collection", {})
        is_active = bool(coll.get("active", True))

        # Rule 1: Inactive items do not trigger downloads
        if not is_active:
            return DownloadEligibilityDecision(
                is_eligible=False,
                reason=None,
                priority=0,
                details={"status": "inactive_item", "content_id": content_id},
            )

        # Rule 2: If asset is already present and validated, skip enqueue
        if provider.has_valid_asset(platform, scope_id, content_id, content_type):
            return DownloadEligibilityDecision(
                is_eligible=False,
                reason=None,
                priority=0,
                details={"status": "asset_already_valid", "content_id": content_id},
            )

        # Rule 3: Current Reappearance Event with missing asset (ONLY for active reappearance observations)
        if is_current_reappearance:
            return DownloadEligibilityDecision(
                is_eligible=True,
                reason=DownloadReason.REAPPEARANCE_ASSET_MISSING,
                priority=DownloadPriority.REPAIR.value,
                details={"status": "reappearance_missing_asset", "content_id": content_id},
            )

        # Rule 4: Known item with missing asset (repair, including items that reappeared in past runs)
        if prior and prior.exists:
            return DownloadEligibilityDecision(
                is_eligible=True,
                reason=DownloadReason.ASSET_MISSING_REPAIR,
                priority=DownloadPriority.REPAIR.value,
                details={"status": "known_item_missing_asset", "content_id": content_id},
            )

        # Rule 5: New collection item
        return DownloadEligibilityDecision(
            is_eligible=True,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
            priority=DownloadPriority.NEW_COLLECTION.value,
            details={"status": "new_collection_item", "content_id": content_id},
        )


class DownloadQueueProducer:
    """Production DownloadQueueProducer implementing C01 protocol and transactional intent generation."""

    def __init__(
        self,
        repository: SqliteMetadataRepository | None = None,
        asset_provider: AssetStateProvider | None = None,
        eligibility_policy: DownloadEligibilityPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.asset_provider = asset_provider or NullAssetStateProvider()
        self.eligibility_policy = eligibility_policy or DownloadEligibilityPolicy()

    def build_download_intent(
        self,
        item: dict[str, Any],
        sync_run_id: str,
        scope_id: str,
        prior: PriorCollectionState | None = None,
        is_current_reappearance: bool = False,
    ) -> DownloadTask | None:
        """Evaluates an item and builds a pure DownloadTask contract if eligible.

        Sanitizes security parameters: strictly strips credentials, cookies, and tokens.
        """
        decision = self.eligibility_policy.evaluate(
            item=item,
            scope_id=scope_id,
            prior=prior,
            is_current_reappearance=is_current_reappearance,
            asset_provider=self.asset_provider,
        )
        if not decision.is_eligible or decision.reason is None:
            return None

        platform = item.get("platform", "douyin")
        content_id = str(item.get("platform_content_id") or "").strip()
        content_type = str(item.get("content_type") or "video").strip()
        source_url = str(item.get("source_url") or f"https://www.douyin.com/video/{content_id}").strip()

        # Extract and sanitize volatile download input hints
        raw_download_input = item.get("media", {}).get("download_input") or {}
        sanitized_input: dict[str, Any] = {}
        if isinstance(raw_download_input, dict):
            for k, v in raw_download_input.items():
                # Strip credential and secret headers/cookies
                if k.lower() in ("cookie", "cookies", "sessionid", "sid_guard", "token", "authorization"):
                    continue
                sanitized_input[k] = v
        sanitized_input["is_volatile_hint"] = True

        task_id = compute_download_task_id(
            platform=platform,
            scope_id=scope_id,
            platform_content_id=content_id,
            content_type=content_type,
        )

        return DownloadTask(
            task_id=task_id,
            platform=platform,
            scope_id=scope_id,
            platform_content_id=content_id,
            content_type=content_type,
            source_url=source_url,
            download_input=sanitized_input,
            canonical_item_version=item.get("schema_version", "collection-item-v1"),
            collection_sync_run_id=sync_run_id,
            created_at=utcnow_iso(),
            priority=decision.priority,
            reason=decision.reason.value,
            attempt_policy={"max_attempts": 3, "backoff_multiplier": 2.0},
            status=DownloadTaskStatus.PENDING.value,
        )

    def build_intents_for_items(
        self,
        items: list[dict[str, Any]],
        sync_run_id: str,
        scope_id: str,
        priors: dict[str, PriorCollectionState] | None = None,
        reappearances: dict[str, bool] | None = None,
    ) -> list[DownloadTask]:
        """Batch transforms a list of items into eligible DownloadTasks."""
        priors_map = priors or {}
        reappearances_map = reappearances or {}
        tasks: list[DownloadTask] = []
        for it in items:
            cid = str(it.get("platform_content_id") or "").strip()
            prior = priors_map.get(cid)
            is_reapp = reappearances_map.get(cid, False)
            task = self.build_download_intent(
                item=it,
                sync_run_id=sync_run_id,
                scope_id=scope_id,
                prior=prior,
                is_current_reappearance=is_reapp,
            )
            if task:
                tasks.append(task)
        return tasks

    def enqueue(self, task: DownloadTask) -> bool:
        """C01 Generic Protocol compliance: enqueues a DownloadTask into the repository outbox."""
        if not self.repository:
            logger.warning(f"No repository configured on DownloadQueueProducer; cannot persist task {task.task_id}")
            return False
        try:
            self.repository.persist_outbox_tasks([task])
            return True
        except Exception as e:
            logger.error(f"Failed to enqueue task '{task.task_id}': {e}")
            return False


class DownloadOutboxDispatcher:
    """Dispatches durable download outbox records to downstream consumers (Downloader Queue)."""

    def __init__(self, repository: SqliteMetadataRepository) -> None:
        self.repository = repository

    def poll_pending(self, limit: int = 50, scope_id: str | None = None) -> list[DownloadTask]:
        """Polls available pending download tasks from the outbox table."""
        records = self.repository.get_pending_outbox_tasks(limit=limit, scope_id=scope_id)
        return [r.to_task() for r in records]

    def mark_dispatched(self, task_ids: list[str]) -> int:
        """Marks tasks as DISPATCHED (handed off to downstream queue)."""
        return self.repository.mark_outbox_dispatched(task_ids)

    def mark_delivery_failed(self, task_id: str, error: str) -> None:
        """Marks a task as FAILED with error provenance."""
        self.repository.mark_outbox_failed(task_id, error)
