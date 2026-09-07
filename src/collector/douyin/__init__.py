"""Douyin collection ingestion package."""

from .auth_state import (
    AuthPreflightResult,
    AuthReasonCode,
    AuthState,
    CollectionAccess,
    DouyinAuthStateDetector,
    PageSignalsEvidence,
    RecoveryAction,
    SourceHealth,
)
from .browser_runtime import BrowserSession, DouyinBrowserRuntimeProvider, ProfileRuntimeLock
from .collector import DouyinCollector
from .config import DouyinCollectorConfig
from .source_client import (
    CollectionPage,
    DouyinSourceClient,
    SourceClientError,
    SourceClientErrorCode,
    SourceHttpError,
    SourceInvalidRequestError,
    SourcePlatformError,
    SourceProbeEvidence,
    SourceResponseInvalidError,
    SourceTimeoutError,
)

from .transform import (
    CanonicalTransformResult,
    DouyinCanonicalTransformer,
    PriorCollectionState,
    RawRefContext,
    TransformContext,
    TransformError,
    TransformInvalidRawError,
    TransformMissingIdentityError,
    TransformSchemaValidationError,
)
from .sync_engine import DouyinSyncEngine, generate_sync_run_id
from .sync_policy import (
    IncrementalSyncPolicy,
    PageFacts,
    StopAction,
    StopDecision,
    StopReasonCode,
    is_watermark_reached,
)

__all__ = [
    "DiskRawArchiver",
    "RawArchiveRef",
    "DouyinCollector",
    "DouyinCollectorConfig",
    "DouyinBrowserRuntimeProvider",
    "ProfileRuntimeLock",
    "BrowserSession",
    "DouyinSourceClient",
    "CollectionPage",
    "SourceProbeEvidence",
    "SourceClientError",
    "SourceClientErrorCode",
    "SourceHttpError",
    "SourceInvalidRequestError",
    "SourcePlatformError",
    "SourceResponseInvalidError",
    "SourceTimeoutError",
    "DouyinAuthStateDetector",
    "AuthState",
    "SourceHealth",
    "CollectionAccess",
    "RecoveryAction",
    "AuthReasonCode",
    "AuthPreflightResult",
    "PageSignalsEvidence",
    "DouyinCanonicalTransformer",
    "TransformContext",
    "PriorCollectionState",
    "RawRefContext",
    "CanonicalTransformResult",
    "TransformError",
    "TransformInvalidRawError",
    "TransformMissingIdentityError",
    "TransformSchemaValidationError",
    "DouyinSyncEngine",
    "generate_sync_run_id",
    "IncrementalSyncPolicy",
    "PageFacts",
    "StopAction",
    "StopDecision",
    "StopReasonCode",
    "is_watermark_reached",
]


