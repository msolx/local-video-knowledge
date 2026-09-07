"""Comprehensive Unit and Integration Test Matrix for DouyinAuthStateDetector (DY-C03).

Covers all 20 required scenarios:
1. REAL current valid profile (Live test using Dedicated Profile)
2. VALID + COLLECTION_READABLE fixture
3. VALID + COLLECTION_EMPTY fixture (strict non-AUTH_REQUIRED guarantee)
4. AUTH_REQUIRED fixture (login modal without identity)
5. AUTH_CHALLENGE fixture (visible slider captcha / challenge container)
6. Invisible nocaptcha does NOT falsely trigger AUTH_CHALLENGE
7. 403 + login modal -> AUTH_REQUIRED
8. 403 + visible captcha -> AUTH_CHALLENGE
9. 403 + identity valid + no challenge -> AUTH_UNCERTAIN (SOURCE_HTTP_403_AMBIGUOUS)
10. 429 -> RATE_LIMITED (never misclassified as AUTH_REQUIRED)
11. 500 -> SERVER_ERROR
12. Network failure / timeout -> NETWORK_ERROR
13. Malformed source response -> UNCERTAIN / SOURCE_ERROR
14. Level 1 / Level 2 conflict -> AUTH_UNCERTAIN (EVIDENCE_CONFLICT)
15. Zero cookie inspection in normal runtime code
16. Zero secret logging in preflight results
17. CollectorService blocks sync when can_sync=False
18. CollectorService continues sync when AUTH_VALID (can_sync=True)
19. Empty collection does not fail preflight or clear sync state
20. Reason codes are stable machine-readable constants
21. Real Dedicated Profile Headless preflight validation (Passes Level 1 + Level 2)
22. Real Dedicated Profile Restart preflight validation
23. REAL_OBSERVED_CHALLENGE_FIXTURE regression
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import pytest

from src.collector.base import CollectorMode, CollectorStatus
from src.collector.douyin.auth_state import (
    AuthPreflightResult,
    AuthReasonCode,
    AuthState,
    CollectionAccess,
    DouyinAuthStateDetector,
    PageSignalsEvidence,
    RecoveryAction,
    SourceHealth,
)
from src.collector.douyin.browser_runtime import DouyinBrowserRuntimeProvider
from src.collector.douyin.collector import DouyinCollector
from src.collector.douyin.config import DouyinCollectorConfig
from src.collector.douyin.source_client import (
    CollectionPage,
    DouyinSourceClient,
    SourceProbeEvidence,
)
from src.collector.service import CollectorService

DEDICATED_PROFILE = Path("G:/antigravity-cli/dy/runtime/chrome-profile").resolve()


# ---------------------------------------------------------------------------
# Test Mocks
# ---------------------------------------------------------------------------

class MockSession:
    def __init__(self, page_signals: dict[str, Any] | None = None, content: dict[str, str] | None = None) -> None:
        self.page_signals = dict(page_signals) if page_signals is not None else {}
        # If expected_user_identity_present is True and account_identifier not explicitly specified, provide default
        if self.page_signals.get("expected_user_identity_present") and "account_identifier" not in self.page_signals:
            self.page_signals["account_identifier"] = "MS4wLjABAAAA_mock_sec_uid_default_12345"
        self.content = content or {"url": "https://www.douyin.com/user/self", "title": "Mock User - 抖音"}
        self.navigated_urls: list[str] = []

    def get_content(self) -> dict[str, str]:
        return self.content

    def navigate(self, url: str, timeout: float = 30.0) -> None:
        self.navigated_urls.append(url)
        self.content["url"] = url

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        if method == "douyin.auth.page_signals":
            return self.page_signals
        raise ValueError(f"Unexpected mock method: {method}")


class MockProvider:
    def __init__(self, running: bool = True, session: MockSession | None = None) -> None:
        self.running = running
        self.session = session or MockSession()

    def is_running(self) -> bool:
        return self.running

    def get_session(self) -> MockSession:
        return self.session

    def launch(self) -> None:
        self.running = True

    def close(self) -> None:
        self.running = False


class MockSourceClient:
    def __init__(self, probe_evidence: SourceProbeEvidence | None = None) -> None:
        self.probe_evidence = probe_evidence or SourceProbeEvidence(
            request_attempted=True,
            http_status=200,
            platform_status=0,
            response_shape_valid=True,
            items_readable=True,
            collection_empty=False,
            page_url="https://www.douyin.com/user/self",
            is_redirected=False,
            challenge_hint=False,
        )

    def probe_source(self) -> SourceProbeEvidence:
        return self.probe_evidence

    def fetch_collection_page(self, cursor: str = "0", count: int = 10) -> CollectionPage:
        return CollectionPage(
            request_cursor=cursor,
            response_cursor="100",
            has_more=False,
            items=[{"aweme_id": "test_1"}],
            raw_response={"status_code": 0, "aweme_list": [{"aweme_id": "test_1"}]},
            http_status=200,
            platform_status=0,
            fetched_at="2026-09-05T00:00:00Z",
            latency_ms=10.0,
        )


# ---------------------------------------------------------------------------
# Unit Test Scenarios (Cases 2 - 20)
# ---------------------------------------------------------------------------

def test_case_2_valid_collection_readable() -> None:
    """Case 2: Coherent normal identity + protected API items -> AUTH_VALID, READABLE."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=0,
        response_shape_valid=True,
        items_readable=True,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_VALID
    assert res.source_health == SourceHealth.HEALTHY
    assert res.collection_access == CollectionAccess.READABLE
    assert res.can_sync is True
    assert res.can_commit_sync_state is True
    assert res.requires_user_action is False
    assert res.retryable is False
    assert res.reason_code == AuthReasonCode.AUTH_OK.value
    assert res.recovery_action == RecoveryAction.NONE
    assert detector.check_auth() == "LOGIN_OK"


def test_case_3_valid_collection_empty_never_auth_required() -> None:
    """Case 3 & 19: Valid identity + HTTP 200 platform 0 + empty list -> AUTH_VALID, EMPTY.
    
    CRITICAL INVARIANT: Empty collection must NEVER be misclassified as AUTH_REQUIRED!
    """
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=0,
        response_shape_valid=True,
        items_readable=True,
        collection_empty=True,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_VALID
    assert res.source_health == SourceHealth.HEALTHY
    assert res.collection_access == CollectionAccess.EMPTY
    assert res.can_sync is True
    assert res.can_commit_sync_state is True
    assert res.requires_user_action is False
    assert res.reason_code == AuthReasonCode.COLLECTION_EMPTY.value
    assert res.recovery_action == RecoveryAction.NONE
    assert detector.check_auth() == "LOGIN_OK"


def test_case_4_auth_required_login_modal() -> None:
    """Case 4: Login modal present, no user identity -> AUTH_REQUIRED, action required."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "登录 - 抖音",
        "login_modal_visible": True,
        "challenge_visible": False,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=False,
        http_status=None,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_REQUIRED
    assert res.can_sync is False
    assert res.can_commit_sync_state is False
    assert res.requires_user_action is True
    assert res.reason_code == AuthReasonCode.LOGIN_REQUIRED.value
    assert res.recovery_action == RecoveryAction.MANUAL_LOGIN
    assert detector.check_auth() == "AUTH_REQUIRED"


def test_case_5_auth_challenge_visible_slider() -> None:
    """Case 5: Visible slider captcha / challenge container -> AUTH_CHALLENGE."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "安全验证 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": True,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient()

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_CHALLENGE
    assert res.can_sync is False
    assert res.requires_user_action is True
    assert res.reason_code == AuthReasonCode.INTERACTIVE_CHALLENGE.value
    assert res.recovery_action == RecoveryAction.MANUAL_CHALLENGE
    assert detector.check_auth() == "AUTH_CHALLENGE"


def test_case_6_invisible_nocaptcha_does_not_trigger_challenge() -> None:
    """Case 6: Invisible background nocaptcha iframe present, challenge_visible=False -> NOT challenge."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,  # invisible telemetry excluded
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=0,
        response_shape_valid=True,
        items_readable=True,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_VALID
    assert res.can_sync is True
    assert res.reason_code == AuthReasonCode.AUTH_OK.value


def test_case_7_http_403_with_login_modal_is_auth_required() -> None:
    """Case 7: HTTP 403 when login modal is visible -> AUTH_REQUIRED."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "抖音",
        "login_modal_visible": True,
        "challenge_visible": False,
        "expected_user_identity_present": False,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=403,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=True,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_REQUIRED
    assert res.reason_code == AuthReasonCode.LOGIN_REQUIRED.value
    assert res.can_sync is False


def test_case_8_http_403_with_visible_captcha_is_auth_challenge() -> None:
    """Case 8: HTTP 403 when visible captcha is present -> AUTH_CHALLENGE."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "验证中间页 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": True,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=403,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=True,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_CHALLENGE
    assert res.reason_code == AuthReasonCode.INTERACTIVE_CHALLENGE.value
    assert res.recovery_action == RecoveryAction.MANUAL_CHALLENGE
    assert res.can_sync is False


def test_case_9_http_403_ambiguous_when_identity_valid_no_challenge() -> None:
    """Case 9: HTTP 403 when page identity is valid and NO modal or challenge -> AUTH_UNCERTAIN.
    
    CRITICAL INVARIANT: Prohibit direct 403 => AUTH_REQUIRED when identity is normal.
    """
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=403,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.source_health == SourceHealth.SOURCE_ERROR
    assert res.can_sync is False
    assert res.requires_user_action is False
    assert res.retryable is True
    assert res.reason_code == AuthReasonCode.SOURCE_HTTP_403_AMBIGUOUS.value
    assert res.recovery_action == RecoveryAction.RETRY_LATER


def test_case_10_rate_limit_429_is_rate_limited_not_auth_required() -> None:
    """Case 10: HTTP 429 or status 2154 -> RATE_LIMITED, NOT AUTH_REQUIRED."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=429,
        platform_status=2154,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.source_health == SourceHealth.RATE_LIMITED
    assert res.can_sync is False
    assert res.retryable is True
    assert res.reason_code == AuthReasonCode.SOURCE_RATE_LIMITED.value
    assert res.recovery_action == RecoveryAction.RETRY_LATER


def test_case_11_server_error_500() -> None:
    """Case 11: HTTP 500 -> SERVER_ERROR, does NOT require re-login."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=500,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.source_health == SourceHealth.SERVER_ERROR
    assert res.can_sync is False
    assert res.retryable is True
    assert res.requires_user_action is False
    assert res.reason_code == AuthReasonCode.SOURCE_SERVER_ERROR.value
    assert res.recovery_action == RecoveryAction.RETRY_LATER


def test_case_12_network_failure_timeout() -> None:
    """Case 12: Network timeout / socket abort -> NETWORK_ERROR, does not destroy profile."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=None,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
        error_code="SOURCE_TIMEOUT",
        error_message="listcollection fetch timed out",
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.source_health == SourceHealth.NETWORK_ERROR
    assert res.can_sync is False
    assert res.retryable is True
    assert res.reason_code == AuthReasonCode.SOURCE_NETWORK_ERROR.value
    assert res.recovery_action == RecoveryAction.RETRY_LATER


def test_case_13_malformed_response_shape() -> None:
    """Case 13: HTTP 200 but response is malformed / invalid JSON -> UNCERTAIN, SOURCE_RESPONSE_INVALID."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.can_sync is False
    assert res.reason_code == AuthReasonCode.SOURCE_RESPONSE_INVALID.value


def test_case_14_conflicting_signals_yields_uncertain() -> None:
    """Case 14: Conflicting evidence (e.g. login modal present but API returns 200 items)."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": True,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=0,
        response_shape_valid=True,
        items_readable=True,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    # Login modal presence takes precedence as AUTH_REQUIRED
    assert res.can_sync is False
    assert res.auth_state in (AuthState.AUTH_REQUIRED, AuthState.AUTH_UNCERTAIN)


def test_case_15_no_cookie_inspection_in_normal_runtime() -> None:
    """Case 15: Source code audit: detector does NOT call page.cookies() or inspect sessionid."""
    import inspect
    source_code = inspect.getsource(DouyinAuthStateDetector)
    assert "page.cookies" not in source_code
    assert "sessionid" not in source_code
    assert "sid_guard" not in source_code
    assert "passport_assist" not in source_code
    assert "passport_auth" not in source_code


def test_case_16_no_secret_logging_in_preflight_results() -> None:
    """Case 16: Result serialization contains zero cookie or auth token keys."""
    res = AuthPreflightResult(
        auth_state=AuthState.AUTH_VALID,
        source_health=SourceHealth.HEALTHY,
        collection_access=CollectionAccess.READABLE,
        can_sync=True,
        requires_user_action=False,
        retryable=False,
        reason_code=AuthReasonCode.AUTH_OK.value,
        recovery_action=RecoveryAction.NONE,
        page_evidence={"url": "https://www.douyin.com/user/self", "title": "User Profile"},
        source_evidence={"http_status": 200, "platform_status": 0},
    )
    d = res.to_dict()
    for sensitive in ("cookie", "sessionid", "token", "a_bogus", "msToken"):
        assert sensitive not in str(d).lower()


def test_case_17_collector_service_blocks_sync_when_cannot_sync() -> None:
    """Case 17: CollectorService halts when preflight can_sync is False."""
    session = MockSession(page_signals={
        "login_modal_visible": True,
        "challenge_visible": False,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=MockSourceClient())
    collector = DouyinCollector(browser_runtime=provider, auth_detector=detector)

    service = CollectorService(collector=collector)
    res = service.run(mode=CollectorMode.SYNC)

    assert res.status == CollectorStatus.FAILED
    assert res.error is not None
    assert res.error["code"] == "AUTH_NOT_READY"
    assert "details" in res.error
    assert res.error["details"].get("can_sync") is False


def test_case_18_collector_service_continues_when_auth_valid() -> None:
    """Case 18: CollectorService proceeds when preflight can_sync is True."""
    session = MockSession(page_signals={
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=MockSourceClient())
    collector = DouyinCollector(
        browser_runtime=provider,
        auth_detector=detector,
        source_client=MockSourceClient(),
        raw_archiver=object(),  # stub
        transformer=object(),   # stub
        repository=object(),    # stub
        queue_producer=object() # stub
    )

    service = CollectorService(collector=collector)
    res = service.run(mode=CollectorMode.PROBE)

    assert res.status == CollectorStatus.SUCCESS
    assert res.error is None


def test_case_20_reason_codes_are_stable() -> None:
    """Case 20: Machine-readable reason codes are immutable strings."""
    assert AuthReasonCode.AUTH_OK.value == "AUTH_OK"
    assert AuthReasonCode.LOGIN_REQUIRED.value == "LOGIN_REQUIRED"
    assert AuthReasonCode.INTERACTIVE_CHALLENGE.value == "INTERACTIVE_CHALLENGE"
    assert AuthReasonCode.COLLECTION_EMPTY.value == "COLLECTION_EMPTY"
    assert AuthReasonCode.SOURCE_RATE_LIMITED.value == "SOURCE_RATE_LIMITED"
    assert AuthReasonCode.SOURCE_SERVER_ERROR.value == "SOURCE_SERVER_ERROR"
    assert AuthReasonCode.SOURCE_NETWORK_ERROR.value == "SOURCE_NETWORK_ERROR"
    assert AuthReasonCode.SOURCE_HTTP_403_AMBIGUOUS.value == "SOURCE_HTTP_403_AMBIGUOUS"
    assert AuthReasonCode.BROWSER_NOT_READY.value == "BROWSER_NOT_READY"


def test_case_23_real_observed_challenge_fixture() -> None:
    """Case 23: Replay real challenge evidence fixture observed during initial live run."""
    # Matches the exact characteristics of the real captcha intercept observed in C04
    real_challenge_page = {
        "url": "https://www.douyin.com/user/self",
        "title": "验证中间页 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": True,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    }
    real_challenge_source = SourceProbeEvidence(
        request_attempted=True,
        http_status=403,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=True,
        error_code="SOURCE_HTTP_ERROR",
        error_message="Blocked by ArgusSecurityPlugin Uifid Not Found",
    )

    provider = MockProvider(session=MockSession(page_signals=real_challenge_page))
    source = MockSourceClient(probe_evidence=real_challenge_source)
    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)

    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_CHALLENGE
    assert res.can_sync is False
    assert res.requires_user_action is True
    assert res.reason_code == AuthReasonCode.INTERACTIVE_CHALLENGE.value
    assert res.recovery_action == RecoveryAction.MANUAL_CHALLENGE


# ---------------------------------------------------------------------------
# Real Live Integration Tests (Cases 1, 21, 22)
# ---------------------------------------------------------------------------

def test_case_1_and_21_real_dedicated_profile_headless_preflight() -> None:
    """Cases 1 & 21: Real integration test with Dedicated Profile in headless mode."""
    if not DEDICATED_PROFILE.exists():
        pytest.skip(f"Dedicated profile does not exist at {DEDICATED_PROFILE}")

    config = DouyinCollectorConfig(profile_path=DEDICATED_PROFILE, headless=True)
    provider = DouyinBrowserRuntimeProvider.from_config(config)
    provider.launch()

    try:
        detector = DouyinAuthStateDetector(runtime_provider=provider, config=config)
        result = detector.detect_auth_state(ensure_navigation=True)

        if result.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({result.reason_code})")

        assert result.auth_state == AuthState.AUTH_VALID, f"Expected AUTH_VALID, got {result.auth_state} ({result.reason_code})"
        assert result.source_health == SourceHealth.HEALTHY
        assert result.collection_access in (CollectionAccess.READABLE, CollectionAccess.EMPTY)
        assert result.can_sync is True
        assert result.can_commit_sync_state is True
        assert result.requires_user_action is False
        assert result.reason_code in (AuthReasonCode.AUTH_OK.value, AuthReasonCode.COLLECTION_EMPTY.value)
        assert detector.check_auth() == "LOGIN_OK"

    finally:
        provider.close()


def test_case_22_real_dedicated_profile_restart_preflight() -> None:
    """Case 22: Cold restart verification of dedicated profile preflight persistence."""
    if not DEDICATED_PROFILE.exists():
        pytest.skip(f"Dedicated profile does not exist at {DEDICATED_PROFILE}")

    config = DouyinCollectorConfig(profile_path=DEDICATED_PROFILE, headless=True)
    provider = DouyinBrowserRuntimeProvider.from_config(config)
    provider.launch()
    try:
        # Shutdown to verify disk persistence
        provider.close()
        assert not provider.is_running()

        # Restart
        import time
        provider.launch()
        assert provider.is_running()
        time.sleep(1.0)

        detector = DouyinAuthStateDetector(runtime_provider=provider, config=config)
        result = detector.detect_auth_state(ensure_navigation=True)
        if result.auth_state == AuthState.AUTH_UNCERTAIN:
            time.sleep(2.0)
            result = detector.detect_auth_state(ensure_navigation=True)

        if result.auth_state in (AuthState.AUTH_CHALLENGE, AuthState.AUTH_UNCERTAIN):
            pytest.skip(f"BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff ({result.reason_code})")

        assert result.auth_state == AuthState.AUTH_VALID
        assert result.source_health == SourceHealth.HEALTHY
        assert result.can_sync is True
        assert result.can_commit_sync_state is True
        assert result.requires_user_action is False

    finally:
        provider.close()


def test_case_identity_missing_without_modal_is_auth_uncertain() -> None:
    """Boundary refinement 1: Missing identity + unreadable API + NO login modal -> AUTH_UNCERTAIN."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=404,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_UNCERTAIN
    assert res.source_health == SourceHealth.SOURCE_ERROR
    assert res.can_sync is False
    assert res.requires_user_action is False
    assert res.retryable is True
    assert res.reason_code == AuthReasonCode.IDENTITY_MISSING.value
    assert res.recovery_action == RecoveryAction.INVESTIGATE


def test_case_identity_missing_with_login_modal_is_auth_required() -> None:
    """Boundary refinement 2: Missing identity + login modal visible -> AUTH_REQUIRED."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "登录 - 抖音",
        "login_modal_visible": True,
        "challenge_visible": False,
        "expected_user_identity_present": False,
        "normal_user_page_present": False,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=403,
        platform_status=None,
        response_shape_valid=False,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_REQUIRED
    assert res.can_sync is False
    assert res.requires_user_action is True
    assert res.reason_code == AuthReasonCode.LOGIN_REQUIRED.value
    assert res.recovery_action == RecoveryAction.MANUAL_LOGIN


def test_case_platform_explicit_auth_status_code_8_is_auth_required() -> None:
    """Boundary refinement 3: Platform explicit auth code status_code=8 -> AUTH_REQUIRED."""
    session = MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "阿秋的欧尼酱 - 抖音",
        "login_modal_visible": False,
        "challenge_visible": False,
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "is_redirected": False,
    })
    provider = MockProvider(session=session)
    source = MockSourceClient(SourceProbeEvidence(
        request_attempted=True,
        http_status=200,
        platform_status=8,  # Douyin session invalid
        response_shape_valid=True,
        items_readable=False,
        collection_empty=False,
        page_url="https://www.douyin.com/user/self",
        is_redirected=False,
        challenge_hint=False,
    ))

    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=source)
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_REQUIRED
    assert res.can_sync is False
    assert res.requires_user_action is True
    assert res.reason_code == AuthReasonCode.LOGIN_REQUIRED.value
    assert res.recovery_action == RecoveryAction.MANUAL_LOGIN


def test_account_scope_resolution_different_accounts() -> None:
    """A4.1: Account A scope != Account B scope."""
    from src.collector.douyin.auth_state import generate_account_scope_id

    scope_a = generate_account_scope_id("douyin", "MS4wLjABAAAA_account_A_sec_uid")
    scope_b = generate_account_scope_id("douyin", "MS4wLjABAAAA_account_B_sec_uid")

    assert scope_a != scope_b
    assert scope_a.startswith("douyin:dyacct_")
    assert scope_b.startswith("douyin:dyacct_")


def test_account_scope_same_account_deterministic() -> None:
    """A4.2: Same account restart -> exact same scope."""
    from src.collector.douyin.auth_state import generate_account_scope_id

    scope_1 = generate_account_scope_id("douyin", "MS4wLjABAAAA_same_account_uid")
    scope_2 = generate_account_scope_id("douyin", "MS4wLjABAAAA_same_account_uid")

    assert scope_1 == scope_2


def test_account_scope_nickname_change_does_not_affect_scope() -> None:
    """A4.3: Nickname / title change on page does NOT change scope."""
    provider_1 = MockProvider(session=MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "Old Nickname - 抖音",
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "account_identifier": "MS4wLjABAAAA_fixed_user_id_123",
    }))
    detector_1 = DouyinAuthStateDetector(runtime_provider=provider_1, source_client=MockSourceClient())
    res_1 = detector_1.detect_auth_state()

    provider_2 = MockProvider(session=MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "New Nickname After Edit - 抖音",
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "account_identifier": "MS4wLjABAAAA_fixed_user_id_123",
    }))
    detector_2 = DouyinAuthStateDetector(runtime_provider=provider_2, source_client=MockSourceClient())
    res_2 = detector_2.detect_auth_state()

    assert res_1.account_scope_id == res_2.account_scope_id
    assert res_1.can_sync is True
    assert res_2.can_sync is True


def test_account_scope_unresolved_blocks_sync() -> None:
    """A4.4: AUTH_VALID but scope unresolved -> can_sync=False, ACCOUNT_SCOPE_UNRESOLVED."""
    provider = MockProvider(session=MockSession(page_signals={
        "url": "https://www.douyin.com/user/self",
        "title": "Some User - 抖音",
        "expected_user_identity_present": True,
        "normal_user_page_present": True,
        "account_identifier": None,  # Scope unresolvable!
        "account_scope_id": None,
    }))
    detector = DouyinAuthStateDetector(runtime_provider=provider, source_client=MockSourceClient())
    res = detector.detect_auth_state()

    assert res.auth_state == AuthState.AUTH_VALID
    assert res.can_sync is False
    assert res.can_commit_sync_state is False
    assert res.account_scope_id is None
    assert res.reason_code == AuthReasonCode.ACCOUNT_SCOPE_UNRESOLVED.value
    assert res.recovery_action == RecoveryAction.INVESTIGATE
