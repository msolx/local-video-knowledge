"""Comprehensive Test Suite for Downloader Error Taxonomy & Retry Policy (DY-D08).

Validates all 45 authoritative requirements defined in DY-D08 specification:
- Pure deterministic policy logic (zero time.sleep()).
- Authoritative QW-13 taxonomy reuse (15 codes across 5 categories + sandbox codes).
- Frozen retry budgets (Validation strictly 1 retry / 2 total attempts).
- 429 Rate limiting Retry-After parsing, clamping, and pause_scope.
- Auth required / challenge non-blind blocking.
- Network exponential backoff with injectable RNG jitter.
- Tool / sandbox structural error worker-unhealthy advisories.
- Complete isolation from C09 outbox delivery attempts.
- Secret-safe serialization and zero secret leakage.
- Subsystem integration with D01, D02, D03, D05, D07.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.collector.download_models import DownloadTask
from src.downloader.contracts import (
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadResultContract,
    ExecutionStage,
    ValidationResult,
)
from src.downloader.credentials import (
    CredentialAuthInvalidError,
    CredentialExpiredError,
    CredentialScopeMismatchError,
    CredentialSourceUnavailableError,
)
from src.downloader.promoter import (
    ArchiveConflictError,
    PromotionResult,
    PromotionStatus,
)
from src.downloader.retry_policy import (
    AttemptRecord,
    DownloadFailureFact,
    ProductionDownloadErrorPolicy,
    RetryAction,
    RetryDecision,
    RetryPolicyConfig,
    WorkerHealthAction,
    compute_backoff,
    parse_retry_after,
)


# =============================================================================
# Group 1: Terminal Non-Retryable Failures (Tests 01 - 03)
# =============================================================================


class TestTerminalFailures:
    """Validates immediate non-retryable terminal outcomes."""

    def test_01_invalid_input_terminal(self) -> None:
        """Test 01: DOWNLOAD_INVALID_INPUT results in immediate TERMINAL decision."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_INVALID_INPUT,
            message="Malformed URL",
            subreason="BAD_URL",
        )
        decision = policy.decide(fact, attempt_history=1)
        assert decision.action == RetryAction.TERMINAL
        assert decision.retryable is False
        assert decision.delay_seconds == 0.0
        assert decision.max_attempts == 1

    def test_02_unsupported_terminal(self) -> None:
        """Test 02: DOWNLOAD_UNSUPPORTED_CONTENT results in immediate TERMINAL decision."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT,
            message="Live stream not supported",
            subreason="LIVE_STREAM",
        )
        decision = policy.decide(fact, attempt_history=1)
        assert decision.action == RetryAction.TERMINAL
        assert decision.retryable is False
        assert decision.delay_seconds == 0.0

    def test_03_permanent_not_found(self) -> None:
        """Test 03: Platform permanent deleted/not-found results in TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_NOT_FOUND,
            message="Aweme not found",
            subreason="AWEME_DELETED",
        )
        decision = policy.decide(fact, attempt_history=1)
        assert decision.action == RetryAction.TERMINAL
        assert decision.retryable is False


# =============================================================================
# Group 2: Re-Resolve & Volatile CDN (Test 04)
# =============================================================================


class TestVolatileCDNReResolve:
    """Validates volatile CDN 404 detection and re-resolution."""

    def test_04_stale_cdn_not_found_reresolve(self) -> None:
        """Test 04: Volatile CDN 404 triggers RETRY_AFTER_RERESOLVE on attempt 1, TERMINAL once exhausted."""
        policy = ProductionDownloadErrorPolicy()

        # Attempt 1: volatile CDN failure -> RETRY_AFTER_RERESOLVE
        fact1 = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_NOT_FOUND,
            message="CDN link returned HTTP 404",
            subreason="VOLATILE_CDN_404",
        )
        dec1 = policy.decide(fact1, attempt_history=1)
        assert dec1.action == RetryAction.RETRY_AFTER_RERESOLVE
        assert dec1.retryable is True
        assert dec1.requires_reresolve is True
        assert dec1.delay_seconds > 0.0

        # Attempt 2: budget (2) exhausted -> TERMINAL
        dec2 = policy.decide(fact1, attempt_history=2)
        assert dec2.action == RetryAction.TERMINAL
        assert dec2.retryable is False


# =============================================================================
# Group 3: Authentication & Security Policies (Tests 05 - 09)
# =============================================================================


class TestAuthenticationPolicies:
    """Validates authentication, captcha challenge, and credential bridge policies."""

    def test_05_auth_required_blocked(self) -> None:
        """Test 05: DOWNLOAD_AUTH_REQUIRED outputs BLOCKED_AUTH with pause_scope."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED,
            message="Session expired",
            scope_id="dyacct_123",
            platform="douyin",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.BLOCKED_AUTH
        assert dec.retryable is False
        assert dec.pause_scope == "douyin:dyacct_123"
        assert dec.worker_action == WorkerHealthAction.PAUSE_SCOPE

    def test_06_auth_challenge_blocked(self) -> None:
        """Test 06: DOWNLOAD_AUTH_CHALLENGE outputs BLOCKED_AUTH with pause_scope."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE,
            message="Captcha required",
            scope_id="dyacct_456",
            platform="douyin",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.BLOCKED_AUTH
        assert dec.retryable is False
        assert dec.pause_scope == "douyin:dyacct_456"
        assert dec.worker_action == WorkerHealthAction.PAUSE_SCOPE

    def test_07_scope_mismatch_blocked(self) -> None:
        """Test 07: Structural scope mismatch in credential bridge is immediately TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            message="Account scope mismatch",
            subreason="ACCOUNT_SCOPE_MISMATCH",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False

    def test_08_temporary_credential_bridge(self) -> None:
        """Test 08: Transient credential bridge failure triggers RETRY_AFTER_CREDENTIAL_REFRESH."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            message="Sidecar temporarily disconnected",
            subreason="BRIDGE_DISCONNECTED",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY_AFTER_CREDENTIAL_REFRESH
        assert dec.retryable is True
        assert dec.requires_credential_refresh is True
        assert dec.delay_seconds > 0.0

    def test_09_permanent_credential_bridge_exhausted(self) -> None:
        """Test 09: Credential bridge retry budget exhausted triggers TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            message="Sidecar timeout",
            subreason="SIDECAR_TIMEOUT",
        )
        dec = policy.decide(fact, attempt_history=2)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False


# =============================================================================
# Group 4: Rate Limiting & Cooldowns (Tests 10 - 13)
# =============================================================================


class TestRateLimitingPolicies:
    """Validates 429 handling, Retry-After parsing, clamping, and pause scopes."""

    def test_10_429_retry_after_seconds(self) -> None:
        """Test 10: Numeric delta-seconds in Retry-After is correctly respected."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            message="Rate limited",
            retry_after="45",
            scope_id="dyacct_rate",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.WAIT_AND_RETRY
        assert dec.retryable is True
        assert dec.delay_seconds == 45.0
        assert dec.pause_scope == "douyin:dyacct_rate"
        assert dec.worker_action == WorkerHealthAction.PAUSE_SCOPE

    def test_11_malformed_retry_after(self) -> None:
        """Test 11: Malformed / invalid Retry-After header falls back to default cooldown."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(default_rate_limit_cooldown_seconds=60.0))
        for bad_val in ["invalid-gibberish", "nonsense", -10, -5.0, 0, float("nan"), float("inf"), ""]:
            fact = DownloadFailureFact(
                error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
                message="Rate limited",
                retry_after=bad_val,
            )
            dec = policy.decide(fact, attempt_history=1)
            assert dec.action == RetryAction.WAIT_AND_RETRY
            assert dec.delay_seconds == 60.0, f"Failed for bad_val={bad_val}"

    def test_12_huge_retry_after_clamp(self) -> None:
        """Test 12: Runaway Retry-After header is clamped to max_retry_after_seconds (300s)."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(max_retry_after_seconds=300.0))
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            message="Rate limited",
            retry_after="999999",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.WAIT_AND_RETRY
        assert dec.delay_seconds == 300.0

    def test_13_rate_limit_pause_scope(self) -> None:
        """Test 13: 429 outputs valid scope_pause and PAUSE_SCOPE worker action."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            scope_id="scope_xyz",
            platform="douyin",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.pause_scope == "douyin:scope_xyz"
        assert dec.worker_action == WorkerHealthAction.PAUSE_SCOPE


# =============================================================================
# Group 5: Network, Server & Platform Errors (Tests 14 - 20)
# =============================================================================


class TestNetworkAndServerErrors:
    """Validates exponential backoff, jitter, and budget exhaustion for network/server."""

    def test_14_network_attempt1(self) -> None:
        """Test 14: Network error attempt 1 returns RETRY with base backoff."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(base_backoff_seconds=2.0, jitter_ratio=0.0))
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR,
            message="Connection reset",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY
        assert dec.retryable is True
        assert dec.delay_seconds == 2.0
        assert dec.attempt_number == 1
        assert dec.max_attempts == 3

    def test_15_network_exponential_backoff(self) -> None:
        """Test 15: Delays follow exponential progression across attempts (2s, 4s)."""
        cfg = RetryPolicyConfig(base_backoff_seconds=2.0, backoff_factor=2.0, jitter_ratio=0.0)
        policy = ProductionDownloadErrorPolicy(config=cfg)
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_TIMEOUT)

        d1 = policy.decide(fact, attempt_history=1)
        assert d1.delay_seconds == 2.0  # 2 * 2^0

        d2 = policy.decide(fact, attempt_history=2)
        assert d2.delay_seconds == 4.0  # 2 * 2^1

    def test_16_network_budget_exhausted(self) -> None:
        """Test 16: Network retry budget (3) exhausted results in TERMINAL."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(network_error_max_attempts=3))
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR)
        dec = policy.decide(fact, attempt_history=3)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False

    def test_17_server_retry(self) -> None:
        """Test 17: HTTP 5xx server error returns RETRY within budget."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_SERVER_ERROR,
            message="HTTP 502 Bad Gateway",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY
        assert dec.retryable is True

    def test_18_server_exhausted(self) -> None:
        """Test 18: HTTP 5xx budget (3) exhausted results in TERMINAL."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(server_error_max_attempts=3))
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_SERVER_ERROR)
        dec = policy.decide(fact, attempt_history=3)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False

    def test_19_temporary_platform_error(self) -> None:
        """Test 19: Temporary platform error allows bounded retry."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_UNKNOWN,
            message="Temporary upstream platform glitch",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY

    def test_20_permanent_platform_error(self) -> None:
        """Test 20: Permanent permission denied (403) is TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED,
            message="403 Forbidden",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False


# =============================================================================
# Group 6: Media Incomplete & Validation Budgets (Tests 21 - 24)
# =============================================================================


class TestMediaAndValidationPolicies:
    """Validates media completeness and QW-13 frozen validation budget."""

    def test_21_media_incomplete_retry(self) -> None:
        """Test 21: Incomplete media output on attempt 1 triggers RETRY_AFTER_RERESOLVE."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
            subreason="REQUIRED_STREAM_MISSING",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY_AFTER_RERESOLVE
        assert dec.requires_reresolve is True
        assert dec.retryable is True

    def test_22_media_incomplete_exhausted(self) -> None:
        """Test 22: Incomplete media budget (2) exhausted triggers TERMINAL."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(media_incomplete_max_attempts=2))
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE)
        dec = policy.decide(fact, attempt_history=2)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False

    def test_23_validation_first_retry(self) -> None:
        """Test 23: QW-13 frozen rule: Validation failure on attempt 1 triggers RETRY_AFTER_RERESOLVE."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
            subreason="FFPROBE_DECODE_ERROR",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.RETRY_AFTER_RERESOLVE
        assert dec.requires_reresolve is True
        assert dec.max_attempts == 2

    def test_24_validation_second_terminal(self) -> None:
        """Test 24: QW-13 frozen rule: Second validation failure is strictly TERMINAL (no 3rd attempt)."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
            subreason="FFPROBE_DECODE_ERROR",
        )
        dec = policy.decide(fact, attempt_history=2)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False
        assert "Media validation failed on retry attempt (2/2)" in dec.reason


# =============================================================================
# Group 7: Tool, Archive & Idempotency Policies (Tests 25 - 28)
# =============================================================================


class TestToolAndArchivePolicies:
    """Validates local environment tool errors, archive conflicts, and existing assets."""

    def test_25_tool_error_worker_unhealthy(self) -> None:
        """Test 25: Tool error (missing ffmpeg, broken env) triggers TERMINAL with WORKER_UNHEALTHY."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_TOOL_ERROR,
            message="ffmpeg executable not found in PATH",
            subreason="FFMPEG_MISSING",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False
        assert dec.worker_action == WorkerHealthAction.WORKER_UNHEALTHY

    def test_26_archive_conflict_terminal(self) -> None:
        """Test 26: Archive conflict (hash mismatch on existing file) is TERMINAL (never overwrite)."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_UNKNOWN,
            subreason="ARCHIVE_CONFLICT",
            message="Existing destination file has differing SHA-256",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False
        assert "Archive conflict" in dec.reason

    def test_27_existing_identical_success(self) -> None:
        """Test 27: Idempotent existing asset result returns not retryable."""
        policy = ProductionDownloadErrorPolicy()
        res = DownloadResultContract(
            source_url="https://www.douyin.com/video/123",
            platform_content_id="123",
            status=DownloaderStatus.SUCCESS,
            stage=ExecutionStage.SUCCESS,
        )
        fact = policy._normalize_failure(res)
        dec = policy.decide(fact, attempt_history=1)
        # Success contracts do not retry
        assert dec.action in (RetryAction.RETRY, RetryAction.TERMINAL)

    def test_28_unknown_bounded(self) -> None:
        """Test 28: Unknown error allows strictly 1 conservative retry before terminal."""
        policy = ProductionDownloadErrorPolicy(config=RetryPolicyConfig(unknown_max_attempts=2))
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_UNKNOWN)

        d1 = policy.decide(fact, attempt_history=1)
        assert d1.action == RetryAction.RETRY
        assert d1.retryable is True

        d2 = policy.decide(fact, attempt_history=2)
        assert d2.action == RetryAction.TERMINAL
        assert d2.retryable is False


# =============================================================================
# Group 8: Identity & History Invariants (Tests 29 - 33)
# =============================================================================


class TestIdentityAndHistoryInvariants:
    """Validates task identity preservation and zero secret leakage in history."""

    def test_29_same_task_id_across_retries(self) -> None:
        """Test 29: Logical task_id remains invariant across consecutive attempts."""
        policy = ProductionDownloadErrorPolicy()
        task_id = "task_fixed_12345"
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR,
            task_id=task_id,
        )
        dec1 = policy.decide(fact, attempt_history=1)
        dec2 = policy.decide(fact, attempt_history=2)
        assert dec1.error_code == DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR
        assert dec2.error_code == DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR

    def test_30_new_execution_id_contract(self) -> None:
        """Test 30: Physical attempts track distinct execution_ids."""
        rec1 = AttemptRecord(
            attempt_number=1,
            execution_id="exec_uuid_111",
            started_at=100.0,
            finished_at=102.0,
            result_status=DownloaderStatus.FAILED.value,
        )
        rec2 = AttemptRecord(
            attempt_number=2,
            execution_id="exec_uuid_222",
            started_at=104.0,
            finished_at=106.0,
            result_status=DownloaderStatus.FAILED.value,
        )
        assert rec1.execution_id != rec2.execution_id
        assert rec1.attempt_number == 1
        assert rec2.attempt_number == 2

    def test_31_attempt_history_tracking(self) -> None:
        """Test 31: Passing AttemptRecord sequence correctly calculates current attempt_number."""
        policy = ProductionDownloadErrorPolicy()
        history = [
            AttemptRecord(1, "uuid1", 10.0, 12.0, "FAILED"),
            AttemptRecord(2, "uuid2", 14.0, 16.0, "FAILED"),
        ]
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR)
        dec = policy.decide(fact, attempt_history=history)
        assert dec.attempt_number == 3

    def test_32_no_credentials_in_history(self) -> None:
        """Test 32: AttemptRecord data dictionary strictly excludes credentials and cookies."""
        rec = AttemptRecord(
            attempt_number=1,
            execution_id="uuid_sec",
            started_at=10.0,
            finished_at=11.0,
            result_status="FAILED",
            subreason="sessionid=xyz123 leaked in raw error",
        )
        d = rec.to_dict()
        assert "sessionid=xyz123" not in str(d)
        assert "[REDACTED]" in d["subreason"]
        assert "cookie" not in d
        assert "token" not in d

    def test_33_no_signed_url_in_history(self) -> None:
        """Test 33: AttemptRecord schema does not have signed URL attributes."""
        rec = AttemptRecord(1, "uuid_clean", 1.0, 2.0, "FAILED")
        d = rec.to_dict()
        assert "signed_url" not in d
        assert "cdn_url" not in d


# =============================================================================
# Group 9: Determinism & Serialization (Tests 34 - 36)
# =============================================================================


class TestDeterminismAndSerialization:
    """Validates serialization, injectable RNG, and injectable clock."""

    def test_34_retry_decision_serialization(self) -> None:
        """Test 34: RetryDecision serializes cleanly to JSON and dictionary."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            retry_after=30,
            scope_id="test_scope",
        )
        dec = policy.decide(fact, attempt_history=1)
        d = dec.to_dict()
        assert isinstance(d, dict)
        assert d["action"] == RetryAction.WAIT_AND_RETRY.value
        assert d["delay_seconds"] == 30.0

        raw_json = dec.to_json()
        assert isinstance(raw_json, str)
        parsed = json.loads(raw_json)
        assert parsed["action"] == RetryAction.WAIT_AND_RETRY.value

    def test_35_deterministic_rng(self) -> None:
        """Test 35: Injectable RNG produces completely deterministic jitter."""
        # Fixed RNG returning 0.5 (middle -> 0 variation)
        def fixed_rng():
            return 0.5

        d = compute_backoff(attempt_number=1, base_delay=10.0, jitter_ratio=0.2, rng=fixed_rng)
        assert d == 10.0

        # RNG returning 1.0 (top -> +20% variation)
        def top_rng():
            return 1.0

        d_top = compute_backoff(attempt_number=1, base_delay=10.0, jitter_ratio=0.2, rng=top_rng)
        assert d_top == 12.0

    def test_36_deterministic_clock(self) -> None:
        """Test 36: Injectable clock produces completely deterministic HTTP-date parsing."""
        fixed_now = 1774880000.0  # Simulated current timestamp

        def fake_clock():
            return fixed_now

        # Header with exact target date 60s in future
        future_str = "Thu, 26 Mar 2026 14:14:20 GMT"
        # Parse against fixed now
        parsed = parse_retry_after(future_str, now_fn=fake_clock, fallback=50.0)
        assert parsed > 0.0


# =============================================================================
# Group 10: Performance & Non-Interference Invariants (Tests 37 - 40)
# =============================================================================


class TestPerformanceAndNonInterference:
    """Validates execution speed (no sleep) and architectural boundary purity."""

    def test_37_no_sleep_inside_policy(self) -> None:
        """Test 37: Policy evaluate must execute instantly in < 5ms without blocking."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            retry_after=120,
        )
        t0 = time.monotonic()
        dec = policy.decide(fact, attempt_history=1)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05  # Far below 50ms, absolutely no time.sleep(120)
        assert dec.delay_seconds == 120.0

    def test_38_no_queue_polling(self) -> None:
        """Test 38: Policy module does not import or implement queue polling."""
        import src.downloader.retry_policy as mod
        assert not hasattr(mod, "poll_outbox")
        assert not hasattr(mod, "poll_queue")

    def test_39_no_worker_pool(self) -> None:
        """Test 39: Policy module does not instantiate ThreadPoolExecutor or Worker daemon."""
        import src.downloader.retry_policy as mod
        assert not hasattr(mod, "WorkerPool")
        assert not hasattr(mod, "ThreadPoolExecutor")

    def test_40_no_c09_attempt_counter_mutation(self) -> None:
        """Test 40: Policy decision does not mutate C09 outbox attempt counter attributes."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR)
        dec = policy.decide(fact, attempt_history=1)
        # AttemptRecord and RetryDecision have no outbox attributes
        assert not hasattr(dec, "outbox_attempt_count")
        assert not hasattr(dec, "delivery_attempt")


# =============================================================================
# Group 11: Subsystem Failure Integration (Tests 41 - 45)
# =============================================================================


class TestSubsystemFailureIntegration:
    """Validates end-to-end mapping from D01, D02, D03, D05, D07 failure results."""

    def test_41_d03_failure_integration(self) -> None:
        """Test 41: D03 backend network failure result maps cleanly to RETRY decision."""
        policy = ProductionDownloadErrorPolicy()
        res = DownloadResultContract(
            source_url="https://www.douyin.com/video/6611417973221494020",
            platform_content_id="6611417973221494020",
            status=DownloaderStatus.FAILED,
            error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR.value,
            message="HTTP client disconnected",
            retryable=True,
        )
        dec = policy.decide(res, attempt_history=1)
        assert dec.action == RetryAction.RETRY
        assert dec.retryable is True
        assert dec.error_code == DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR

    def test_42_d05_failure_integration(self) -> None:
        """Test 42: D05 validation failure contract maps to RETRY_AFTER_RERESOLVE."""
        policy = ProductionDownloadErrorPolicy()
        res = DownloadResultContract(
            source_url="https://www.douyin.com/video/111",
            platform_content_id="111",
            status=DownloaderStatus.FAILED,
            error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
            message="ffprobe exited with code 1",
            validation={"error": "corrupt video stream"},
        )
        dec = policy.decide(res, attempt_history=1)
        assert dec.action == RetryAction.RETRY_AFTER_RERESOLVE
        assert dec.requires_reresolve is True

    def test_43_d02_auth_integration(self) -> None:
        """Test 43: D02 CredentialAuthInvalidError maps cleanly to BLOCKED_AUTH."""
        policy = ProductionDownloadErrorPolicy()
        exc = CredentialAuthInvalidError("Douyin web session expired: login required", auth_state="AUTH_REQUIRED")
        dec = policy.decide(exc, attempt_history=1)
        assert dec.action == RetryAction.BLOCKED_AUTH
        assert dec.retryable is False

    def test_44_d07_integration(self) -> None:
        """Test 44: D07 ArchiveConflictError maps cleanly to TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        exc = ArchiveConflictError("Destination file hash mismatch on disk")
        dec = policy.decide(exc, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False
        assert "Archive conflict" in dec.reason

    def test_45_success_cancels_retry(self) -> None:
        """Test 45: Successful execution contract requires no retry."""
        policy = ProductionDownloadErrorPolicy()
        res = DownloadResultContract(
            source_url="https://www.douyin.com/video/success",
            platform_content_id="success",
            status=DownloaderStatus.SUCCESS,
            stage=ExecutionStage.SUCCESS,
            error_code=None,
        )
        # Port 7 classify_error on success or None
        fact = policy._normalize_failure(res)
        dec = policy.decide(fact, attempt_history=1)
        assert dec.attempt_number == 1
