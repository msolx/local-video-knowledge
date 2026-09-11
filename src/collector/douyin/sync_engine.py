"""Orchestration Engine for Douyin Incremental Watermark Sync and Backfill (DY-C05).

Coordinates:
- C02: BrowserRuntimeProvider
- C03: DouyinAuthStateDetector (Account Scope Resolution)
- C04: DouyinSourceClient (In-Browser listcollection fetching)
- C06: DiskRawArchiver (Deterministic Raw-Before-Transform storage)
- C07: DouyinCanonicalTransformer (Pure canonical JSON transformation)
- C08: SqliteMetadataRepository (Transactional SQLite staging & WAL persistence)
- QW-04: IncrementalSyncPolicy (Authoritative watermark & boundary stopping rules)

Enforces Authoritative Invariants:
1. Incremental sync ALWAYS begins at cursor="0".
2. Strategy A (first-known early stop) is strictly prohibited.
3. Safe early stop condition is evaluated at page boundaries:
   `int(response_cursor) <= int(committed_watermark) OR has_more == 0`.
4. Run scope is frozen at begin_sync_run and verified throughout execution.
5. In-run content deduplication protects against duplicate staging within the same run.
6. Failed or aborted runs NEVER advance committed watermark or mutate existing collection items.
7. Explicit inactive mutation remains conservative (empty list by default).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..base import utcnow_iso
from ..errors import CollectorError, CollectorErrorCode
from ..raw_archive import DiskRawArchiver, RawArchiveRef
from ..repository_models import SyncRunRecord

if TYPE_CHECKING:
    from ..repository import SqliteMetadataRepository

DEFAULT_SCOPE_ID = "douyin:default"
from .auth_state import DouyinAuthStateDetector
from .config import DouyinCollectorConfig
from .source_client import CollectionPage, DouyinSourceClient, SourceClientError
from .sync_policy import (
    IncrementalSyncPolicy,
    PageFacts,
    StopAction,
    StopDecision,
    StopReasonCode,
)
from .transform import (
    CanonicalTransformResult,
    DouyinCanonicalTransformer,
    PriorCollectionState,
    RawRefContext,
    TransformContext,
)

logger = logging.getLogger("collector.douyin.engine")


def generate_sync_run_id() -> str:
    """Generate canonical sync run ID matching sync-run-v1 specification."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand_suffix = uuid.uuid4().hex[:6]
    return f"sync_douyin_coll_{ts}_{rand_suffix}"


class DouyinSyncEngine:
    """Production incremental and backfill synchronization engine for Douyin."""

    def __init__(
        self,
        config: DouyinCollectorConfig,
        repository: SqliteMetadataRepository,
        archiver: DiskRawArchiver,
        transformer: DouyinCanonicalTransformer,
        source_client: DouyinSourceClient,
        auth_detector: DouyinAuthStateDetector | None = None,
        queue_producer: Any | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.archiver = archiver
        self.transformer = transformer
        self.source_client = source_client
        self.auth_detector = auth_detector
        self.queue_producer = queue_producer

    def sync(
        self,
        sync_run_id: str | None = None,
        scope_id: str | None = None,
        mode: str = "incremental",
        page_size: int | None = None,
        max_pages: int | None = None,
        backfill_limit: int | None = None,
        resume: bool = False,
        initial_cursor: str | None = None,
        dry_run: bool = False,
        service_lock_acquired: bool = True,
        inter_page_delay_sec: float = 0.5,
    ) -> SyncRunRecord:
        """Execute synchronization run (incremental or backfill).

        Args:
            sync_run_id: Optional explicit run ID. If None, generated.
            scope_id: Account scope ID (e.g. "douyin:dyacct_<16_hex>"). If None, resolved via C03.
            mode: "incremental" or "backfill".
            page_size: Items per page (defaults to config.page_size).
            max_pages: Maximum pages to fetch.
            backfill_limit: Maximum items to collect (for backfill mode).
            resume: In backfill mode, whether to resume from backfill_checkpoint_cursor.
            initial_cursor: In backfill mode, explicit initial request cursor.
            dry_run: If True, fetches & stages items, but rolls back staging without committing watermark.
            service_lock_acquired: Flag proving caller holds ServiceLock.
            inter_page_delay_sec: Polite delay between page requests.

        Returns:
            Finalized SyncRunRecord from repository.
        """
        run_id = sync_run_id or generate_sync_run_id()
        eff_page_size = page_size or self.config.page_size
        eff_mode = mode.lower()

        logger.info(f"[{run_id}] Initiating Douyin collection sync (mode={eff_mode}, dry_run={dry_run})")

        # ---------------------------------------------------------------------
        # 1. Resolve Account Scope ID (Section A Production Guard)
        # ---------------------------------------------------------------------
        resolved_scope = scope_id
        if not resolved_scope:
            if self.auth_detector:
                preflight = self.auth_detector.detect_auth_state()
                if not preflight.can_sync or not preflight.account_scope_id:
                    raise CollectorError(
                        CollectorErrorCode.AUTH_NOT_READY,
                        f"Cannot start sync: auth preflight failed ({preflight.reason_code}) "
                        f"or account_scope_id unresolved: {preflight.account_scope_id}",
                        details=preflight.to_dict(),
                    )
                resolved_scope = preflight.account_scope_id
            else:
                resolved_scope = DEFAULT_SCOPE_ID

        logger.info(f"[{run_id}] Frozen account scope: {resolved_scope}")

        # ---------------------------------------------------------------------
        # 2. Crash Recovery & Repository Run Initialization
        # ---------------------------------------------------------------------
        # Clean any orphaned RUNNING runs from crashed predecessors
        recovered_stale = self.repository.recover_stale_runs(
            scope_id=resolved_scope,
            service_lock_acquired=service_lock_acquired,
        )
        if recovered_stale:
            logger.warning(f"[{run_id}] Recovered {len(recovered_stale)} stale runs: {recovered_stale}")

        # Begin sync run
        run_record = self.repository.begin_sync_run(
            sync_run_id=run_id,
            mode=eff_mode,
            scope_id=resolved_scope,
            platform="douyin",
        )

        # Retrieve current committed watermark and historical coverage for this scope
        sync_state = self.repository.get_sync_state(scope_id=resolved_scope, platform="douyin")
        committed_watermark = (
            sync_state.incremental_head_watermark_cursor
            if sync_state else None
        )
        history_complete = sync_state.history_complete if sync_state else False
        backfill_checkpoint = sync_state.backfill_checkpoint_cursor if sync_state else None
        logger.info(
            f"[{run_id}] Baseline state: watermark='{committed_watermark}', "
            f"history_complete={history_complete}, backfill_checkpoint='{backfill_checkpoint}'"
        )

        # ---------------------------------------------------------------------
        # 3. Policy Initialization
        # ---------------------------------------------------------------------
        policy = IncrementalSyncPolicy(
            mode=eff_mode,
            committed_watermark=committed_watermark,
            max_pages=max_pages,
            backfill_limit=backfill_limit,
            history_complete=history_complete,
        )

        # ---------------------------------------------------------------------
        # 4. Pagination & Ingestion Loop
        # ---------------------------------------------------------------------
        # Invariant 1: Incremental sync ALWAYS begins at cursor="0".
        # Backfill can resume from backfill_checkpoint or initial_cursor if requested.
        current_request_cursor = "0"
        if eff_mode == "backfill":
            if initial_cursor is not None:
                current_request_cursor = str(initial_cursor)
            elif resume and backfill_checkpoint is not None:
                current_request_cursor = str(backfill_checkpoint)
        page_number = 1

        seen_platform_content_ids: set[str] = set()
        items_seen = 0
        new_items = 0
        known_items = 0
        reappeared_items = 0
        duplicate_in_run = 0
        discovered_items: list[str] = []

        stop_decision: StopDecision | None = None
        last_page: CollectionPage | None = None

        try:
            while True:
                logger.info(
                    f"[{run_id}] Fetching page {page_number} (request_cursor='{current_request_cursor}', count={eff_page_size})..."
                )

                # 4.1 Fetch Page (C04 In-Browser SourceClient)
                page = self._fetch_page_with_retry(
                    cursor=current_request_cursor,
                    count=eff_page_size,
                    run_id=run_id,
                )
                last_page = page

                logger.info(
                    f"[{run_id}] Page {page_number} received: {len(page.items)} items, "
                    f"response_cursor='{page.response_cursor}', has_more={page.has_more}"
                )

                # 4.2 Raw-Before-Transform Archiving (C06)
                archive_ref = self.archiver.archive_page(
                    sync_run_id=run_id,
                    platform="douyin",
                    page_number=page_number,
                    request_cursor=current_request_cursor,
                    response_cursor=page.response_cursor,
                    raw_response=page.raw_response,
                    fetched_at=page.fetched_at,
                )

                # 4.3 Canonical Transformation & DB Staging (C07 -> C08)
                page_new = 0
                page_known = 0
                page_reappeared = 0
                page_duplicates = 0

                for item_idx, raw_item in enumerate(page.items):
                    platform_content_id = str(raw_item.get("aweme_id") or "").strip()
                    if not platform_content_id:
                        logger.warning(f"[{run_id}] Skipping item at index {item_idx}: missing aweme_id")
                        continue

                    # In-run content deduplication guard
                    if platform_content_id in seen_platform_content_ids:
                        page_duplicates += 1
                        duplicate_in_run += 1
                        logger.debug(
                            f"[{run_id}] In-run duplicate item '{platform_content_id}' seen on page {page_number}"
                        )
                        continue

                    seen_platform_content_ids.add(platform_content_id)
                    items_seen += 1

                    # Query prior state from repository for this account scope
                    prior_state = self.repository.get_prior_state(
                        platform_content_id=platform_content_id,
                        scope_id=resolved_scope,
                        platform="douyin",
                    )

                    # Build context with C06 RawRef pointer
                    raw_ref_ctx = RawRefContext(
                        archive_file=archive_ref.path,
                        sha256=archive_ref.sha256,
                        item_index=item_idx,
                    )
                    ctx = TransformContext(
                        sync_run_id=run_id,
                        observed_at=page.fetched_at,
                        page_number=page_number,
                        position_in_page=item_idx,
                        global_rank_seen=items_seen,
                        request_cursor=current_request_cursor,
                        response_cursor=page.response_cursor,
                        raw_ref=raw_ref_ctx,
                    )

                    # Execute deterministic transformation
                    transform_result = self.transformer.transform_item(
                        raw_item=raw_item,
                        context=ctx,
                        prior=prior_state,
                    )

                    # Stage into run_item_staging and append observation
                    self.repository.stage_transform_result(
                        result=transform_result,
                        sync_run_id=run_id,
                        scope_id=resolved_scope,
                        platform="douyin",
                    )

                    # Update counters
                    if transform_result.is_first_observation:
                        page_new += 1
                        new_items += 1
                        discovered_items.append(transform_result.platform_content_id)
                    else:
                        page_known += 1
                        known_items += 1

                    if transform_result.is_reappearance:
                        page_reappeared += 1
                        reappeared_items += 1

                # 4.4 QW-04 Incremental Policy Evaluation
                facts = PageFacts(
                    page_number=page_number,
                    request_cursor=current_request_cursor,
                    response_cursor=page.response_cursor,
                    has_more=1 if page.has_more else 0,
                    items_count=len(page.items),
                    new_items_count=page_new,
                    known_items_count=page_known,
                    reappeared_items_count=page_reappeared,
                    duplicate_in_run_count=page_duplicates,
                )

                stop_decision = policy.evaluate_page(facts)
                logger.info(
                    f"[{run_id}] Policy decision: action='{stop_decision.action.value}', "
                    f"reason='{stop_decision.reason_code.value}', "
                    f"candidate_wm='{stop_decision.candidate_watermark_cursor}'"
                )

                if stop_decision.should_stop:
                    logger.info(
                        f"[{run_id}] Stopping sync loop: {stop_decision.reason_code.value} "
                        f"(details={stop_decision.details})"
                    )
                    break

                # Prepare next page
                current_request_cursor = str(page.response_cursor)
                page_number += 1

                if inter_page_delay_sec > 0:
                    time.sleep(inter_page_delay_sec)

        except Exception as e:
            logger.exception(f"[{run_id}] Error occurred during collection sync: {e}")
            err_code = "SYNC_EXECUTION_ERROR"
            if isinstance(e, SourceClientError):
                err_code = e.source_code.value if hasattr(e, "source_code") else "SOURCE_CLIENT_ERROR"

            metrics = {
                "pages_fetched": page_number,
                "items_seen": items_seen,
                "new_items": new_items,
                "known_items": known_items,
                "reappeared_items": reappeared_items,
                "duplicate_in_run": duplicate_in_run,
                "discovered": list(discovered_items),
            }
            # Guaranteed rollback of staged items and preservation of watermark
            failed_run = self.repository.finalize_failure(
                sync_run_id=run_id,
                error_code=err_code,
                error_message=str(e),
                metrics=metrics,
                scope_id=resolved_scope,
                platform="douyin",
            )
            raise CollectorError(
                CollectorErrorCode.RUNTIME_ERROR,
                f"Sync run '{run_id}' failed: {e}",
                details={"sync_run_id": run_id, "scope_id": resolved_scope, "error_code": err_code},
            ) from e

        # ---------------------------------------------------------------------
        # 5. Finalization (Success vs Dry-Run)
        # ---------------------------------------------------------------------
        metrics = {
            "pages_fetched": page_number,
            "items_seen": items_seen,
            "new_items": new_items,
            "known_items": known_items,
            "reappeared_items": reappeared_items,
            "duplicate_in_run": duplicate_in_run,
            "discovered": list(discovered_items),
        }

        if dry_run:
            logger.info(f"[{run_id}] Dry-run mode: clearing staging without committing items or watermark.")
            final_record = self.repository.finalize_failure(
                sync_run_id=run_id,
                error_code="DRY_RUN_COMPLETED",
                error_message="Dry run complete; staging cleared.",
                metrics=metrics,
                scope_id=resolved_scope,
                platform="douyin",
            )
            return final_record

        candidate_wm = stop_decision.candidate_watermark_cursor if stop_decision else policy.candidate_watermark
        stop_reason = stop_decision.reason_code.value if stop_decision else "completed"

        # Determine history_complete and backfill_checkpoint for sync_state
        new_history_complete = history_complete
        new_backfill_checkpoint = backfill_checkpoint

        if stop_decision and stop_decision.action == StopAction.STOP_TERMINAL:
            # End of collection reached (has_more == 0 or empty) -> historical coverage complete!
            new_history_complete = True
            new_backfill_checkpoint = None
        elif stop_decision and stop_decision.action == StopAction.STOP_SAFE_BOUNDARY:
            # Reached previous historical boundary during incremental run -> history remains complete
            new_history_complete = True
            new_backfill_checkpoint = None
        elif stop_decision and stop_decision.action == StopAction.STOP_LIMIT:
            # Stopped early before terminal or boundary -> historical coverage incomplete
            if not history_complete:
                new_backfill_checkpoint = last_page.response_cursor if last_page else None
            # STOP_LIMIT must never advance head watermark!
            candidate_wm = None

        # In backfill mode starting from non-zero, candidate_wm must not overwrite head watermark
        if eff_mode == "backfill" and current_request_cursor != "0":
            candidate_wm = None

        # ---------------------------------------------------------------------
        # Generate transactional download tasks if queue_producer is configured
        # ---------------------------------------------------------------------
        download_tasks = None
        if self.queue_producer is not None:
            staged_records = self.repository.get_staged_items(run_id)
            if staged_records:
                staged_dicts = []
                priors_map: dict[str, PriorCollectionState] = {}
                reappearances_map: dict[str, bool] = {}
                for sr in staged_records:
                    try:
                        item_dict = json.loads(sr.canonical_item_json)
                        staged_dicts.append(item_dict)
                        prior_state = self.repository.get_prior_state(
                            platform_content_id=sr.platform_content_id,
                            scope_id=resolved_scope,
                            platform="douyin",
                        )
                        priors_map[sr.platform_content_id] = prior_state
                        reappearances_map[sr.platform_content_id] = sr.is_reappearance
                    except Exception as e:
                        logger.warning(f"[{run_id}] Failed to parse staged item for queue producer: {e}")
                download_tasks = self.queue_producer.build_intents_for_items(
                    items=staged_dicts,
                    sync_run_id=run_id,
                    scope_id=resolved_scope,
                    priors=priors_map,
                    reappearances=reappearances_map,
                )
                logger.info(
                    f"[{run_id}] Queue producer evaluated {len(staged_dicts)} staged items -> {len(download_tasks)} download tasks."
                )

        final_record = self.repository.finalize_success(
            sync_run_id=run_id,
            candidate_watermark=candidate_wm,
            stop_reason=stop_reason,
            metrics=metrics,
            stop_cursor=last_page.response_cursor if last_page else None,
            has_more=1 if (last_page and last_page.has_more) else 0,
            history_complete=new_history_complete,
            backfill_checkpoint_cursor=new_backfill_checkpoint,
            download_tasks=download_tasks,
            items_to_mark_inactive=[],  # Conservative empty list per Section A / C08
            expected_previous_watermark=committed_watermark,
            scope_id=resolved_scope,
            platform="douyin",
        )

        logger.info(
            f"[{run_id}] Sync successfully completed! Items seen: {items_seen}, new: {new_items}, "
            f"new watermark: '{candidate_wm}', history_complete={new_history_complete}"
        )
        return final_record

    def backfill(
        self,
        sync_run_id: str | None = None,
        scope_id: str | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
        resume: bool = False,
        initial_cursor: str | None = None,
        dry_run: bool = False,
        service_lock_acquired: bool = True,
        inter_page_delay_sec: float = 0.5,
        **kwargs: Any,
    ) -> SyncRunRecord:
        """Historical backfill synchronization (delegates to sync in backfill mode)."""
        return self.sync(
            sync_run_id=sync_run_id,
            scope_id=scope_id,
            mode="backfill",
            backfill_limit=limit,
            max_pages=max_pages,
            resume=resume,
            initial_cursor=initial_cursor,
            dry_run=dry_run,
            service_lock_acquired=service_lock_acquired,
            inter_page_delay_sec=inter_page_delay_sec,
            **kwargs,
        )

    def _fetch_page_with_retry(
        self,
        cursor: str,
        count: int,
        run_id: str,
        max_retries: int = 2,
    ) -> CollectionPage:
        """Fetch collection page with bounded retries for transient transport/timeout errors."""
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return self.source_client.fetch_collection_page(cursor=cursor, count=count)
            except SourceClientError as e:
                last_err = e
                # Only retry on transient timeouts or network issues
                if "TIMEOUT" in e.message or "Context" in e.message:
                    logger.warning(
                        f"[{run_id}] Transient source fetch error (attempt {attempt}/{max_retries}): {e}. Retrying..."
                    )
                    time.sleep(1.0 * attempt)
                    continue
                # Non-retryable platform / auth errors re-raise immediately
                raise
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[{run_id}] Unexpected error fetching page (attempt {attempt}/{max_retries}): {e}. Retrying..."
                )
                time.sleep(1.0 * attempt)

        raise SourceClientError(
            source_code=CollectorErrorCode.RUNTIME_ERROR,  # type: ignore
            message=f"Failed to fetch collection page at cursor '{cursor}' after {max_retries} attempts: {last_err}",
        ) from last_err
