"""Douyin Authentication State Detector & Preflight Gatekeeper (DY-C03).

Provides rigorous, two-tier behavioral authentication detection combining:
- Level 1: In-browser page DOM and challenge telemetry (douyin.auth.page_signals)
- Level 2: Protected Collection API probe evidence (source_client.probe_source())

Design Invariants:
1. Two-dimensional separation: AuthState (VALID/REQUIRED/CHALLENGE/UNCERTAIN) vs
   SourceHealth (HEALTHY/RATE_LIMITED/SERVER_ERROR/NETWORK_ERROR/SOURCE_ERROR).
2. Behavioral probe only: Zero cookie inspection, no secret extraction.
3. CollectionAccess: READABLE vs EMPTY vs UNAVAILABLE. Empty collection is NEVER
   judged as AUTH_REQUIRED.
4. HTTP 403 Ambiguity: 403 without login modal or interactive captcha is AUTH_UNCERTAIN,
   never prematurely classified as AUTH_REQUIRED.
5. Passive detection: No automatic popups or automated captcha solving.
6. Commit safety: can_sync and can_commit_sync_state are strictly synchronized.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..base import utcnow_iso
from ..interfaces import AuthStateDetector, BrowserRuntimeProvider, SourceClient
from .config import DouyinCollectorConfig
from .source_client import DouyinSourceClient, SourceProbeEvidence

logger = logging.getLogger("collector.douyin.auth")


def generate_account_scope_id(platform: str, account_identifier: str) -> str:
    """Generates a stable, non-secret, deterministic account scope ID.

    Formula: {platform}:dyacct_{sha256(platform + ':' + identifier)[:16]}
    Raw identifier (sec_uid / uid) is hashed and never leaked into logs.
    """
    raw_key = f"{platform}:{account_identifier}".encode("utf-8")
    digest = hashlib.sha256(raw_key).hexdigest()[:16]
    return f"{platform}:dyacct_{digest}"


class AuthState(str, Enum):
    """Authentication lifecycle state."""
    AUTH_VALID = "AUTH_VALID"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_CHALLENGE = "AUTH_CHALLENGE"
    AUTH_UNCERTAIN = "AUTH_UNCERTAIN"


class SourceHealth(str, Enum):
    """Transport and remote service health."""
    HEALTHY = "HEALTHY"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    SOURCE_ERROR = "SOURCE_ERROR"
    UNKNOWN = "UNKNOWN"


class CollectionAccess(str, Enum):
    """Accessibility of protected collection resources."""
    READABLE = "READABLE"
    EMPTY = "EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class RecoveryAction(str, Enum):
    """Machine-readable suggested recovery action."""
    NONE = "NONE"
    RETRY_LATER = "RETRY_LATER"
    MANUAL_LOGIN = "MANUAL_LOGIN"
    MANUAL_CHALLENGE = "MANUAL_CHALLENGE"
    INVESTIGATE = "INVESTIGATE"


class AuthReasonCode(str, Enum):
    """Machine-readable standardized reason codes."""
    AUTH_OK = "AUTH_OK"
    COLLECTION_EMPTY = "COLLECTION_EMPTY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    INTERACTIVE_CHALLENGE = "INTERACTIVE_CHALLENGE"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    ACCOUNT_SCOPE_UNRESOLVED = "ACCOUNT_SCOPE_UNRESOLVED"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_SERVER_ERROR = "SOURCE_SERVER_ERROR"
    SOURCE_NETWORK_ERROR = "SOURCE_NETWORK_ERROR"
    SOURCE_HTTP_403_AMBIGUOUS = "SOURCE_HTTP_403_AMBIGUOUS"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    SOURCE_RESPONSE_INVALID = "SOURCE_RESPONSE_INVALID"
    BROWSER_NOT_READY = "BROWSER_NOT_READY"
    UNKNOWN_AUTH_STATE = "UNKNOWN_AUTH_STATE"


@dataclass(frozen=True)
class PageSignalsEvidence:
    """Sanitized Level 1 in-browser page signals."""
    url: str
    title: str
    login_modal_visible: bool
    challenge_visible: bool
    expected_user_identity_present: bool
    normal_user_page_present: bool
    is_redirected: bool
    account_identifier: str | None = None  # Internal raw identifier; omitted from to_dict()
    account_scope_id: str | None = None   # Safe deterministic scope ID, e.g. douyin:dyacct_05113fadd33e874f

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "login_modal_visible": self.login_modal_visible,
            "challenge_visible": self.challenge_visible,
            "expected_user_identity_present": self.expected_user_identity_present,
            "normal_user_page_present": self.normal_user_page_present,
            "is_redirected": self.is_redirected,
            "account_scope_id": self.account_scope_id,
            "has_account_scope": bool(self.account_scope_id),
        }


@dataclass(frozen=True)
class AuthPreflightResult:
    """Immutable, structured preflight evaluation result."""
    auth_state: AuthState
    source_health: SourceHealth
    collection_access: CollectionAccess
    can_sync: bool
    requires_user_action: bool
    retryable: bool
    reason_code: str
    recovery_action: RecoveryAction
    observed_at: str = field(default_factory=utcnow_iso)
    page_evidence: dict[str, Any] = field(default_factory=dict)
    source_evidence: dict[str, Any] = field(default_factory=dict)
    account_scope_id: str | None = None

    @property
    def can_commit_sync_state(self) -> bool:
        """Strict safety invariant: sync state commits permitted ONLY when can_sync is True."""
        return self.can_sync

    def to_dict(self) -> dict[str, Any]:
        return {
            "auth_state": self.auth_state.value,
            "source_health": self.source_health.value,
            "collection_access": self.collection_access.value,
            "can_sync": self.can_sync,
            "can_commit_sync_state": self.can_commit_sync_state,
            "requires_user_action": self.requires_user_action,
            "retryable": self.retryable,
            "reason_code": self.reason_code,
            "recovery_action": self.recovery_action.value,
            "account_scope_id": self.account_scope_id,
            "observed_at": self.observed_at,
            "page_evidence": self.page_evidence,
            "source_evidence": self.source_evidence,
        }


class DouyinAuthStateDetector(AuthStateDetector):
    """Preflight authentication and operational health gatekeeper for Douyin (C03)."""

    def __init__(
        self,
        runtime_provider: BrowserRuntimeProvider,
        source_client: SourceClient | None = None,
        config: DouyinCollectorConfig | None = None,
    ) -> None:
        self.runtime_provider = runtime_provider
        self.config = config or DouyinCollectorConfig()
        self.source_client = source_client or DouyinSourceClient(
            runtime_provider=runtime_provider,
            config=self.config,
        )
        self.last_result: AuthPreflightResult | None = None

    def check_auth(self) -> str:
        """Protocol compliance: return string status summary."""
        result = self.detect_auth_state()
        if result.can_sync:
            return "LOGIN_OK"
        if result.auth_state == AuthState.AUTH_REQUIRED:
            return "AUTH_REQUIRED"
        if result.auth_state == AuthState.AUTH_CHALLENGE:
            return "AUTH_CHALLENGE"
        return result.auth_state.value

    def detect_auth_state(self, ensure_navigation: bool = True) -> AuthPreflightResult:
        """Execute full preflight detection combining Level 1 and Level 2 signals."""
        observed_at = utcnow_iso()

        # Step 1: Check runtime health
        if not self.runtime_provider.is_running():
            logger.warning("Preflight detection aborted: BrowserRuntimeProvider is not running.")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.SOURCE_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.BROWSER_NOT_READY.value,
                recovery_action=RecoveryAction.INVESTIGATE,
                observed_at=observed_at,
                page_evidence={"error": "Runtime not running"},
                source_evidence={},
            )

        # Step 2: Query Level 1 Page Signals
        page_signals = self._query_page_signals(ensure_navigation=ensure_navigation)
        if page_signals is None:
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.SOURCE_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.BROWSER_NOT_READY.value,
                recovery_action=RecoveryAction.INVESTIGATE,
                observed_at=observed_at,
                page_evidence={"error": "Failed to query page signals from browser session"},
                source_evidence={},
            )

        # Early exit 1: Visible interactive challenge on DOM
        if page_signals.challenge_visible:
            logger.warning("Level 1 detected visible interactive challenge on page.")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_CHALLENGE,
                source_health=SourceHealth.HEALTHY,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=True,
                retryable=False,
                reason_code=AuthReasonCode.INTERACTIVE_CHALLENGE.value,
                recovery_action=RecoveryAction.MANUAL_CHALLENGE,
                observed_at=observed_at,
                page_evidence=page_signals.to_dict(),
                source_evidence={"request_attempted": False},
            )

        # Early exit 2: Explicit login modal without user identity
        if page_signals.login_modal_visible and not page_signals.expected_user_identity_present:
            logger.warning("Level 1 detected explicit login modal without user identity.")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_REQUIRED,
                source_health=SourceHealth.HEALTHY,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=True,
                retryable=False,
                reason_code=AuthReasonCode.LOGIN_REQUIRED.value,
                recovery_action=RecoveryAction.MANUAL_LOGIN,
                observed_at=observed_at,
                page_evidence=page_signals.to_dict(),
                source_evidence={"request_attempted": False},
            )

        # Step 3: Query Level 2 Protected API Probe
        source_evidence = self._query_source_probe()

        # Step 4: Evaluate Decision Table
        result = self._evaluate_decision_table(page_signals, source_evidence, observed_at)
        self.last_result = result
        return result

    def _query_page_signals(self, ensure_navigation: bool = True) -> PageSignalsEvidence | None:
        """Query Level 1 page signals from active browser session."""
        try:
            if not hasattr(self.runtime_provider, "get_session"):
                return None
            session = self.runtime_provider.get_session()
            if ensure_navigation:
                content = session.get_content()
                current_url = content.get("url", "")
                if not current_url or "douyin.com" not in current_url:
                    session.navigate("https://www.douyin.com/user/self", timeout=30.0)

            raw_signals = session.request("douyin.auth.page_signals", timeout=15.0)
            if not isinstance(raw_signals, dict):
                return None

            raw_ident = raw_signals.get("account_identifier")
            acct_scope_id = raw_signals.get("account_scope_id")
            if not acct_scope_id and raw_ident:
                acct_scope_id = generate_account_scope_id("douyin", str(raw_ident))

            return PageSignalsEvidence(
                url=str(raw_signals.get("url", "")),
                title=str(raw_signals.get("title", "")),
                login_modal_visible=bool(raw_signals.get("login_modal_visible", raw_signals.get("is_login_modal_present", False))),
                challenge_visible=bool(raw_signals.get("challenge_visible", raw_signals.get("has_captcha", False))),
                expected_user_identity_present=bool(raw_signals.get("expected_user_identity_present", raw_signals.get("has_avatar", False))),
                normal_user_page_present=bool(raw_signals.get("normal_user_page_present", True)),
                is_redirected=bool(raw_signals.get("is_redirected", False)),
                account_identifier=str(raw_ident) if raw_ident else None,
                account_scope_id=acct_scope_id,
            )
        except Exception as e:
            logger.warning(f"Error querying page signals: {e}")
            return None

    def _query_source_probe(self) -> SourceProbeEvidence:
        """Query Level 2 protected API probe from SourceClient."""
        if hasattr(self.source_client, "probe_source"):
            return self.source_client.probe_source()
        return SourceProbeEvidence(
            request_attempted=False,
            http_status=None,
            platform_status=None,
            response_shape_valid=False,
            items_readable=False,
            page_url="",
            is_redirected=False,
            challenge_hint=False,
            error_message="source_client does not implement probe_source()",
        )

    def _evaluate_decision_table(
        self,
        page: PageSignalsEvidence,
        source: SourceProbeEvidence,
        observed_at: str,
    ) -> AuthPreflightResult:
        """Apply the formal decision table across Level 1 & Level 2 multi-signals."""
        page_dict = page.to_dict()
        source_dict = source.to_dict()

        # Case 1: Active interactive challenge
        if page.challenge_visible or (source.challenge_hint and ("verify" in page.url or "验证" in page.title)):
            logger.warning(f"Preflight decided AUTH_CHALLENGE (title='{page.title}', url='{page.url}').")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_CHALLENGE,
                source_health=SourceHealth.HEALTHY,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=True,
                retryable=False,
                reason_code=AuthReasonCode.INTERACTIVE_CHALLENGE.value,
                recovery_action=RecoveryAction.MANUAL_CHALLENGE,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 2: Login Required (login modal or platform_status == 8)
        if page.login_modal_visible or source.platform_status == 8:
            logger.warning(f"Preflight decided AUTH_REQUIRED (login_modal={page.login_modal_visible}, platform_status={source.platform_status}).")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_REQUIRED,
                source_health=SourceHealth.HEALTHY,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=True,
                retryable=False,
                reason_code=AuthReasonCode.LOGIN_REQUIRED.value,
                recovery_action=RecoveryAction.MANUAL_LOGIN,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 3: Rate Limited (HTTP 429 or status_code 2154)
        if source.http_status == 429 or source.platform_status == 2154:
            logger.warning(f"Preflight decided RATE_LIMITED (http={source.http_status}, platform={source.platform_status}).")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.RATE_LIMITED,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.SOURCE_RATE_LIMITED.value,
                recovery_action=RecoveryAction.RETRY_LATER,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 4: Server Error (HTTP 5xx)
        if source.http_status and 500 <= source.http_status < 600:
            logger.warning(f"Preflight decided SERVER_ERROR (http={source.http_status}).")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.SERVER_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.SOURCE_SERVER_ERROR.value,
                recovery_action=RecoveryAction.RETRY_LATER,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 5: Network / Transport failure / Timeout
        if (
            source.error_code in ("SOURCE_TIMEOUT", "SOURCE_NETWORK_ERROR")
            or (source.http_status is None and source.error_message)
        ):
            logger.warning(f"Preflight decided NETWORK_ERROR: {source.error_message or source.error_code}")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.NETWORK_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.SOURCE_NETWORK_ERROR.value,
                recovery_action=RecoveryAction.RETRY_LATER,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 6: HTTP 403 Ambiguous
        if source.http_status == 403:
            if page.login_modal_visible or "login" in page.url or "passport" in page.url:
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_REQUIRED,
                    source_health=SourceHealth.SOURCE_ERROR,
                    collection_access=CollectionAccess.UNAVAILABLE,
                    can_sync=False,
                    requires_user_action=True,
                    retryable=False,
                    reason_code=AuthReasonCode.LOGIN_REQUIRED.value,
                    recovery_action=RecoveryAction.MANUAL_LOGIN,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )
            if not page.expected_user_identity_present:
                logger.warning("Preflight encountered HTTP 403 without identity evidence (no explicit login modal).")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_UNCERTAIN,
                    source_health=SourceHealth.SOURCE_ERROR,
                    collection_access=CollectionAccess.UNKNOWN,
                    can_sync=False,
                    requires_user_action=False,
                    retryable=True,
                    reason_code=AuthReasonCode.IDENTITY_MISSING.value,
                    recovery_action=RecoveryAction.INVESTIGATE,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )
            logger.warning("Preflight encountered ambiguous HTTP 403 with valid page identity.")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.SOURCE_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.SOURCE_HTTP_403_AMBIGUOUS.value,
                recovery_action=RecoveryAction.RETRY_LATER,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 7: Invalid response shape
        if source.http_status == 200 and not source.response_shape_valid:
            logger.warning("Preflight encountered invalid response shape from listcollection API.")
            return AuthPreflightResult(
                auth_state=AuthState.AUTH_UNCERTAIN,
                source_health=SourceHealth.SOURCE_ERROR,
                collection_access=CollectionAccess.UNAVAILABLE,
                can_sync=False,
                requires_user_action=False,
                retryable=True,
                reason_code=AuthReasonCode.SOURCE_RESPONSE_INVALID.value,
                recovery_action=RecoveryAction.INVESTIGATE,
                observed_at=observed_at,
                page_evidence=page_dict,
                source_evidence=source_dict,
            )

        # Case 8: Identity missing on page and protected items not readable
        if not page.expected_user_identity_present and not source.items_readable:
            has_explicit_login_signal = (
                page.login_modal_visible
                or "login" in page.url
                or "passport" in page.url
                or source.platform_status == 8
            )
            if has_explicit_login_signal:
                logger.warning("Preflight decided AUTH_REQUIRED: explicit login failure signal present.")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_REQUIRED,
                    source_health=SourceHealth.HEALTHY,
                    collection_access=CollectionAccess.UNAVAILABLE,
                    can_sync=False,
                    requires_user_action=True,
                    retryable=False,
                    reason_code=AuthReasonCode.LOGIN_REQUIRED.value,
                    recovery_action=RecoveryAction.MANUAL_LOGIN,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )
            else:
                logger.warning("Preflight decided AUTH_UNCERTAIN: identity missing but no explicit login-required signal.")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_UNCERTAIN,
                    source_health=SourceHealth.SOURCE_ERROR,
                    collection_access=CollectionAccess.UNKNOWN,
                    can_sync=False,
                    requires_user_action=False,
                    retryable=True,
                    reason_code=AuthReasonCode.IDENTITY_MISSING.value,
                    recovery_action=RecoveryAction.INVESTIGATE,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )

        # Case 9: Coherent Valid State (Identity valid + HTTP 200 + platform_status 0 + items readable)
        if (
            page.expected_user_identity_present
            and source.http_status == 200
            and source.platform_status == 0
            and source.items_readable
        ):
            account_scope_id = page.account_scope_id
            if not account_scope_id:
                logger.warning("Preflight encountered AUTH_VALID but account scope could not be resolved.")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_VALID,
                    source_health=SourceHealth.HEALTHY,
                    collection_access=CollectionAccess.EMPTY if source.collection_empty else CollectionAccess.READABLE,
                    can_sync=False,
                    requires_user_action=False,
                    retryable=True,
                    reason_code=AuthReasonCode.ACCOUNT_SCOPE_UNRESOLVED.value,
                    recovery_action=RecoveryAction.INVESTIGATE,
                    account_scope_id=None,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )

            if source.collection_empty:
                logger.info(f"Preflight decided AUTH_VALID with COLLECTION_EMPTY (scope={account_scope_id}).")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_VALID,
                    source_health=SourceHealth.HEALTHY,
                    collection_access=CollectionAccess.EMPTY,
                    can_sync=True,
                    requires_user_action=False,
                    retryable=False,
                    reason_code=AuthReasonCode.COLLECTION_EMPTY.value,
                    recovery_action=RecoveryAction.NONE,
                    account_scope_id=account_scope_id,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )
            else:
                logger.info(f"Preflight decided AUTH_VALID with COLLECTION_READABLE (scope={account_scope_id}).")
                return AuthPreflightResult(
                    auth_state=AuthState.AUTH_VALID,
                    source_health=SourceHealth.HEALTHY,
                    collection_access=CollectionAccess.READABLE,
                    can_sync=True,
                    requires_user_action=False,
                    retryable=False,
                    reason_code=AuthReasonCode.AUTH_OK.value,
                    recovery_action=RecoveryAction.NONE,
                    account_scope_id=account_scope_id,
                    observed_at=observed_at,
                    page_evidence=page_dict,
                    source_evidence=source_dict,
                )

        # Case 10: Signal Conflict (Level 1 and Level 2 contradict each other)
        logger.warning(f"Preflight encountered contradictory evidence: page={page_dict}, source={source_dict}")
        return AuthPreflightResult(
            auth_state=AuthState.AUTH_UNCERTAIN,
            source_health=SourceHealth.SOURCE_ERROR,
            collection_access=CollectionAccess.UNKNOWN,
            can_sync=False,
            requires_user_action=False,
            retryable=True,
            reason_code=AuthReasonCode.EVIDENCE_CONFLICT.value,
            recovery_action=RecoveryAction.INVESTIGATE,
            observed_at=observed_at,
            page_evidence=page_dict,
            source_evidence=source_dict,
        )

    preflight = detect_auth_state
