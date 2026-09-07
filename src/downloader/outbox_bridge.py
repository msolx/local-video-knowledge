"""Outbox Consumer Bridge (DY-D10).

Connects Collector C09 Transactional Outbox to the Downloader Durable Job Store.
Enforces the Durable Accept Protocol and Crash Gap invariants.

Key Invariants:
1. Durable Accept Before Outbox ACK:
   - Poll C09 PENDING outbox.
   - Validate download-task-v1 payload and audit against forbidden secrets.
   - Durably commit task into DownloaderJobStore in state READY.
   - ONLY AFTER successful job store commit: mark C09 outbox as DISPATCHED.
2. Crash Gap Safety:
   - Crash before job commit: Outbox remains PENDING -> redelivered next cycle.
   - Crash after job commit but before outbox ACK: Outbox remains PENDING ->
     idempotent durable accept on redelivery -> outbox marked DISPATCHED without
     creating duplicate jobs.
   - Crash after outbox DISPATCHED: Durable job remains READY in job store ->
     worker resumes it on restart. Zero tasks lost.
3. Separation of Concerns:
   - Outbox DISPATCHED != download succeeded.
   - Outbox FAILED != media download failed. Media download errors are handled by
     DownloaderJobStore and D08, never calling mark_outbox_failed.
   - Only structural payload/protocol delivery failures call mark_outbox_failed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from src.collector.download_models import DownloadTask, OutboxRecord
from src.downloader.contracts import scrub_secrets
from src.downloader.job_store import DownloaderJobStore, ImmutableIdentityConflictError

logger = logging.getLogger(__name__)

# Patterns identifying forbidden secrets in outbox payloads
_FORBIDDEN_SECRET_PATTERNS = (
    re.compile(r"sessionid", re.IGNORECASE),
    re.compile(r"sid_guard", re.IGNORECASE),
    re.compile(r"cookie\s*[:=]", re.IGNORECASE),
    re.compile(r"authorization\s*[:=]", re.IGNORECASE),
)


class OutboxPayloadValidationError(Exception):
    """Raised when an outbox payload violates schema or contains forbidden secrets."""


class OutboxConsumerBridge:
    """Consumes pending tasks from Collector Outbox and commits them to Downloader Job Store."""

    def __init__(
        self,
        collector_repo: Any,
        job_store: DownloaderJobStore,
    ) -> None:
        self.collector_repo = collector_repo
        self.job_store = job_store

    def validate_outbox_record(self, record: OutboxRecord) -> DownloadTask:
        """Validates outbox record schema, identity consistency, and secret absence.

        Raises OutboxPayloadValidationError if malformed.
        """
        if not record.payload_json or not record.payload_json.strip():
            raise OutboxPayloadValidationError(f"Empty payload_json for task '{record.task_id}'.")

        # Secret audit on raw payload
        for pat in _FORBIDDEN_SECRET_PATTERNS:
            if pat.search(record.payload_json):
                raise OutboxPayloadValidationError(
                    f"Outbox payload for task '{record.task_id}' contains forbidden credential pattern '{pat.pattern}'."
                )

        try:
            data = json.loads(record.payload_json)
        except json.JSONDecodeError as exc:
            raise OutboxPayloadValidationError(
                f"Invalid JSON payload for task '{record.task_id}': {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise OutboxPayloadValidationError(
                f"Payload for task '{record.task_id}' must be a JSON object, got {type(data).__name__}."
            )

        try:
            task = DownloadTask.from_dict(data)
        except Exception as exc:
            raise OutboxPayloadValidationError(
                f"Failed to deserialize DownloadTask from payload for '{record.task_id}': {exc}"
            ) from exc

        # Identity consistency checks
        if task.task_id != record.task_id:
            raise OutboxPayloadValidationError(
                f"Identity conflict: record.task_id='{record.task_id}' != payload.task_id='{task.task_id}'."
            )
        if task.platform != record.platform:
            raise OutboxPayloadValidationError(
                f"Identity conflict: record.platform='{record.platform}' != payload.platform='{task.platform}'."
            )
        if task.platform_content_id != record.platform_content_id:
            raise OutboxPayloadValidationError(
                f"Identity conflict: record.platform_content_id='{record.platform_content_id}' != "
                f"payload.platform_content_id='{task.platform_content_id}'."
            )

        return task

    def intake_batch(self, limit: int = 20) -> int:
        """Polls available pending outbox records and durably ingests them.

        Returns the number of tasks successfully accepted and marked DISPATCHED.
        """
        # Step 1: Poll pending outbox using official C09 API
        poll_fn = getattr(self.collector_repo, "poll_pending_outbox", None) or getattr(
            self.collector_repo, "get_pending_outbox_tasks", None
        )
        if not poll_fn:
            return 0
        records: list[OutboxRecord] = poll_fn(limit=limit)
        if not records:
            return 0

        dispatched_task_ids: list[str] = []

        for record in records:
            # Step 2: Validate payload
            try:
                task = self.validate_outbox_record(record)
            except OutboxPayloadValidationError as val_err:
                sanitized_msg = scrub_secrets(str(val_err))
                logger.error("Outbox task delivery rejected: %s", sanitized_msg)
                try:
                    self.collector_repo.mark_outbox_failed(
                        task_id=record.task_id,
                        error=sanitized_msg,
                        max_attempts=1,  # Terminal FAILED for structural delivery violations
                    )
                except Exception as repo_err:
                    logger.warning("Failed to mark outbox failed for '%s': %s", record.task_id, repo_err)
                continue

            # Step 3: Durably accept into DownloaderJobStore
            try:
                job, was_created = self.job_store.accept_task(
                    task=task,
                    outbox_id=record.outbox_id,
                    sync_run_id=record.source_sync_run_id,
                )
                logger.debug(
                    "Task '%s' accepted into DownloaderJobStore (created=%s, state=%s)",
                    record.task_id,
                    was_created,
                    job.state.value,
                )
            except ImmutableIdentityConflictError as conflict_err:
                sanitized_msg = scrub_secrets(str(conflict_err))
                logger.error("Immutable identity conflict for '%s': %s", record.task_id, sanitized_msg)
                try:
                    self.collector_repo.mark_outbox_failed(
                        task_id=record.task_id,
                        error=sanitized_msg,
                        max_attempts=1,
                    )
                except Exception as repo_err:
                    logger.warning("Failed to mark outbox failed: %s", repo_err)
                continue
            except Exception as store_err:
                logger.error("Failed to commit task '%s' into DownloaderJobStore: %s", record.task_id, store_err)
                # DO NOT mark dispatched; outbox remains PENDING for retry next iteration
                continue

            # Step 4: ONLY AFTER durable job commit: mark outbox as DISPATCHED
            dispatched_task_ids.append(record.task_id)

        if dispatched_task_ids:
            try:
                acked_count = self.collector_repo.mark_outbox_dispatched(dispatched_task_ids)
                logger.info(
                    "Marked %d outbox records as DISPATCHED in Collector DB.",
                    acked_count,
                )
            except Exception as ack_err:
                logger.warning(
                    "Error marking outbox tasks dispatched (%s): %s. Jobs were durably committed.",
                    dispatched_task_ids,
                    ack_err,
                )

        return len(dispatched_task_ids)

    def intake_all(self, batch_size: int = 50, max_batches: int = 100) -> int:
        """Drains all currently available pending outbox records."""
        total_ingested = 0
        for _ in range(max_batches):
            count = self.intake_batch(limit=batch_size)
            total_ingested += count
            if count < batch_size:
                break
        return total_ingested
