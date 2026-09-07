"""Tests for Douyin In-Browser SourceClient (DY-C04).

Validates:
- Protocol compliance (SourceClient)
- Request validation (count > 0, cursor >= 0)
- Cursor precision preservation across string boundary (16-digit Douyin cursors)
- Malformed response handling (missing status_code, non-list aweme_list, non-dict payload)
- HTTP error mapping (403, 429, 500 -> SourceHttpError)
- Platform error mapping (status_code != 0 -> SourcePlatformError)
- Timeout error mapping (TIMEOUT -> SourceTimeoutError)
- Unknown protocol method handling
- Raw response preservation for C06 RawArchiver
- Zero secrets / credentials in IPC params and logs
- SourceProbeEvidence capture for C03 AuthState integration
- DouyinCollector slot integration
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.collector.base import CollectorMode, CollectorStatus
from src.collector.douyin import (
    CollectionPage,
    DouyinBrowserRuntimeProvider,
    DouyinCollector,
    DouyinCollectorConfig,
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
from src.collector.interfaces import BrowserRuntimeProvider, SourceClient


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

class MockBrowserSession:
    """Mock BrowserSession for testing SourceClient RPC interactions."""

    def __init__(self, rpc_handler=None) -> None:
        self.rpc_handler = rpc_handler or (lambda method, params, timeout: {})
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        params = params or {}
        self.calls.append((method, params, timeout))
        return self.rpc_handler(method, params, timeout)


class MockRuntimeProvider(BrowserRuntimeProvider):
    """Mock runtime provider satisfying BrowserRuntimeProvider protocol."""

    def __init__(self, session: MockBrowserSession | None = None, running: bool = True) -> None:
        self._session = session or MockBrowserSession()
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def launch(self) -> None:
        self._running = True

    def close(self) -> None:
        self._running = False

    def health(self) -> dict[str, Any]:
        return {"healthy": self._running}

    def info(self) -> dict[str, Any]:
        return {"running": self._running}

    def restart(self) -> None:
        pass

    def get_session(self) -> MockBrowserSession:
        return self._session


def make_valid_douyin_response(
    cursor: str = "1788528929463841",
    has_more: int = 1,
    item_count: int = 2,
    status_code: int = 0,
) -> dict[str, Any]:
    items = [
        {
            "aweme_id": f"741234567890123456{i}",
            "desc": f"Test Douyin collection item {i}",
            "author": {"nickname": f"Author_{i}", "sec_uid": f"SEC_{i}"},
        }
        for i in range(item_count)
    ]
    raw = {
        "status_code": status_code,
        "cursor": cursor,
        "has_more": has_more,
        "aweme_list": items,
        "log_pb": {"impr_id": "20260905_impr_test"},
    }
    return {
        "http_status": 200,
        "platform_status": status_code,
        "raw_response": raw,
        "latency_ms": 142.5,
        "page_url": "https://www.douyin.com/user/self?showTab=favorite_collection",
        "cursor_returned": cursor,
        "has_more": bool(has_more),
        "items_count": len(items),
    }


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

def test_source_client_protocol_compliance() -> None:
    """Verify DouyinSourceClient satisfies SourceClient protocol."""
    provider = MockRuntimeProvider()
    client = DouyinSourceClient(runtime_provider=provider)
    assert isinstance(client, SourceClient)
    assert issubclass(DouyinSourceClient, SourceClient)
    assert hasattr(client, "fetch_page")
    assert hasattr(client, "fetch_collection_page")
    assert hasattr(client, "probe_source")


def test_request_validation_count_and_cursor() -> None:
    """Verify strict validation of count (> 0) and cursor (>= 0 decimal string)."""
    provider = MockRuntimeProvider()
    client = DouyinSourceClient(runtime_provider=provider)

    # Invalid count: 0 or negative
    with pytest.raises(SourceInvalidRequestError) as exc_info:
        client.fetch_collection_page(count=0)
    assert "count must be an integer > 0" in str(exc_info.value)

    with pytest.raises(SourceInvalidRequestError) as exc_info:
        client.fetch_collection_page(count=-10)
    assert "count must be an integer > 0" in str(exc_info.value)

    # Invalid cursor: negative, non-numeric string
    with pytest.raises(SourceInvalidRequestError) as exc_info:
        client.fetch_collection_page(cursor="-1")
    assert "cursor must be a non-negative decimal string" in str(exc_info.value)

    with pytest.raises(SourceInvalidRequestError) as exc_info:
        client.fetch_collection_page(cursor="invalid_cursor_xyz")
    assert "cursor must be a non-negative decimal string" in str(exc_info.value)

    # Valid cursor normalizations
    assert client._validate_and_normalize_cursor(0) == "0"
    assert client._validate_and_normalize_cursor("0") == "0"
    assert client._validate_and_normalize_cursor(1788528929463841) == "1788528929463841"
    assert client._validate_and_normalize_cursor("1788528929463841") == "1788528929463841"
    assert client._validate_and_normalize_cursor(None) == "0"


def test_cursor_precision_round_trip() -> None:
    """Verify 16-digit Douyin cursors are passed as decimal strings without precision loss."""
    test_cursors = [
        "1788528929463841",
        "1702202852184224",
        "1641058690000000",
        "0",
    ]

    for req_cursor in test_cursors:
        expected_next_cursor = "1799999999999999"

        def handler(method: str, params: dict[str, Any], timeout: float):
            assert method == "douyin.collection.fetch_page"
            # Crucial: verify cursor received by RPC is string matching req_cursor exactly
            assert isinstance(params["cursor"], str)
            assert params["cursor"] == req_cursor
            return make_valid_douyin_response(cursor=expected_next_cursor)

        session = MockBrowserSession(rpc_handler=handler)
        provider = MockRuntimeProvider(session=session)
        client = DouyinSourceClient(runtime_provider=provider)

        page = client.fetch_collection_page(cursor=req_cursor, count=10)

        assert isinstance(page, CollectionPage)
        assert page.request_cursor == req_cursor
        assert page.response_cursor == expected_next_cursor
        assert isinstance(page.request_cursor, str)
        assert isinstance(page.response_cursor, str)
        assert page.has_more is True
        assert len(page.items) == 2


def test_runtime_not_running_raises_error() -> None:
    """Verify attempting to fetch when runtime is stopped raises SourceBrowserNotReadyError."""
    provider = MockRuntimeProvider(running=False)
    client = DouyinSourceClient(runtime_provider=provider)

    with pytest.raises(SourceClientError) as exc_info:
        client.fetch_collection_page(cursor=0, count=10)

    assert exc_info.value.source_code == SourceClientErrorCode.SOURCE_BROWSER_NOT_READY


def test_malformed_response_handling() -> None:
    """Verify malformed sidecar or platform responses raise SourceResponseInvalidError."""
    # 1. Non-dict RPC response
    provider = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: "not_a_dict"))
    client = DouyinSourceClient(runtime_provider=provider)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client.fetch_collection_page()
    assert "expected dict" in str(exc.value)

    # 2. Missing http_status
    provider = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {"raw_response": {}}))
    client = DouyinSourceClient(runtime_provider=provider)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client.fetch_collection_page()
    assert "Missing http_status" in str(exc.value)

    # 3. HTTP 200 but raw_response is not dict
    provider = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {"http_status": 200, "raw_response": "<html>"}))
    client = DouyinSourceClient(runtime_provider=provider)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client.fetch_collection_page()
    assert "Failed to parse listcollection JSON" in str(exc.value)

    # 4. HTTP 200 but missing status_code
    provider = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {"http_status": 200, "raw_response": {"cursor": 0}}))
    client = DouyinSourceClient(runtime_provider=provider)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client.fetch_collection_page()
    assert "missing required 'status_code'" in str(exc.value)

    # 5. HTTP 200 but aweme_list is not a list (invalid type)
    provider = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {
        "http_status": 200,
        "raw_response": {"status_code": 0, "cursor": 0, "has_more": 0, "aweme_list": "invalid_string"}
    }))
    client = DouyinSourceClient(runtime_provider=provider)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client.fetch_collection_page()
    assert "not a list" in str(exc.value)

    # 6. HTTP 200 with aweme_list=None and has_more=0 (valid platform representation of empty terminal page)
    provider_terminal = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {
        "http_status": 200,
        "raw_response": {"status_code": 0, "cursor": 0, "has_more": 0, "aweme_list": None}
    }))
    client_terminal = DouyinSourceClient(runtime_provider=provider_terminal)
    page = client_terminal.fetch_collection_page()
    assert page.items == []
    assert page.has_more is False

    # 7. HTTP 200 with aweme_list=None but has_more=1 (invalid: claims more items exist while omitting list)
    provider_invalid_terminal = MockRuntimeProvider(session=MockBrowserSession(lambda m, p, t: {
        "http_status": 200,
        "raw_response": {"status_code": 0, "cursor": 1788000000000000, "has_more": 1, "aweme_list": None}
    }))
    client_invalid = DouyinSourceClient(runtime_provider=provider_invalid_terminal)
    with pytest.raises(SourceResponseInvalidError) as exc:
        client_invalid.fetch_collection_page()
    assert "indicates more items" in str(exc.value)


def test_http_error_mapping() -> None:
    """Verify non-200 HTTP status is mapped to SourceHttpError with code preserved.
    
    Critically, C04 must NOT prematurely classify HTTP 403 as AUTH_REQUIRED.
    """
    for code in [403, 429, 500, 502]:
        def handler(method: str, params: dict[str, Any], timeout: float):
            return {
                "http_status": code,
                "platform_status": None,
                "raw_response": None,
                "parse_error": f"HTTP {code} Error",
                "page_url": "https://www.douyin.com/user/self",
            }

        provider = MockRuntimeProvider(session=MockBrowserSession(handler))
        client = DouyinSourceClient(runtime_provider=provider)

        with pytest.raises(SourceHttpError) as exc_info:
            client.fetch_collection_page()

        err = exc_info.value
        assert err.source_code == SourceClientErrorCode.SOURCE_HTTP_ERROR
        assert err.http_status == code
        assert f"HTTP {code}" in err.message
        # Confirm no premature enum conversion to AuthState
        assert not hasattr(err, "auth_state")


def test_platform_error_mapping() -> None:
    """Verify non-zero platform status_code raises SourcePlatformError."""
    def handler(method: str, params: dict[str, Any], timeout: float):
        return {
            "http_status": 200,
            "platform_status": 2154,
            "raw_response": {
                "status_code": 2154,
                "status_msg": "Access Denied: verification required",
                "aweme_list": [],
            },
        }

    provider = MockRuntimeProvider(session=MockBrowserSession(handler))
    client = DouyinSourceClient(runtime_provider=provider)

    with pytest.raises(SourcePlatformError) as exc_info:
        client.fetch_collection_page()

    err = exc_info.value
    assert err.source_code == SourceClientErrorCode.SOURCE_PLATFORM_ERROR
    assert err.platform_status == 2154
    assert "2154" in err.message


def test_timeout_error_mapping() -> None:
    """Verify fetch_error == 'TIMEOUT' raises SourceTimeoutError."""
    def handler(method: str, params: dict[str, Any], timeout: float):
        return {
            "http_status": None,
            "platform_status": None,
            "raw_response": None,
            "fetch_error": "TIMEOUT",
        }

    provider = MockRuntimeProvider(session=MockBrowserSession(handler))
    client = DouyinSourceClient(runtime_provider=provider, max_transport_retries=0)

    with pytest.raises(SourceTimeoutError) as exc_info:
        client.fetch_collection_page()

    assert exc_info.value.source_code == SourceClientErrorCode.SOURCE_TIMEOUT


def test_raw_response_preservation() -> None:
    """Verify CollectionPage.raw_response preserves complete untouched API payload."""
    raw_payload = {
        "status_code": 0,
        "cursor": "1788528929463841",
        "has_more": 1,
        "aweme_list": [{"aweme_id": "111", "desc": "sample 1"}],
        "log_pb": {"impr_id": "test_impr"},
        "extra": {"now": 1725520000, "fatal_item_ids": []},
    }

    def handler(method: str, params: dict[str, Any], timeout: float):
        return {
            "http_status": 200,
            "platform_status": 0,
            "raw_response": raw_payload,
            "latency_ms": 95.0,
            "page_url": "https://www.douyin.com/user/self",
        }

    provider = MockRuntimeProvider(session=MockBrowserSession(handler))
    client = DouyinSourceClient(runtime_provider=provider)

    page = client.fetch_collection_page(cursor="0", count=10)

    assert page.raw_response == raw_payload
    assert page.raw_response["extra"]["fatal_item_ids"] == []
    assert page.raw_response["log_pb"]["impr_id"] == "test_impr"
    assert page.items == [{"aweme_id": "111", "desc": "sample 1"}]


def test_no_secret_in_ipc_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Verify zero cookies, tokens, or credential headers are sent via IPC or logged."""
    session = MockBrowserSession(rpc_handler=lambda m, p, t: make_valid_douyin_response())
    provider = MockRuntimeProvider(session=session)
    client = DouyinSourceClient(runtime_provider=provider)

    with caplog.at_level(logging.DEBUG):
        client.fetch_collection_page(cursor="100", count=20)

    # 1. Verify IPC params contain ONLY cursor, count, timeout_ms
    assert len(session.calls) == 1
    method, params, _ = session.calls[0]
    assert method == "douyin.collection.fetch_page"
    assert set(params.keys()) == {"cursor", "count", "timeout_ms"}
    assert "cookie" not in params
    assert "token" not in params
    assert "sessionid" not in params

    # 2. Verify logs contain no sensitive credential keywords
    log_text = caplog.text.lower()
    assert "sessionid" not in log_text
    assert "passport" not in log_text
    assert "cookie" not in log_text


def test_source_probe_evidence_capture() -> None:
    """Verify probe_source captures multi-signal evidence without unhandled exceptions."""
    # 1. When runtime not running
    stopped_provider = MockRuntimeProvider(running=False)
    client_stopped = DouyinSourceClient(runtime_provider=stopped_provider)
    ev_stopped = client_stopped.probe_source()

    assert isinstance(ev_stopped, SourceProbeEvidence)
    assert ev_stopped.request_attempted is False
    assert ev_stopped.http_status is None
    assert ev_stopped.error_code == SourceClientErrorCode.SOURCE_BROWSER_NOT_READY.value

    # 2. Under HTTP 403 challenge
    def challenge_handler(method: str, params: dict[str, Any], timeout: float):
        if method == "douyin.auth.page_signals":
            return {
                "url": "https://www.douyin.com/user/self",
                "title": "安全验证 - 验证中间页",
                "is_login_modal_present": False,
                "has_avatar": False,
                "has_captcha": True,
            }
        if method == "douyin.collection.fetch_page":
            return {
                "http_status": 403,
                "platform_status": None,
                "raw_response": None,
                "parse_error": "Blocked by ArgusSecurityPlugin",
                "page_url": "https://www.douyin.com/user/self",
            }
        return {}

    session_challenge = MockBrowserSession(rpc_handler=challenge_handler)
    client_challenge = DouyinSourceClient(runtime_provider=MockRuntimeProvider(session=session_challenge))
    ev_challenge = client_challenge.probe_source()

    assert ev_challenge.request_attempted is True
    assert ev_challenge.http_status == 403
    assert ev_challenge.challenge_hint is True
    assert ev_challenge.response_shape_valid is False
    assert ev_challenge.items_readable is False

    # 3. Under normal healthy response
    def healthy_handler(method: str, params: dict[str, Any], timeout: float):
        if method == "douyin.auth.page_signals":
            return {
                "url": "https://www.douyin.com/user/self?showTab=favorite_collection",
                "title": "我的主页 - 抖音",
                "is_login_modal_present": False,
                "has_avatar": True,
                "has_captcha": False,
            }
        if method == "douyin.collection.fetch_page":
            return make_valid_douyin_response()
        return {}

    session_healthy = MockBrowserSession(rpc_handler=healthy_handler)
    client_healthy = DouyinSourceClient(runtime_provider=MockRuntimeProvider(session=session_healthy))
    ev_healthy = client_healthy.probe_source()

    assert ev_healthy.request_attempted is True
    assert ev_healthy.http_status == 200
    assert ev_healthy.platform_status == 0
    assert ev_healthy.response_shape_valid is True
    assert ev_healthy.items_readable is True
    assert ev_healthy.challenge_hint is False


def test_source_client_protocol_fetch_page() -> None:
    """Verify SourceClient protocol method fetch_page returns raw dictionary."""
    session = MockBrowserSession(rpc_handler=lambda m, p, t: make_valid_douyin_response())
    provider = MockRuntimeProvider(session=session)
    client = DouyinSourceClient(runtime_provider=provider)

    raw = client.fetch_page(cursor=0, count=10)
    assert isinstance(raw, dict)
    assert raw["status_code"] == 0
    assert "aweme_list" in raw
    assert len(raw["aweme_list"]) == 2


def test_douyin_collector_wire_source_client() -> None:
    """Verify DouyinCollector accepts DouyinSourceClient and reflects ready status."""
    config = DouyinCollectorConfig()
    session = MockBrowserSession()
    provider = MockRuntimeProvider(session=session)
    source_client = DouyinSourceClient(runtime_provider=provider, config=config)

    collector = DouyinCollector(
        config=config,
        browser_runtime=provider,
        source_client=source_client,
    )

    probe_res = collector.probe("run_probe_c04")
    assert probe_res.metrics["source_client_ready"] is True
    assert probe_res.metrics["browser_runtime_ready"] is True
    assert "SourceClient (C04)" not in probe_res.metrics["missing_dependencies"]


def test_real_source_probe_failure_evidence(tmp_path: Path) -> None:
    """Real integration test: verify DouyinSourceClient accurately captures signals/failure evidence."""
    provider = DouyinBrowserRuntimeProvider(
        profile_path=tmp_path,
        headless=True,
    )
    provider.launch()
    try:
        assert provider.is_running()
        client = DouyinSourceClient(runtime_provider=provider)

        # Real in-browser probe execution
        evidence = client.probe_source()
        assert isinstance(evidence, SourceProbeEvidence)
        assert evidence.request_attempted is True
        assert evidence.page_url != ""
        # In unauthenticated/temp profile, probe must capture failure signals accurately
        assert evidence.response_shape_valid is False or evidence.http_status == 200

        # Verify sidecar allowlist rejection of prohibited method via session
        session = provider.get_session()
        with pytest.raises(Exception) as exc:
            session.request("prohibited.mutation.endpoint", {"foo": "bar"})
        assert "not permitted by sidecar allowlist" in str(exc.value)
        assert exc.value.details.get("code") == "PROTOCOL_METHOD_NOT_ALLOWED"

    finally:
        provider.close()
        assert not provider.is_running()


def test_real_listcollection_fetch_success() -> None:
    """Real integration test: verify dedicated profile achieves REAL SUCCESS fetch.
    
    Strict invariant: ONLY passes when HTTP 200, status_code=0, and genuine aweme_list returned.
    If dedicated profile is currently challenged by captcha, explicitly skips with reason.
    """
    config = DouyinCollectorConfig()
    from src.collector.douyin.browser_runtime import DEFAULT_PROFILE_PATH
    profile_dir = Path(config.profile_path or DEFAULT_PROFILE_PATH).resolve()
    if not profile_dir.exists():
        pytest.skip(f"Dedicated profile path does not exist: {profile_dir}")

    provider = DouyinBrowserRuntimeProvider(
        profile_path=profile_dir,
        headless=True,
    )
    provider.launch()
    try:
        client = DouyinSourceClient(runtime_provider=provider, config=config)

        # First probe for challenge state
        probe = client.probe_source()
        if probe.challenge_hint:
            pytest.skip("BLOCKED: Dedicated profile currently requires manual human captcha verification (AUTH_CHALLENGE_REQUIRES_USER_ACTION)")

        # Execute genuine fetch
        page = client.fetch_collection_page(cursor="0", count=1)
        assert page.http_status == 200
        assert page.platform_status == 0
        assert len(page.items) >= 1
        assert "aweme_id" in page.items[0]

    except SourceHttpError as e:
        if e.http_status == 403 or (e.details and e.details.get("challenge_active")):
            pytest.skip(f"BLOCKED: Douyin platform returned HTTP 403/Challenge ({e.message}). Human verification required.")
        raise
    finally:
        provider.close()


