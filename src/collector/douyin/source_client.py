"""Douyin In-Browser SourceClient for collection page retrieval (DY-C04).

Encapsulates authenticated listcollection pagination requests executed directly
within the persistent browser page context.
Features:
- Strict read-only contract for POST /aweme/v1/web/aweme/listcollection/
- In-browser dynamic SDK signing (zero custom a_bogus / msToken calculation)
- String-based cursor precision preservation across Python, JSON, and Node
- Strict request validation (count > 0, cursor >= 0)
- Structured CollectionPage and SourceProbeEvidence output models
- Granular SourceClientError hierarchy (no premature AuthState classification)
- Zero credential / sensitive query parameter leakage in logs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..base import utcnow_iso
from ..errors import CollectorError, CollectorErrorCode
from ..interfaces import BrowserRuntimeProvider, SourceClient
from .browser_runtime import BrowserSession, DouyinBrowserRuntimeProvider
from .config import DouyinCollectorConfig

logger = logging.getLogger("collector.douyin.source")

API_LISTCOLLECTION = "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"


# ---------------------------------------------------------------------------
# Error Taxonomy
# ---------------------------------------------------------------------------

class SourceClientErrorCode(str, Enum):
    SOURCE_INVALID_REQUEST = "SOURCE_INVALID_REQUEST"
    SOURCE_BROWSER_NOT_READY = "SOURCE_BROWSER_NOT_READY"
    SOURCE_CONTEXT_NOT_READY = "SOURCE_CONTEXT_NOT_READY"
    SOURCE_HTTP_ERROR = "SOURCE_HTTP_ERROR"
    SOURCE_RESPONSE_INVALID = "SOURCE_RESPONSE_INVALID"
    SOURCE_PLATFORM_ERROR = "SOURCE_PLATFORM_ERROR"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_PROTOCOL_ERROR = "SOURCE_PROTOCOL_ERROR"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"


class SourceClientError(CollectorError):
    """Base exception for in-browser SourceClient failures."""

    def __init__(
        self,
        source_code: SourceClientErrorCode,
        message: str,
        http_status: int | None = None,
        platform_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(CollectorErrorCode.RUNTIME_ERROR, message, details)
        self.source_code = source_code
        self.http_status = http_status
        self.platform_status = platform_status

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["source_code"] = self.source_code.value
        base["http_status"] = self.http_status
        base["platform_status"] = self.platform_status
        return base


class SourceInvalidRequestError(SourceClientError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_INVALID_REQUEST, message, details=details)


class SourceBrowserNotReadyError(SourceClientError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_BROWSER_NOT_READY, message, details=details)


class SourceHttpError(SourceClientError):
    def __init__(self, message: str, http_status: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_HTTP_ERROR, message, http_status=http_status, details=details)


class SourceResponseInvalidError(SourceClientError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_RESPONSE_INVALID, message, details=details)


class SourcePlatformError(SourceClientError):
    def __init__(self, message: str, platform_status: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_PLATFORM_ERROR, message, platform_status=platform_status, details=details)


class SourceTimeoutError(SourceClientError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(SourceClientErrorCode.SOURCE_TIMEOUT, message, details=details)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollectionPage:
    """Strongly-typed container for a single fetched raw collection page."""
    request_cursor: str
    response_cursor: str
    has_more: bool
    items: list[dict[str, Any]]
    raw_response: dict[str, Any]
    http_status: int
    platform_status: int
    fetched_at: str
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_cursor": self.request_cursor,
            "response_cursor": self.response_cursor,
            "has_more": self.has_more,
            "items_count": len(self.items),
            "http_status": self.http_status,
            "platform_status": self.platform_status,
            "fetched_at": self.fetched_at,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class SourceProbeEvidence:
    """Rich multi-signal evidence collected for C03 AuthState resolution."""
    request_attempted: bool
    http_status: int | None
    platform_status: int | None
    response_shape_valid: bool
    items_readable: bool
    page_url: str
    is_redirected: bool
    challenge_hint: bool
    collection_empty: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_attempted": self.request_attempted,
            "http_status": self.http_status,
            "platform_status": self.platform_status,
            "response_shape_valid": self.response_shape_valid,
            "items_readable": self.items_readable,
            "collection_empty": self.collection_empty,
            "page_url": self.page_url,
            "is_redirected": self.is_redirected,
            "challenge_hint": self.challenge_hint,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


# ---------------------------------------------------------------------------
# Douyin Source Client Implementation
# ---------------------------------------------------------------------------

class DouyinSourceClient(SourceClient):
    """In-browser authenticated client for Douyin listcollection endpoint."""

    def __init__(
        self,
        runtime_provider: BrowserRuntimeProvider,
        config: DouyinCollectorConfig | None = None,
        default_timeout_sec: float = 20.0,
        max_transport_retries: int = 1,
    ) -> None:
        self.runtime_provider = runtime_provider
        self.config = config or DouyinCollectorConfig()
        self.default_timeout_sec = default_timeout_sec
        self.max_transport_retries = max_transport_retries

    # -----------------------------------------------------------------------
    # SourceClient Protocol Implementation
    # -----------------------------------------------------------------------

    def fetch_page(self, cursor: int | str, count: int) -> dict[str, Any]:
        """Fetch raw JSON dictionary satisfying SourceClient Protocol."""
        page = self.fetch_collection_page(cursor=cursor, count=count)
        return page.raw_response

    # -----------------------------------------------------------------------
    # Formal Typed Public API
    # -----------------------------------------------------------------------

    def fetch_collection_page(
        self,
        cursor: int | str = "0",
        count: int | None = None,
        timeout_sec: float | None = None,
    ) -> CollectionPage:
        """Fetch a single page of genuine collection items via in-browser execution.
        
        Args:
            cursor: Starting position boundary (decimal string or integer >= 0).
            count: Number of items to retrieve (> 0). Defaults to config.page_size.
            timeout_sec: Execution timeout in seconds.
        """
        req_cursor_str = self._validate_and_normalize_cursor(cursor)
        req_count_int = self._validate_and_normalize_count(count)
        timeout = timeout_sec or self.default_timeout_sec

        if not self.runtime_provider.is_running():
            raise SourceBrowserNotReadyError(
                "Cannot fetch collection page: browser runtime is not running.",
                details={"runtime_running": False},
            )

        session = self._get_session()
        params = {
            "cursor": req_cursor_str,
            "count": req_count_int,
            "timeout_ms": int(timeout * 1000),
        }

        # Bounded transport retry loop (0 or max 1 retry)
        last_error: Exception | None = None
        for attempt in range(self.max_transport_retries + 1):
            try:
                fetched_at = utcnow_iso()
                logger.info(
                    f"Executing listcollection fetch (cursor={req_cursor_str}, count={req_count_int}, "
                    f"attempt={attempt+1}/{self.max_transport_retries+1})"
                )

                rpc_res = session.request("douyin.collection.fetch_page", params, timeout=timeout + 5.0)
                return self._parse_and_validate_response(
                    rpc_res=rpc_res,
                    request_cursor=req_cursor_str,
                    fetched_at=fetched_at,
                )

            except (SourceTimeoutError, SourceBrowserNotReadyError) as e:
                last_error = e
                if attempt < self.max_transport_retries:
                    logger.warning(f"Transport failure on attempt {attempt+1}: {e.message}. Retrying...")
                    continue
                raise
            except Exception as e:
                # Non-transient errors fail immediately
                raise

        if last_error:
            raise last_error
        raise SourceClientError(SourceClientErrorCode.SOURCE_UNKNOWN, "Unknown fetch failure")

    def probe_source(self) -> SourceProbeEvidence:
        """Perform a non-destructive inspection of source availability and signals.
        
        Returns raw evidence for downstream C03 AuthState consumption without
        making a premature final authentication classification.
        """
        if not self.runtime_provider.is_running():
            return SourceProbeEvidence(
                request_attempted=False,
                http_status=None,
                platform_status=None,
                response_shape_valid=False,
                items_readable=False,
                page_url="",
                is_redirected=False,
                challenge_hint=False,
                error_code=SourceClientErrorCode.SOURCE_BROWSER_NOT_READY.value,
                error_message="Browser runtime is not running",
            )

        session = self._get_session()

        # Check page context signals
        page_url = ""
        is_redirected = False
        challenge_hint = False

        try:
            signals = session.request("douyin.auth.page_signals", timeout=10.0)
            page_url = signals.get("url", "")
            title = signals.get("title", "")
            is_redirected = not page_url.startswith("https://www.douyin.com/user/self")
            challenge_hint = Boolean(
                signals.get("has_captcha")
                or "verify" in page_url
                or "安全验证" in title
                or "验证码" in title
                or "验证中间页" in title
            )
        except Exception as e:
            logger.debug(f"Failed to query page signals: {e}")

        # Attempt probe fetch with cursor=0, count=1
        try:
            rpc_res = session.request(
                "douyin.collection.fetch_page",
                {"cursor": "0", "count": 1, "timeout_ms": 15000},
                timeout=20.0,
            )
            http_status = rpc_res.get("http_status")
            platform_status = rpc_res.get("platform_status")
            raw = rpc_res.get("raw_response")
            page_url = rpc_res.get("page_url") or page_url

            shape_valid = isinstance(raw, dict) and "aweme_list" in raw and "status_code" in raw
            items_readable = shape_valid and isinstance(raw.get("aweme_list"), list)
            collection_empty = items_readable and (len(raw.get("aweme_list", [])) == 0)

            return SourceProbeEvidence(
                request_attempted=True,
                http_status=http_status,
                platform_status=platform_status,
                response_shape_valid=shape_valid,
                items_readable=items_readable,
                collection_empty=collection_empty,
                page_url=page_url,
                is_redirected=is_redirected,
                challenge_hint=Boolean(challenge_hint or (http_status == 403) or rpc_res.get("challenge_active")),
            )

        except SourceClientError as e:
            return SourceProbeEvidence(
                request_attempted=True,
                http_status=e.http_status,
                platform_status=e.platform_status,
                response_shape_valid=False,
                items_readable=False,
                page_url=page_url,
                is_redirected=is_redirected,
                challenge_hint=challenge_hint or (e.http_status == 403),
                error_code=e.source_code.value,
                error_message=e.message,
            )
        except Exception as e:
            return SourceProbeEvidence(
                request_attempted=True,
                http_status=None,
                platform_status=None,
                response_shape_valid=False,
                items_readable=False,
                page_url=page_url,
                is_redirected=is_redirected,
                challenge_hint=challenge_hint,
                error_code=SourceClientErrorCode.SOURCE_UNKNOWN.value,
                error_message=str(e),
            )

    # -----------------------------------------------------------------------
    # Internal Validation & Response Parsing
    # -----------------------------------------------------------------------

    def _validate_and_normalize_cursor(self, cursor: int | str) -> str:
        """Validate that cursor is a valid non-negative integer representation."""
        if cursor is None:
            return "0"
        s = str(cursor).strip()
        if not s.isdigit():
            raise SourceInvalidRequestError(
                f"Invalid cursor value '{cursor}': cursor must be a non-negative decimal string or integer.",
                details={"cursor": cursor},
            )
        return s

    def _validate_and_normalize_count(self, count: int | None) -> int:
        """Validate that count is a positive integer."""
        c = count if count is not None else self.config.page_size
        if not isinstance(c, int) or c <= 0:
            raise SourceInvalidRequestError(
                f"Invalid count value '{count}': page count must be an integer > 0.",
                details={"count": count, "resolved": c},
            )
        return c

    def _get_session(self) -> BrowserSession:
        if hasattr(self.runtime_provider, "get_session"):
            return self.runtime_provider.get_session()
        raise SourceBrowserNotReadyError("Provided runtime does not support get_session()")

    def _parse_and_validate_response(
        self,
        rpc_res: dict[str, Any],
        request_cursor: str,
        fetched_at: str,
    ) -> CollectionPage:
        """Parse RPC response payload and strictly enforce response shape contracts."""
        if not isinstance(rpc_res, dict):
            raise SourceResponseInvalidError(
                f"Malformed sidecar response: expected dict, got {type(rpc_res).__name__}",
                details={"raw_rpc": str(rpc_res)},
            )

        fetch_err = rpc_res.get("fetch_error")
        if fetch_err:
            if fetch_err == "TIMEOUT":
                raise SourceTimeoutError("listcollection fetch timed out in browser context.")
            raise SourceClientError(
                SourceClientErrorCode.SOURCE_UNKNOWN,
                f"In-browser network transport failure: {fetch_err}",
                details={"fetch_error": fetch_err},
            )

        http_status = rpc_res.get("http_status")
        if http_status is None:
            raise SourceResponseInvalidError("Missing http_status in sidecar response payload.")

        # HTTP error boundary (e.g. 403, 429, 500)
        if http_status != 200:
            parse_err = rpc_res.get("parse_error")
            page_url = rpc_res.get("page_url", "")
            raise SourceHttpError(
                f"listcollection endpoint returned HTTP {http_status} (url='{page_url}'). "
                f"Details: {parse_err or 'Non-200 HTTP response'}",
                http_status=http_status,
                details={"http_status": http_status, "page_url": page_url, "parse_error": parse_err},
            )

        raw = rpc_res.get("raw_response")
        if not isinstance(raw, dict):
            parse_err = rpc_res.get("parse_error")
            raise SourceResponseInvalidError(
                f"Failed to parse listcollection JSON response: {parse_err or 'Payload not JSON'}",
                details={"http_status": http_status, "parse_error": parse_err},
            )

        platform_status = raw.get("status_code")
        if platform_status is None:
            raise SourceResponseInvalidError(
                "Platform response missing required 'status_code' field.",
                details={"raw_keys": list(raw.keys())},
            )

        # Non-zero platform status code indicates business / auth / parameter error
        if platform_status != 0:
            status_msg = raw.get("status_msg") or raw.get("prompts") or "Platform error"
            raise SourcePlatformError(
                f"listcollection returned platform status_code {platform_status}: {status_msg}",
                platform_status=platform_status,
                details={"status_code": platform_status, "status_msg": status_msg},
            )

        has_more_raw = raw.get("has_more")
        has_more = bool(has_more_raw) if has_more_raw is not None else False
        resp_cursor = str(raw.get("cursor", "0"))

        aweme_list = raw.get("aweme_list")
        if aweme_list is None:
            # Strict terminal response guard: aweme_list=null is ONLY accepted on terminal end of listcollection
            # where platform status_code == 0 and has_more is False/0.
            if not has_more:
                aweme_list = []
            else:
                raise SourceResponseInvalidError(
                    "Platform response 'aweme_list' is None but 'has_more' indicates more items.",
                    details={"has_more": has_more_raw, "cursor": resp_cursor},
                )
        elif not isinstance(aweme_list, list):
            raise SourceResponseInvalidError(
                f"Platform response 'aweme_list' is not a list (got {type(aweme_list).__name__}).",
                details={"raw_keys": list(raw.keys())},
            )
        latency_ms = float(rpc_res.get("latency_ms", 0.0))

        logger.info(
            f"Successfully fetched listcollection page: {len(aweme_list)} items, "
            f"has_more={has_more}, next_cursor={resp_cursor}, latency={latency_ms:.1f}ms"
        )

        return CollectionPage(
            request_cursor=request_cursor,
            response_cursor=resp_cursor,
            has_more=has_more,
            items=aweme_list,
            raw_response=raw,
            http_status=http_status,
            platform_status=platform_status,
            fetched_at=fetched_at,
            latency_ms=latency_ms,
        )


def Boolean(val: Any) -> bool:
    """Helper boolean coercion."""
    return bool(val)
