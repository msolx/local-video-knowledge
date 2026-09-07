"""Downloader Error Taxonomy & Retry Policy Engine (DY-D08).

Pure deterministic policy engine mapping download failure facts from D02, D03,
D04, D05, D06, and D07 into canonical QW-13 error classifications, retry
decisions, and worker health advisories.

Key Invariants:
1. Strict Separation of Concerns:
   - C09 Outbox Delivery Attempts != D08 Download Execution Attempts.
   - D08 does not poll queues, spawn worker threads, or mutate outbox state.
2. Pure Deterministic Logic:
   - Zero time.sleep() inside policy; backoff delays and cooldowns are returned as floats.
   - Clock and RNG are injectable for deterministic testing.
3. Secret-Safe & Serializable:
   - All AttemptRecords and RetryDecisions are scrubbed of secrets and JSON-serializable.
   - Cookies, tokens, and signed URLs are never stored in history records.
4. Bounded Retry Budgets:
   - Validation failure: strictly 1 retry (2 total attempts) per QW-13 frozen contract.
   - Tool/sandbox structural errors: TERMINAL with WORKER_UNHEALTHY advisory.
   - 429 Rate limits: WAIT_AND_RETRY with Retry-After parsing, clamping, and pause_scope.
   - Auth required / challenge: BLOCKED_AUTH without blind rapid retries.
   - Archive conflicts: TERMINAL (never overwrite destination).
"""

from __future__ import annotations

import datetime
import email.utils
import enum
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from src.downloader.contracts import (
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadErrorPolicy,
    DownloadResultContract,
    ErrorClassification,
    scrub_secrets,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Enums & Core Types
# =============================================================================


class RetryAction(str, enum.Enum):
    """Actionable decision for downstream worker scheduler on how to handle a failure."""

    RETRY = "RETRY"
    """Standard retry of download execution with backoff delay."""

    RETRY_AFTER_RERESOLVE = "RETRY_AFTER_RERESOLVE"
    """Re-resolve fresh platform content / CDN URLs before re-acquiring."""

    RETRY_AFTER_CREDENTIAL_REFRESH = "RETRY_AFTER_CREDENTIAL_REFRESH"
    """Discard expired/broken credentials and acquire fresh snapshot from D02 before retrying."""

    WAIT_AND_RETRY = "WAIT_AND_RETRY"
    """Rate-limited; wait out the cooldown (Retry-After) before next attempt."""

    BLOCKED_AUTH = "BLOCKED_AUTH"
    """Authentication required or captcha challenge active; paused pending operator recovery.
    Note: retryable is set to False to halt blind automated retry loops, but this is NOT an
    unrecoverable terminal failure (TERMINAL). The outbox task must not be terminal-ACKed;
    scheduling should resume once authentication is restored.
    """

    TERMINAL = "TERMINAL"
    """Permanent fatal failure; no further retries permitted."""


class WorkerHealthAction(str, enum.Enum):
    """Health advisory for worker daemon execution environment."""

    HEALTHY = "HEALTHY"
    """Host worker node is healthy; failure was remote, transient, or input-specific."""

    PAUSE_SCOPE = "PAUSE_SCOPE"
    """Rate-limit or auth challenge observed; pause scheduling for this account/platform scope."""

    WORKER_UNHEALTHY = "WORKER_UNHEALTHY"
    """Host environment is broken (missing ffmpeg/ffprobe, F2 import failure, disk fault)."""


# Subreasons indicating volatile CDN failures that warrant re-resolving fresh media URLs
_VOLATILE_CDN_SUBREASONS = frozenset(
    {
        "CDN_EXPIRED",
        "STALE_URL",
        "VOLATILE_CDN_404",
        "CDN_URL_EXPIRED",
        "URL_SIGNATURE_EXPIRED",
        "CDN_404",
    }
)

# Subreasons indicating temporary credential bridge issues (retryable with fresh acquire)
_TEMPORARY_BRIDGE_SUBREASONS = frozenset(
    {
        "BRIDGE_DISCONNECTED",
        "SIDECAR_TIMEOUT",
        "BROWSER_CLOSED",
        "TEMPORARY_UNAVAILABLE",
        "HTTP_503_SIDECAR",
        "SIDECAR_CONNECT_ERROR",
    }
)

# Subreasons indicating structural credential failures (never retryable)
_STRUCTURAL_BRIDGE_SUBREASONS = frozenset(
    {
        "ACCOUNT_SCOPE_MISMATCH",
        "SCOPE_NOT_FOUND",
        "SCOPE_MISMATCH",
        "UNAUTHORIZED_SCOPE",
    }
)


# =============================================================================
# 2. Configuration & Attempt Record Models
# =============================================================================


@dataclass(frozen=True)
class RetryPolicyConfig:
    """Configurable budgets and backoff coefficients for download retry policy."""

    max_total_attempts: int = 3
    """Default total attempts allowed (1 initial + 2 retries)."""

    validation_max_attempts: int = 2
    """QW-13 frozen budget: strictly initial + 1 retry for validation failures."""

    media_incomplete_max_attempts: int = 2
    """Maximum attempts allowed for incomplete media output before terminal."""

    server_error_max_attempts: int = 3
    """Maximum attempts allowed for upstream 5xx errors."""

    network_error_max_attempts: int = 3
    """Maximum attempts allowed for network timeouts and disconnects."""

    credential_bridge_max_attempts: int = 2
    """Maximum attempts allowed for transient credential bridge connection issues."""

    unknown_max_attempts: int = 2
    """Maximum attempts allowed for unclassified unknown errors."""

    # Backoff coefficients
    base_backoff_seconds: float = 2.0
    backoff_factor: float = 2.0
    max_backoff_seconds: float = 60.0
    jitter_ratio: float = 0.2

    # Rate limiting coefficients
    default_rate_limit_cooldown_seconds: float = 60.0
    min_retry_after_seconds: float = 1.0
    max_retry_after_seconds: float = 300.0


@dataclass(frozen=True)
class AttemptRecord:
    """Historical telemetry record of an individual physical download attempt.

    STRICT INVARIANT: Never records cookies, secret tokens, or signed transient URLs.
    """

    attempt_number: int
    execution_id: str
    started_at: float
    finished_at: float
    result_status: str
    error_code: str | None = None
    subreason: str | None = None
    retry_decision: str | None = None
    delay_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "execution_id": self.execution_id,
            "started_at": round(self.started_at, 3),
            "finished_at": round(self.finished_at, 3),
            "result_status": self.result_status,
            "error_code": self.error_code,
            "subreason": scrub_secrets(self.subreason or ""),
            "retry_decision": self.retry_decision,
            "delay_seconds": round(self.delay_seconds, 3),
        }


@dataclass(frozen=True)
class RetryDecision:
    """Authoritative decision on whether and how to retry a failed download attempt."""

    action: RetryAction
    retryable: bool
    delay_seconds: float
    reason: str
    error_code: DownloaderErrorCode
    subreason: str = ""
    attempt_number: int = 1
    max_attempts: int = 3
    requires_reresolve: bool = False
    requires_credential_refresh: bool = False
    pause_scope: str | None = None
    worker_action: WorkerHealthAction = WorkerHealthAction.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "retryable": self.retryable,
            "delay_seconds": round(self.delay_seconds, 3),
            "reason": scrub_secrets(self.reason),
            "error_code": self.error_code.value,
            "subreason": scrub_secrets(self.subreason),
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
            "requires_reresolve": self.requires_reresolve,
            "requires_credential_refresh": self.requires_credential_refresh,
            "pause_scope": self.pause_scope,
            "worker_action": self.worker_action.value,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class DownloadFailureFact:
    """Normalized representation of failure evidence across all downloader subsystems."""

    error_code: DownloaderErrorCode
    subreason: str = ""
    message: str = ""
    retry_after: float | str | None = None
    scope_id: str | None = None
    platform: str = "douyin"
    platform_content_id: str = ""
    task_id: str = ""
    execution_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# 3. Pure Calculation Utilities (Backoff, Jitter, Retry-After)
# =============================================================================


def parse_retry_after(
    header_val: str | int | float | None,
    now_fn: Callable[[], float] | None = None,
    min_sec: float = 1.0,
    max_sec: float = 300.0,
    fallback: float = 60.0,
) -> float:
    """Safely parses a Retry-After value (delta-seconds or HTTP-date) with clamping.

    Handles malformed, negative, NaN, infinite, or excessively large values safely.
    """
    if header_val is None:
        return fallback

    # Case 1: Numeric float / int directly passed
    if isinstance(header_val, (int, float)):
        if math.isnan(header_val) or math.isinf(header_val) or header_val <= 0:
            return fallback
        return min(max_sec, max(min_sec, float(header_val)))

    # Case 2: String value
    raw = str(header_val).strip()
    if not raw:
        return fallback

    # Try delta-seconds integer/float format
    try:
        val = float(raw)
        if math.isnan(val) or math.isinf(val) or val <= 0:
            return fallback
        return min(max_sec, max(min_sec, val))
    except ValueError:
        pass

    # Try HTTP-date format (e.g. "Wed, 21 Oct 2026 07:28:00 GMT")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        target_ts = dt.timestamp()
        current_ts = now_fn() if now_fn else time.time()
        delta = target_ts - current_ts
        if delta <= 0:
            return fallback
        return min(max_sec, max(min_sec, delta))
    except Exception:
        return fallback


def compute_backoff(
    attempt_number: int,
    base_delay: float = 2.0,
    factor: float = 2.0,
    max_delay: float = 60.0,
    jitter_ratio: float = 0.0,
    rng: Callable[[], float] | None = None,
) -> float:
    """Calculates deterministic bounded exponential backoff with optional injected jitter.

    attempt_number is 1-based (attempt 1 failing means 1 retry completed, next is attempt 2).
    """
    # Nominal backoff: base_delay * factor^(attempt_number - 1)
    exponent = max(0, attempt_number - 1)
    nominal = base_delay * (factor ** exponent)
    nominal = min(max_delay, nominal)

    if jitter_ratio > 0.0 and rng is not None:
        u = rng()  # Expected float in [0.0, 1.0]
        variation = (u * 2.0 - 1.0) * jitter_ratio
        delay = nominal * (1.0 + variation)
    else:
        delay = nominal

    return max(0.0, round(delay, 3))


# =============================================================================
# 4. Production Download Error Policy Engine
# =============================================================================


class ProductionDownloadErrorPolicy(DownloadErrorPolicy):
    """Production canonical error classification and retry decision engine (DY-D08).

    Implements:
    1. Port 7 DownloadErrorPolicy protocol: classify_error(error) -> ErrorClassification
    2. Full decision engine: decide(failure, attempt_history, context) -> RetryDecision
    """

    def __init__(
        self,
        config: RetryPolicyConfig | None = None,
        clock: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or RetryPolicyConfig()
        self._clock = clock or time.time
        self._rng = rng or random.random

    # -------------------------------------------------------------------------
    # Port 7 Protocol Conformance (DY-D01 Compatible)
    # -------------------------------------------------------------------------

    def classify_error(self, error: Exception | str) -> ErrorClassification:
        """Classifies an exception or message into the canonical 15-code taxonomy."""
        fact = self._normalize_failure(error)
        decision = self.decide(fact, attempt_history=1)
        return ErrorClassification(
            error_code=fact.error_code,
            retryable=decision.retryable,
            retry_after=int(decision.delay_seconds) if decision.delay_seconds > 0 else None,
            user_action=decision.reason,
        )

    # -------------------------------------------------------------------------
    # D08 Pure Deterministic Decision Engine
    # -------------------------------------------------------------------------

    def decide(
        self,
        failure: DownloadFailureFact | DownloadResultContract | Exception | dict[str, Any] | str,
        attempt_history: Sequence[AttemptRecord] | int = 1,
        context: dict[str, Any] | None = None,
    ) -> RetryDecision:
        """Evaluates failure evidence and returns an authoritative RetryDecision.

        Deterministic, pure-logic evaluation:
        - Never sleeps or waits.
        - Preserves task_id, provides worker health advisories.
        - Strictly enforces frozen attempt budgets.
        """
        fact = self._normalize_failure(failure, context=context)

        # Resolve attempt number
        if isinstance(attempt_history, int):
            attempt_number = max(1, attempt_history)
        elif isinstance(attempt_history, Sequence):
            attempt_number = len(attempt_history) + 1
        else:
            attempt_number = 1

        code = fact.error_code
        sub = fact.subreason.upper()
        msg_clean = scrub_secrets(fact.message)
        scope_pause = f"{fact.platform}:{fact.scope_id}" if fact.scope_id else f"platform:{fact.platform}"

        # ---------------------------------------------------------------------
        # Rule 1: INVALID INPUT (Section L)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_INVALID_INPUT:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason="Invalid task schema, illegal URL, or corrupted input parameters. Terminal failure.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=1,
            )

        # ---------------------------------------------------------------------
        # Rule 2: UNSUPPORTED CONTENT (Section M)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason="Content type is not supported by the downloader worker engine. Terminal failure.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=1,
            )

        # ---------------------------------------------------------------------
        # Rule 3: NOT FOUND / DELETED (Section N)
        # ---------------------------------------------------------------------
        if code in (DownloaderErrorCode.DOWNLOAD_NOT_FOUND, DownloaderErrorCode.DOWNLOAD_UNAVAILABLE_DELETED):
            # Check for volatile CDN 404 (stale signed link) vs permanent platform deletion
            is_volatile = (
                sub in _VOLATILE_CDN_SUBREASONS
                or "CDN" in sub
                or "STALE" in sub
                or "EXPIRED" in sub
            )

            if is_volatile:
                budget = self.config.media_incomplete_max_attempts
                if attempt_number < budget:
                    delay = compute_backoff(
                        attempt_number,
                        base_delay=self.config.base_backoff_seconds,
                        factor=self.config.backoff_factor,
                        max_delay=self.config.max_backoff_seconds,
                        jitter_ratio=self.config.jitter_ratio,
                        rng=self._rng,
                    )
                    return RetryDecision(
                        action=RetryAction.RETRY_AFTER_RERESOLVE,
                        retryable=True,
                        delay_seconds=delay,
                        reason="Volatile CDN resource returned 404; re-resolving fresh media URL before retry.",
                        error_code=code,
                        subreason=fact.subreason,
                        attempt_number=attempt_number,
                        max_attempts=budget,
                        requires_reresolve=True,
                    )
                else:
                    return RetryDecision(
                        action=RetryAction.TERMINAL,
                        retryable=False,
                        delay_seconds=0.0,
                        reason=f"Volatile CDN resource re-resolve budget exhausted ({budget} attempts). Terminal.",
                        error_code=code,
                        subreason=fact.subreason,
                        attempt_number=attempt_number,
                        max_attempts=budget,
                    )

            # Permanent platform deletion / not found
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason="Content permanently deleted, private, or non-existent on platform. Terminal failure.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=1,
            )

        # ---------------------------------------------------------------------
        # Rule 4: AUTH REQUIRED & AUTH CHALLENGE (Section O, P)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED:
            return RetryDecision(
                action=RetryAction.BLOCKED_AUTH,
                retryable=False,
                delay_seconds=0.0,
                reason="Platform session unauthenticated or expired. Paused waiting for operator re-login.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=attempt_number,
                pause_scope=scope_pause,
                worker_action=WorkerHealthAction.PAUSE_SCOPE,
            )

        if code == DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE:
            return RetryDecision(
                action=RetryAction.BLOCKED_AUTH,
                retryable=False,
                delay_seconds=0.0,
                reason="Platform security verification challenge / captcha triggered. Paused waiting for human solve.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=attempt_number,
                pause_scope=scope_pause,
                worker_action=WorkerHealthAction.PAUSE_SCOPE,
            )

        # ---------------------------------------------------------------------
        # Rule 5: CREDENTIAL BRIDGE FAILED (Section Q)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED:
            # Check if structural (scope mismatch) vs temporary (sidecar disconnect)
            is_structural = (
                sub in _STRUCTURAL_BRIDGE_SUBREASONS
                or "MISMATCH" in sub
                or "NOT_FOUND" in sub
            )

            if is_structural:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Structural credential binding failure ({fact.subreason or 'scope mismatch'}). Terminal.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=1,
                )

            # Temporary bridge issue
            budget = self.config.credential_bridge_max_attempts
            if attempt_number < budget:
                delay = compute_backoff(
                    attempt_number,
                    base_delay=1.0,
                    factor=self.config.backoff_factor,
                    max_delay=10.0,
                    jitter_ratio=0.0,
                )
                return RetryDecision(
                    action=RetryAction.RETRY_AFTER_CREDENTIAL_REFRESH,
                    retryable=True,
                    delay_seconds=delay,
                    reason="Credential bridge temporarily unavailable; re-acquiring credential snapshot.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                    requires_credential_refresh=True,
                )
            else:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Credential bridge connection budget exhausted ({budget} attempts). Terminal.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )

        # ---------------------------------------------------------------------
        # Rule 6: RATE LIMITED (429) (Section R, S)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_RATE_LIMITED:
            cooldown = parse_retry_after(
                fact.retry_after,
                now_fn=self._clock,
                min_sec=self.config.min_retry_after_seconds,
                max_sec=self.config.max_retry_after_seconds,
                fallback=self.config.default_rate_limit_cooldown_seconds,
            )
            return RetryDecision(
                action=RetryAction.WAIT_AND_RETRY,
                retryable=True,
                delay_seconds=cooldown,
                reason=f"Upstream rate limit (429) encountered. Cooldown of {cooldown:.1f}s enforced.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=self.config.max_total_attempts,
                pause_scope=scope_pause,
                worker_action=WorkerHealthAction.PAUSE_SCOPE,
            )

        # ---------------------------------------------------------------------
        # Rule 7: NETWORK ERROR & TIMEOUT (Section T, U)
        # ---------------------------------------------------------------------
        if code in (DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR, DownloaderErrorCode.DOWNLOAD_TIMEOUT):
            budget = self.config.network_error_max_attempts
            if attempt_number < budget:
                delay = compute_backoff(
                    attempt_number,
                    base_delay=self.config.base_backoff_seconds,
                    factor=self.config.backoff_factor,
                    max_delay=self.config.max_backoff_seconds,
                    jitter_ratio=self.config.jitter_ratio,
                    rng=self._rng,
                )
                return RetryDecision(
                    action=RetryAction.RETRY,
                    retryable=True,
                    delay_seconds=delay,
                    reason=f"Transient network connectivity error or timeout. Retrying in {delay:.1f}s.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )
            else:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Network error retry budget exhausted ({budget} attempts). Terminal.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )

        # ---------------------------------------------------------------------
        # Rule 8: SERVER ERROR (5xx) (Section V)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_SERVER_ERROR:
            budget = self.config.server_error_max_attempts
            if attempt_number < budget:
                delay = compute_backoff(
                    attempt_number,
                    base_delay=self.config.base_backoff_seconds,
                    factor=self.config.backoff_factor,
                    max_delay=self.config.max_backoff_seconds,
                    jitter_ratio=self.config.jitter_ratio,
                    rng=self._rng,
                )
                return RetryDecision(
                    action=RetryAction.RETRY,
                    retryable=True,
                    delay_seconds=delay,
                    reason=f"Upstream server error (5xx). Retrying in {delay:.1f}s.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )
            else:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Server error retry budget exhausted ({budget} attempts). Terminal.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )

        # ---------------------------------------------------------------------
        # Rule 9: MEDIA INCOMPLETE (Section X)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE:
            budget = self.config.media_incomplete_max_attempts
            if attempt_number < budget:
                delay = compute_backoff(
                    attempt_number,
                    base_delay=self.config.base_backoff_seconds,
                    factor=self.config.backoff_factor,
                    max_delay=self.config.max_backoff_seconds,
                    jitter_ratio=0.0,
                )
                return RetryDecision(
                    action=RetryAction.RETRY_AFTER_RERESOLVE,
                    retryable=True,
                    delay_seconds=delay,
                    reason="Media artifacts incomplete in sandbox; re-resolving fresh stream before re-acquire.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                    requires_reresolve=True,
                )
            else:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Media incomplete retry budget exhausted ({budget} attempts). Terminal.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )

        # ---------------------------------------------------------------------
        # Rule 10: ARCHIVE CONFLICT & PROMOTION ERRORS (Section AA, AR)
        # ---------------------------------------------------------------------
        if (
            "CONFLICT" in sub
            or "ARCHIVE_CONFLICT" in sub
            or "PROMOTION_CONFLICT" in sub
            or "conflict" in msg_clean.lower()
        ):
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason="Archive conflict detected at destination with different content hash. Operator attention required.",
                error_code=code,
                subreason=fact.subreason or "ARCHIVE_CONFLICT",
                attempt_number=attempt_number,
                max_attempts=1,
            )

        # ---------------------------------------------------------------------
        # Rule 11: VALIDATION FAILED (Section Y, AP)
        # STRICT QW-13 FROZEN BUDGET: Initial attempt + 1 retry = 2 total attempts
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED:
            budget = self.config.validation_max_attempts  # Exactly 2
            if attempt_number < budget:
                delay = compute_backoff(
                    attempt_number,
                    base_delay=self.config.base_backoff_seconds,
                    factor=self.config.backoff_factor,
                    max_delay=self.config.max_backoff_seconds,
                    jitter_ratio=0.0,
                )
                return RetryDecision(
                    action=RetryAction.RETRY_AFTER_RERESOLVE,
                    retryable=True,
                    delay_seconds=delay,
                    reason="Media validation failed (corrupt container / stream); re-resolving fresh asset once.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                    requires_reresolve=True,
                )
            else:
                return RetryDecision(
                    action=RetryAction.TERMINAL,
                    retryable=False,
                    delay_seconds=0.0,
                    reason=f"Media validation failed on retry attempt ({attempt_number}/{budget}). Terminal per QW-13.",
                    error_code=code,
                    subreason=fact.subreason,
                    attempt_number=attempt_number,
                    max_attempts=budget,
                )

        # ---------------------------------------------------------------------
        # Rule 12: TOOL & ENVIRONMENT STRUCTURAL ERRORS (Section Z)
        # ---------------------------------------------------------------------
        if code in (
            DownloaderErrorCode.DOWNLOAD_TOOL_ERROR,
            DownloaderErrorCode.SANDBOX_CREATE_FAILED,
            DownloaderErrorCode.SANDBOX_PATH_ESCAPE,
            DownloaderErrorCode.SANDBOX_METADATA_CORRUPT,
        ):
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason=f"Host execution tool or filesystem environment error ({fact.subreason or 'tool error'}). Terminal.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=1,
                worker_action=WorkerHealthAction.WORKER_UNHEALTHY,
            )

        # ---------------------------------------------------------------------
        # Rule 13: PERMISSION DENIED (403)
        # ---------------------------------------------------------------------
        if code == DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason="Platform permission denied (403). Content private or unauthorized. Terminal failure.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=1,
            )

        # ---------------------------------------------------------------------
        # Rule 14: UNKNOWN / UNCLASSIFIED (Section AB)
        # ---------------------------------------------------------------------
        budget = self.config.unknown_max_attempts
        if attempt_number < budget:
            delay = compute_backoff(
                attempt_number,
                base_delay=self.config.base_backoff_seconds,
                factor=self.config.backoff_factor,
                max_delay=self.config.max_backoff_seconds,
                jitter_ratio=0.0,
            )
            return RetryDecision(
                action=RetryAction.RETRY,
                retryable=True,
                delay_seconds=delay,
                reason="Unclassified error encountered. Single conservative retry permitted.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=budget,
            )
        else:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                retryable=False,
                delay_seconds=0.0,
                reason=f"Unknown error retry budget exhausted ({budget} attempts). Terminal failure.",
                error_code=code,
                subreason=fact.subreason,
                attempt_number=attempt_number,
                max_attempts=budget,
            )

    # -------------------------------------------------------------------------
    # Internal Helpers: Failure Normalization
    # -------------------------------------------------------------------------

    def _normalize_failure(
        self,
        failure: Any,
        context: dict[str, Any] | None = None,
    ) -> DownloadFailureFact:
        """Coerces any failure artifact into a clean DownloadFailureFact."""
        ctx = context or {}

        if isinstance(failure, DownloadFailureFact):
            return failure

        if isinstance(failure, DownloadResultContract):
            # Extract from formal contract
            code_enum = DownloaderErrorCode.DOWNLOAD_UNKNOWN
            if failure.error_code:
                try:
                    code_enum = DownloaderErrorCode(failure.error_code)
                except ValueError:
                    pass

            sub = ""
            if failure.validation and failure.validation.get("error"):
                sub = str(failure.validation.get("error"))

            return DownloadFailureFact(
                error_code=code_enum,
                subreason=sub,
                message=failure.message or "",
                retry_after=failure.retry_after,
                scope_id=failure.scope_id or ctx.get("scope_id"),
                platform=ctx.get("platform", "douyin"),
                platform_content_id=failure.platform_content_id or ctx.get("platform_content_id", ""),
                task_id=failure.task_id or ctx.get("task_id", ""),
            )

        if isinstance(failure, Exception):
            msg = str(failure)
            cls_name = failure.__class__.__name__
            if "ArchiveConflict" in cls_name:
                sub = "ARCHIVE_CONFLICT"
            else:
                sub = getattr(failure, "subreason", "") or getattr(failure, "reason", "") or cls_name
            if hasattr(failure, "error_code") and isinstance(failure.error_code, DownloaderErrorCode):
                code = failure.error_code
            else:
                code = self._infer_code_from_string(msg, sub)
            return DownloadFailureFact(
                error_code=code,
                subreason=sub,
                message=msg,
                scope_id=ctx.get("scope_id"),
                platform=ctx.get("platform", "douyin"),
                platform_content_id=ctx.get("platform_content_id", ""),
                task_id=ctx.get("task_id", ""),
            )

        if isinstance(failure, dict):
            raw_code = failure.get("error_code") or failure.get("code")
            code_enum = DownloaderErrorCode.DOWNLOAD_UNKNOWN
            if raw_code:
                try:
                    code_enum = DownloaderErrorCode(raw_code)
                except ValueError:
                    code_enum = self._infer_code_from_string(str(raw_code), "")
            else:
                code_enum = self._infer_code_from_string(str(failure.get("message", "")), str(failure.get("subreason", "")))

            return DownloadFailureFact(
                error_code=code_enum,
                subreason=str(failure.get("subreason", "")),
                message=str(failure.get("message", "")),
                retry_after=failure.get("retry_after"),
                scope_id=failure.get("scope_id") or ctx.get("scope_id"),
                platform=failure.get("platform") or ctx.get("platform", "douyin"),
                platform_content_id=failure.get("platform_content_id") or ctx.get("platform_content_id", ""),
                task_id=failure.get("task_id") or ctx.get("task_id", ""),
                details=failure.get("details", {}),
            )

        # Fallback string
        msg = str(failure)
        code = self._infer_code_from_string(msg, "")
        return DownloadFailureFact(
            error_code=code,
            subreason="",
            message=msg,
            scope_id=ctx.get("scope_id"),
            platform=ctx.get("platform", "douyin"),
            platform_content_id=ctx.get("platform_content_id", ""),
            task_id=ctx.get("task_id", ""),
        )

    def _infer_code_from_string(self, msg: str, sub: str) -> DownloaderErrorCode:
        """Infers canonical error code from raw string diagnostics."""
        combined = f"{msg} {sub}".lower()

        # Input errors
        if any(k in combined for k in ["invalid_input", "malformed", "illegal domain", "id mismatch", "schema error"]):
            return DownloaderErrorCode.DOWNLOAD_INVALID_INPUT
        if any(k in combined for k in ["unsupported", "live stream", "vip content"]):
            return DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT

        # Auth errors
        if any(k in combined for k in ["auth_challenge", "captcha", "slider", "验证码", "风控验证", "security check"]):
            return DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE
        if any(k in combined for k in ["auth_required", "need login", "session expired", "unauthenticated", "未登录"]):
            return DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED
        if any(k in combined for k in ["credential bridge", "credential_bridge", "scope mismatch", "sidecar disconnect"]):
            return DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED

        # Rate limits
        if any(k in combined for k in ["429", "rate limit", "too many requests", "frequency limit"]):
            return DownloaderErrorCode.DOWNLOAD_RATE_LIMITED

        # Not found / deleted
        if any(k in combined for k in ["deleted", "taken down", "unavailable_deleted", "作品已被删除", "作品已被封禁"]):
            return DownloaderErrorCode.DOWNLOAD_UNAVAILABLE_DELETED
        if any(k in combined for k in ["404", "not found", "aweme_id not found", "notice_code == 2048", "empty detail"]):
            return DownloaderErrorCode.DOWNLOAD_NOT_FOUND

        # Permission
        if any(k in combined for k in ["403", "permission denied", "forbidden", "private_status"]):
            return DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED

        # Server error
        if any(k in combined for k in ["500", "502", "503", "504", "server error", "gateway timeout"]):
            return DownloaderErrorCode.DOWNLOAD_SERVER_ERROR

        # Network / Timeout
        if any(k in combined for k in ["timeout", "timed out", "connect timeout"]):
            return DownloaderErrorCode.DOWNLOAD_TIMEOUT
        if any(k in combined for k in ["connection", "network", "socket", "dns", "ssl", "econnreset"]):
            return DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR

        # Validation & Tool
        if any(k in combined for k in ["validation", "corrupt", "ffprobe", "decode failed", "hash mismatch"]):
            return DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED
        if any(k in combined for k in ["incomplete", "missing member", "zero files", "no media files"]):
            return DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        if any(k in combined for k in ["tool error", "ffmpeg missing", "f2 import", "executable broken", "winerror"]):
            return DownloaderErrorCode.DOWNLOAD_TOOL_ERROR

        return DownloaderErrorCode.DOWNLOAD_UNKNOWN
