"""Douyin Collection Ingestion Adapter implementation skeleton.

Implements BaseCollector and maps dependency injection slots for subsequent engineering tasks:
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

import logging
from typing import Any

from ..base import BaseCollector, CollectorMode, CollectorRunResult, CollectorStatus, utcnow_iso
from ..errors import CollectorErrorCode
from ..interfaces import (
    AuthStateDetector,
    BrowserRuntimeProvider,
    CanonicalTransformer,
    DownloadQueueProducer,
    MetadataRepository,
    RawArchiver,
    SourceClient,
)
from .config import DouyinCollectorConfig

logger = logging.getLogger("collector.douyin")


class DouyinCollector(BaseCollector):
    """Douyin collection ingestion adapter."""

    def __init__(
        self,
        config: DouyinCollectorConfig | None = None,
        browser_runtime: BrowserRuntimeProvider | None = None,
        auth_detector: AuthStateDetector | None = None,
        source_client: SourceClient | None = None,
        raw_archiver: RawArchiver | None = None,
        transformer: CanonicalTransformer | None = None,
        repository: MetadataRepository | None = None,
        queue_producer: DownloadQueueProducer | None = None,
    ) -> None:
        cfg = config or DouyinCollectorConfig()
        super().__init__(cfg)
        self.config: DouyinCollectorConfig = cfg
        self.browser_runtime = browser_runtime
        self.auth_detector = auth_detector
        self.source_client = source_client
        self.raw_archiver = raw_archiver
        self.transformer = transformer
        self.repository = repository
        self.queue_producer = queue_producer

    @property
    def platform(self) -> str:
        return "douyin"

    def initialize(self, run_id: str) -> None:
        logger.info(f"[{run_id}] Initializing DouyinCollector runtime hooks...")
        if not self.browser_runtime and self.config.profile_path:
            profile_p = Path(self.config.profile_path)
            if profile_p.exists():
                from .browser_runtime import DouyinBrowserRuntimeProvider
                self.browser_runtime = DouyinBrowserRuntimeProvider(
                    profile_path=profile_p,
                    headless=self.config.headless,
                )

        if self.browser_runtime and not self.browser_runtime.is_running():
            logger.info(f"[{run_id}] Launching browser runtime...")
            self.browser_runtime.launch()

        # Wire C04 SourceClient if runtime is available and not injected
        if not self.source_client and self.browser_runtime:
            from .source_client import DouyinSourceClient
            self.source_client = DouyinSourceClient(
                runtime_provider=self.browser_runtime,
                config=self.config,
            )

        # Wire C03 AuthStateDetector if runtime is available and not injected
        if not self.auth_detector and self.browser_runtime:
            from .auth_state import DouyinAuthStateDetector
            self.auth_detector = DouyinAuthStateDetector(
                runtime_provider=self.browser_runtime,
                source_client=self.source_client,
                config=self.config,
            )

        if not self.raw_archiver and self.config.raw_archive_root:
            from ..raw_archive import DiskRawArchiver
            self.raw_archiver = DiskRawArchiver(root_dir=self.config.raw_archive_root)

        if not self.transformer:
            from .transform import DouyinCanonicalTransformer
            self.transformer = DouyinCanonicalTransformer()

        if not self.repository and self.config.database_path:
            from ..repository import SqliteMetadataRepository
            self.repository = SqliteMetadataRepository(db_path=self.config.database_path)

    def run_preflight(self, run_id: str) -> Any:
        if not self.auth_detector:
            logger.info(f"[{run_id}] Auth detector not yet injected. Returning default pass.")
            from .auth_state import AuthPreflightResult, AuthReasonCode, AuthState, CollectionAccess, RecoveryAction, SourceHealth
            res = AuthPreflightResult(
                auth_state=AuthState.AUTH_VALID,
                source_health=SourceHealth.HEALTHY,
                collection_access=CollectionAccess.READABLE,
                can_sync=True,
                requires_user_action=False,
                retryable=False,
                reason_code=AuthReasonCode.AUTH_OK.value,
                recovery_action=RecoveryAction.NONE,
            )
            self._last_preflight_res = res
            return res

        if hasattr(self.auth_detector, "detect_auth_state"):
            res = self.auth_detector.detect_auth_state()
            self._last_preflight_res = res
            logger.info(f"[{run_id}] Preflight auth state: {res.auth_state.value}, can_sync={res.can_sync}, reason={res.reason_code}")
            return res

        # Protocol fallback for simple check_auth()
        status = self.auth_detector.check_auth()
        logger.info(f"[{run_id}] Auth state detected via check_auth: {status}")
        from .auth_state import AuthPreflightResult, AuthReasonCode, AuthState, CollectionAccess, RecoveryAction, SourceHealth
        can_sync = (status == "LOGIN_OK")
        return AuthPreflightResult(
            auth_state=AuthState.AUTH_VALID if can_sync else (AuthState.AUTH_CHALLENGE if status == "AUTH_CHALLENGE" else AuthState.AUTH_REQUIRED),
            source_health=SourceHealth.HEALTHY,
            collection_access=CollectionAccess.READABLE if can_sync else CollectionAccess.UNAVAILABLE,
            can_sync=can_sync,
            requires_user_action=not can_sync,
            retryable=False,
            reason_code=AuthReasonCode.AUTH_OK.value if can_sync else AuthReasonCode.LOGIN_REQUIRED.value,
            recovery_action=RecoveryAction.NONE if can_sync else RecoveryAction.MANUAL_LOGIN,
        )

    def preflight_auth_check(self, run_id: str) -> bool:
        res = self.run_preflight(run_id)
        return bool(getattr(res, "can_sync", False))

    def probe(self, run_id: str) -> CollectorRunResult:
        """Lightweight inspection of Douyin collector readiness."""
        started_at = utcnow_iso()
        missing_dependencies = self._get_missing_dependencies()

        return CollectorRunResult(
            run_id=run_id,
            platform=self.platform,
            mode=CollectorMode.PROBE,
            status=CollectorStatus.SUCCESS if not missing_dependencies else CollectorStatus.PARTIAL,
            started_at=started_at,
            finished_at=utcnow_iso(),
            metrics={
                "browser_runtime_ready": self.browser_runtime is not None,
                "auth_detector_ready": self.auth_detector is not None,
                "source_client_ready": self.source_client is not None,
                "raw_archiver_ready": self.raw_archiver is not None,
                "transformer_ready": self.transformer is not None,
                "repository_ready": self.repository is not None,
                "queue_producer_ready": self.queue_producer is not None,
                "missing_dependencies": missing_dependencies,
            },
        )

    def sync(self, run_id: str, **kwargs: Any) -> CollectorRunResult:
        """Incremental synchronization starting from newest items down to watermark."""
        started_at = utcnow_iso()
        missing = self._get_missing_dependencies()

        if missing:
            logger.warning(f"[{run_id}] Sync called but dependencies not ready: {missing}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.SYNC,
                status=CollectorStatus.NOT_IMPLEMENTED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                metrics={"missing_dependencies": missing},
                error={
                    "code": CollectorErrorCode.DEPENDENCY_NOT_READY.value,
                    "message": f"Douyin collector dependencies pending implementation: {missing}. Scheduled in tasks C02-C09.",
                    "details": {"pending_slots": missing},
                },
            )

        # Fallback for lightweight C01 test stubs
        if not hasattr(self.source_client, "fetch_collection_page"):
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.SYNC,
                status=CollectorStatus.SUCCESS,
                started_at=started_at,
                finished_at=utcnow_iso(),
                metrics={"items_synced": 0, "pages_fetched": 0},
            )

        scope_id = kwargs.get("scope_id")
        if not scope_id and hasattr(self, "_last_preflight_res") and self._last_preflight_res:
            scope_id = getattr(self._last_preflight_res, "account_scope_id", None)

        from .sync_engine import DouyinSyncEngine
        engine = DouyinSyncEngine(
            config=self.config,
            repository=self.repository,
            archiver=self.raw_archiver,
            transformer=self.transformer,
            source_client=self.source_client,
            auth_detector=self.auth_detector,
            queue_producer=self.queue_producer,
        )

        try:
            record = engine.sync(
                sync_run_id=run_id,
                scope_id=scope_id,
                mode="incremental",
                page_size=self.config.page_size,
                max_pages=kwargs.get("max_pages"),
                dry_run=bool(kwargs.get("dry_run", False)),
                service_lock_acquired=True,
            )
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.SYNC,
                status=CollectorStatus.SUCCESS,
                started_at=record.started_at,
                finished_at=record.finished_at or utcnow_iso(),
                metrics=record.metrics,
            )
        except Exception as e:
            logger.exception(f"[{run_id}] Sync execution failure: {e}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.SYNC,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error={
                    "code": CollectorErrorCode.RUNTIME_ERROR.value,
                    "message": str(e),
                },
            )

    def backfill(self, run_id: str, limit: int | None = None, **kwargs: Any) -> CollectorRunResult:
        """Historical backfill synchronization."""
        started_at = utcnow_iso()
        missing = self._get_missing_dependencies()

        if missing:
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.BACKFILL,
                status=CollectorStatus.NOT_IMPLEMENTED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                metrics={"missing_dependencies": missing, "limit": limit},
                error={
                    "code": CollectorErrorCode.DEPENDENCY_NOT_READY.value,
                    "message": f"Douyin backfill dependencies pending implementation: {missing}.",
                    "details": {"pending_slots": missing},
                },
            )

        # Fallback for lightweight C01 test stubs
        if not hasattr(self.source_client, "fetch_collection_page"):
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.BACKFILL,
                status=CollectorStatus.SUCCESS,
                started_at=started_at,
                finished_at=utcnow_iso(),
                metrics={"items_backfilled": 0, "limit": limit},
            )

        scope_id = kwargs.get("scope_id")
        if not scope_id and hasattr(self, "_last_preflight_res") and self._last_preflight_res:
            scope_id = getattr(self._last_preflight_res, "account_scope_id", None)

        from .sync_engine import DouyinSyncEngine
        engine = DouyinSyncEngine(
            config=self.config,
            repository=self.repository,
            archiver=self.raw_archiver,
            transformer=self.transformer,
            source_client=self.source_client,
            auth_detector=self.auth_detector,
            queue_producer=self.queue_producer,
        )

        try:
            record = engine.backfill(
                sync_run_id=run_id,
                scope_id=scope_id,
                limit=limit,
                max_pages=kwargs.get("max_pages"),
                dry_run=bool(kwargs.get("dry_run", False)),
                service_lock_acquired=True,
            )
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.BACKFILL,
                status=CollectorStatus.SUCCESS,
                started_at=record.started_at,
                finished_at=record.finished_at or utcnow_iso(),
                metrics=record.metrics,
            )
        except Exception as e:
            logger.exception(f"[{run_id}] Backfill execution failure: {e}")
            return CollectorRunResult(
                run_id=run_id,
                platform=self.platform,
                mode=CollectorMode.BACKFILL,
                status=CollectorStatus.FAILED,
                started_at=started_at,
                finished_at=utcnow_iso(),
                error={
                    "code": CollectorErrorCode.RUNTIME_ERROR.value,
                    "message": str(e),
                },
            )

    def shutdown(self, run_id: str) -> None:
        logger.info(f"[{run_id}] Shutting down DouyinCollector...")
        if self.browser_runtime and self.browser_runtime.is_running():
            self.browser_runtime.close()

    def _get_missing_dependencies(self) -> list[str]:
        missing = []
        if not self.browser_runtime:
            missing.append("BrowserRuntimeProvider (C02)")
        if not self.auth_detector:
            missing.append("AuthStateDetector (C03)")
        if not self.source_client:
            missing.append("SourceClient (C04)")
        if not self.raw_archiver:
            missing.append("RawArchiver (C06)")
        if not self.transformer:
            missing.append("CanonicalTransformer (C07)")
        if not self.repository:
            missing.append("MetadataRepository (C08)")
        return missing
