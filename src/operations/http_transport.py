"""M6-07 HTTP worker transport: protocol DTOs, error contract, and client.

This module owns the *wire* between a remote Windows worker and the NAS
control plane. It deliberately contains:

- the versioned protocol constants (``m6-control-plane-api-v1``)
- the machine-readable error contract
- DTO serialization helpers (ClaimedJob <-> dict)
- the ``HttpWorkerTransport`` client (auth, timeouts, retry, idempotency)

It does NOT contain state-machine rules — every transition is executed by the
server (control plane) against the Operations store. The client only carries
requests and maps structured errors back to the transport exceptions the
WorkerRuntime already understands (``StaleLeaseError``,
``RemoteUnavailableError``, ...).

Secret policy: the lease token IS returned to the owning worker over the
authenticated RPC (it must fence the worker's own start/renew/complete calls),
but it must never appear in logs/observability/CLI. The client keeps it in the
in-memory ClaimedJob only; server responses otherwise strip it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .models import ClaimedJob
from .store import StaleLeaseError
from .transport import (
    RemoteUnavailableError,
    TransportError,
    WorkerOperationsTransport,
)

__all__ = [
    "CONTROL_PLANE_API_VERSION",
    "ERROR_CODE_AUTH_FAILED",
    "ERROR_CODE_VALIDATION_ERROR",
    "ERROR_CODE_NO_JOB",
    "ERROR_CODE_STALE_LEASE",
    "ERROR_CODE_CONFLICT",
    "ERROR_CODE_NOT_FOUND",
    "ERROR_CODE_SERVER_UNAVAILABLE",
    "ERROR_CODE_INVARIANT_ERROR",
    "ApiError",
    "AuthenticationError",
    "HttpWorkerTransport",
    "claimed_job_to_dict",
    "claimed_job_from_dict",
    "redact_job_payload",
]

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

CONTROL_PLANE_API_VERSION = "m6-control-plane-api-v1"

ERROR_CODE_AUTH_FAILED = "AUTH_FAILED"
ERROR_CODE_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_CODE_NO_JOB = "NO_JOB"
ERROR_CODE_STALE_LEASE = "STALE_LEASE"
ERROR_CODE_CONFLICT = "CONFLICT"
ERROR_CODE_NOT_FOUND = "NOT_FOUND"
ERROR_CODE_SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
ERROR_CODE_INVARIANT_ERROR = "INVARIANT_ERROR"

_DEFAULT_AUTH_TOKEN_ENV = "PKP_CONTROL_PLANE_TOKEN"
_DEFAULT_CONNECT_RETRIES = 3
_DEFAULT_RECONNECT_BACKOFF_SECONDS = 1.0
_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0

#: Header carrying the per-logical-operation idempotency key.
HEADER_RPC_REQUEST_ID = "X-PKP-Rpc-Request-Id"
#: Header carrying the client's worker identity (for auditability).
HEADER_WORKER_ID = "X-PKP-Worker-Id"


class ApiError(TransportError):
    """Server-side structured error echoed to the client.

    ``code`` uses the frozen vocabulary above. ``http_status`` is the HTTP
    status the server would map to (informational for the client mapping).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


class AuthenticationError(TransportError):
    """Bearer token missing or rejected by the server."""

    def __init__(self, message: str = "authentication failed") -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# DTO helpers
# ---------------------------------------------------------------------------


def claimed_job_to_dict(claimed: ClaimedJob, *, include_token: bool) -> dict[str, Any]:
    """Serialize a ClaimedJob to a wire-safe dict.

    The lease token is included ONLY when ``include_token`` is True — i.e. the
    authenticated claim response to the owning worker. It is never included in
    any other projection (logs, observability, CLI).
    """
    out: dict[str, Any] = {
        "job_id": claimed.job_id,
        "stage": claimed.stage,
        "canonical_id": claimed.canonical_id,
        "platform": claimed.platform,
        "platform_content_id": claimed.platform_content_id,
        "input_fingerprint": claimed.input_fingerprint,
        "required_capabilities": sorted(claimed.required_capabilities),
        "lease_owner": claimed.lease_owner,
        "leased_at": claimed.leased_at,
        "lease_expires_at": claimed.lease_expires_at,
        "metadata": dict(claimed.metadata),
    }
    if include_token:
        out["lease_token"] = claimed._lease_token
    return out


def claimed_job_from_dict(data: dict[str, Any]) -> ClaimedJob:
    """Rehydrate a ClaimedJob from the claim response body."""
    required = {
        "job_id",
        "stage",
        "canonical_id",
        "platform",
        "platform_content_id",
        "input_fingerprint",
        "required_capabilities",
        "lease_owner",
        "leased_at",
        "lease_expires_at",
        "lease_token",
    }
    missing = required - set(data)
    if missing:
        raise ApiError(
            ERROR_CODE_VALIDATION_ERROR,
            f"claim response missing fields: {sorted(missing)}",
            http_status=502,
        )
    return ClaimedJob(
        job_id=data["job_id"],
        stage=data["stage"],
        canonical_id=data["canonical_id"],
        platform=data["platform"],
        platform_content_id=data["platform_content_id"],
        input_fingerprint=data["input_fingerprint"],
        required_capabilities=frozenset(data["required_capabilities"]),
        lease_owner=data["lease_owner"],
        leased_at=data["leased_at"],
        lease_expires_at=data["lease_expires_at"],
        _lease_token=data["lease_token"],
        metadata=dict(data.get("metadata") or {}),
    )


_SECRET_KEYS = frozenset(
    {
        "lease_token",
        "token",
        "cookie",
        "cookies",
        "sessionid",
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "access_key",
        "secret_key",
        "credentials",
        "auth",
    }
)


def redact_job_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Strip any secret-bearing keys from a job/worker payload for logging."""
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if any(secret in lowered for secret in _SECRET_KEYS):
            continue
        if isinstance(item, dict):
            out[key] = redact_job_payload(item)
        elif isinstance(item, list):
            out[key] = [redact_job_payload(i) if isinstance(i, dict) else i for i in item]
        else:
            out[key] = item
    return out


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class HttpWorkerTransport:
    """Remote worker transport speaking the control-plane HTTP API.

    Rules
    -----
    - One transport per logical worker; it is not thread-safe for concurrent
      claim cycles (the WorkerRuntime is a single-threaded loop).
    - Bearer token is read from ``auth_token_env`` at request time so env
      rotation applies without restart.
    - Every mutation carries ``X-PKP-Rpc-Request-Id`` (stable per logical
      operation across retries) so the server can dedupe committed-but-lost
      responses.
    - Only idempotency-protected requests are auto-retried (register,
      heartbeat, claim, start, renew, complete). Timeout/refused/temporary 5xx
      -> bounded exponential backoff, then ``RemoteUnavailableError``.
    - Structured server errors map to the transport exceptions WorkerRuntime
      understands.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth_token_env: str = _DEFAULT_AUTH_TOKEN_ENV,
        auth_token: Optional[str] = None,
        worker_id: Optional[str] = None,
        http_timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        connect_retries: int = _DEFAULT_CONNECT_RETRIES,
        reconnect_backoff_seconds: float = _DEFAULT_RECONNECT_BACKOFF_SECONDS,
        max_backoff_seconds: float = 60.0,
        clock=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token_env = auth_token_env
        self._auth_token_override = auth_token
        self.worker_id = worker_id
        self.http_timeout_seconds = float(http_timeout_seconds)
        self.connect_retries = max(1, int(connect_retries))
        self.reconnect_backoff_seconds = float(reconnect_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._clock = clock or time.monotonic

    # -- token --------------------------------------------------------------

    def _token(self) -> Optional[str]:
        if self._auth_token_override is not None:
            return self._auth_token_override
        return os.environ.get(self.auth_token_env)

    # -- low-level request --------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        rpc_request_id: Optional[str] = None,
        retryable: bool = True,
    ) -> Any:
        """Send one request with bounded retry. Returns parsed JSON body (or
        None for 204). Raises:
          - ApiError            for structured 4xx/5xx server errors
          - AuthenticationError for auth failures
          - StaleLeaseError     mapped from STALE_LEASE
          - RemoteUnavailableError for transient network/5xx after retries
        """
        token = self._token()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.worker_id:
            headers[HEADER_WORKER_ID] = self.worker_id
        if rpc_request_id:
            headers[HEADER_RPC_REQUEST_ID] = rpc_request_id

        payload = json.dumps(body).encode("utf-8") if body is not None else None

        last_error: Optional[BaseException] = None
        for attempt in range(self.connect_retries):
            req = urllib.request.Request(
                self.base_url + path,
                data=payload,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.http_timeout_seconds
                ) as resp:
                    if resp.status == 204:
                        return None
                    text = resp.read().decode("utf-8")
                    if not text.strip():
                        return None
                    data = json.loads(text)
                    if isinstance(data, dict) and "error" in data:
                        self._raise_api_error(data["error"])
                    return data
            except urllib.error.HTTPError as exc:
                last_error = exc
                try:
                    text = exc.read().decode("utf-8")
                    data = json.loads(text)
                except Exception:
                    data = None
                # Structured 5xx (e.g. SERVER_UNAVAILABLE) is transient: retry
                # when the request carries idempotency protection.
                if (
                    exc.code >= 500
                    and retryable
                    and attempt + 1 < self.connect_retries
                ):
                    self._sleep_backoff(attempt)
                    continue
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    self._raise_api_error(data["error"])
                # Unstructured 5xx: transient server unavailable.
                if exc.code == 401 or exc.code == 403:
                    raise AuthenticationError(
                        f"control plane rejected bearer token (HTTP {exc.code})"
                    )
                if exc.code >= 500:
                    raise RemoteUnavailableError(
                        f"control plane unavailable (HTTP {exc.code})"
                    )
                raise ApiError(
                    ERROR_CODE_SERVER_UNAVAILABLE,
                    f"unexpected HTTP {exc.code}",
                    http_status=exc.code,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if retryable and attempt + 1 < self.connect_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise RemoteUnavailableError(
                    f"control plane unreachable: {exc.__class__.__name__}"
                ) from exc

        raise RemoteUnavailableError(
            f"control plane unreachable after {self.connect_retries} attempts "
            f"(last: {last_error})"
        )

    def _raise_api_error(self, error: dict[str, Any]) -> None:
        code = error.get("code", ERROR_CODE_SERVER_UNAVAILABLE)
        message = error.get("message", "")
        if code == ERROR_CODE_STALE_LEASE:
            raise StaleLeaseError(message or "stale lease")
        if code == ERROR_CODE_AUTH_FAILED:
            raise AuthenticationError(message or "authentication failed")
        if code in {
            ERROR_CODE_VALIDATION_ERROR,
            ERROR_CODE_CONFLICT,
            ERROR_CODE_NOT_FOUND,
            ERROR_CODE_INVARIANT_ERROR,
        }:
            raise ApiError(code, message, http_status=400)
        raise RemoteUnavailableError(message or f"control plane error {code}")

    def _sleep_backoff(self, attempt: int) -> None:
        base = self.reconnect_backoff_seconds * (2 ** attempt)
        delay = min(base, self.max_backoff_seconds) + random.uniform(0, 0.25)
        time.sleep(delay)

    # -- transport operations -----------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        display_name: Optional[str] = None,
        hostname: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        allowed_stages: Optional[list[str]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": list(capabilities),
        }
        if display_name is not None:
            body["display_name"] = display_name
        if hostname is not None:
            body["hostname"] = hostname
        if metadata:
            body["metadata"] = metadata
        if allowed_stages is not None:
            body["allowed_stages"] = list(allowed_stages)
        return self._request(
            "POST",
            "/api/v1/workers/register",
            body=body,
            rpc_request_id=f"register:{worker_id}",
        )["worker"]

    def heartbeat_worker(
        self,
        worker_id: str,
        *,
        capabilities: Optional[list[str]] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"worker_id": worker_id}
        if capabilities is not None:
            body["capabilities"] = list(capabilities)
        if status is not None:
            body["status"] = status
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            "/api/v1/workers/heartbeat",
            body=body,
            rpc_request_id=f"heartbeat:{worker_id}",
        )["worker"]

    def claim_next_job(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        allowed_stages: Optional[list[str]] = None,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> Optional[ClaimedJob]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": list(capabilities),
            "lease_duration_seconds": lease_duration_seconds,
        }
        if allowed_stages is not None:
            body["allowed_stages"] = list(allowed_stages)
        resp = self._request(
            "POST",
            "/api/v1/jobs/claim",
            body=body,
            rpc_request_id=f"claim:{worker_id}:{time.monotonic_ns()}",
        )
        if resp is None or not resp.get("claimed"):
            return None
        return claimed_job_from_dict(resp["claimed"])

    def start_claimed_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "lease_token": lease_token,
        }
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/start",
            body=body,
            rpc_request_id=f"start:{job_id}:{worker_id}",
        )["job"]

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "lease_duration_seconds": lease_duration_seconds,
        }
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/renew",
            body=body,
            rpc_request_id=f"renew:{job_id}:{worker_id}",
        )["job"]

    def complete_job_success(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "outcome": "succeeded",
        }
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/complete",
            body=body,
            rpc_request_id=f"complete:{job_id}:{worker_id}:success",
        )["job"]

    def complete_job_retryable_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "outcome": "retryable_failure",
            "error_class": error_class,
            "error_message": error_message,
        }
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/complete",
            body=body,
            rpc_request_id=f"complete:{job_id}:{worker_id}:retryable",
        )["job"]

    def complete_job_terminal_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "outcome": "terminal_failure",
            "error_class": error_class,
            "error_message": error_message,
        }
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            f"/api/v1/jobs/{job_id}/complete",
            body=body,
            rpc_request_id=f"complete:{job_id}:{worker_id}:terminal",
        )["job"]

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            resp = self._request(
                "GET", f"/api/v1/jobs/{job_id}", retryable=False
            )
        except ApiError as exc:
            if exc.code == ERROR_CODE_NOT_FOUND:
                return None
            raise
        return resp.get("job")

    def get_job_result(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            resp = self._request(
                "GET", f"/api/v1/jobs/{job_id}/result", retryable=False
            )
        except ApiError as exc:
            if exc.code == ERROR_CODE_NOT_FOUND:
                return None
            raise
        return resp.get("result")

    def health_live(self) -> bool:
        try:
            resp = self._request("GET", "/health/live", retryable=False)
            return bool(resp and resp.get("status") == "ok")
        except Exception:
            return False

    def health_ready(self) -> dict[str, Any]:
        return self._request("GET", "/health/ready", retryable=False)

    def __repr__(self) -> str:
        return f"HttpWorkerTransport(base_url={self.base_url!r}, worker_id={self.worker_id!r})"


def _constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison (HMAC-safe, length-independent)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def constant_time_equal(a: str, b: str) -> bool:
    return _constant_time_equal(a, b)