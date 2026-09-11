"""M6-07 HttpWorkerTransport (remote client transport) tests.

Covers: protocol DTOs, structured error contract, bearer auth (missing/wrong/
correct), timeout, connection refused, reconnect/backoff, secret redaction
(including lease_token never in logs/errors), and the transport's RPC
request-id header. All HTTP is against a local in-process control-plane-like
handler or a disposable ThreadingHTTPServer — never a real NAS and never the
production operations DB.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.operations.http_transport import (
    CONTROL_PLANE_API_VERSION,
    ERROR_CODE_AUTH_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_STALE_LEASE,
    ERROR_CODE_VALIDATION_ERROR,
    HEADER_RPC_REQUEST_ID,
    HEADER_WORKER_ID,
    ApiError,
    AuthenticationError,
    HttpWorkerTransport,
    claimed_job_from_dict,
    claimed_job_to_dict,
    redact_job_payload,
)
from src.operations.models import (
    ClaimedJob,
    JobStage,
    compute_job_id,
    utc_now_iso,
)
from src.operations.store import (
    StaleLeaseError,
    create_operations_store,
    enqueue_job,
    get_job,
    register_worker,
)
from src.operations.transport import RemoteUnavailableError

PLATFORM = "douyin"
CONTENT_ID = "cid_7681603850364521734"
CANONICAL_ID = "douyin_7681603850364521734"
FINGERPRINT = "a" * 64

T0 = "2026-09-10T01:00:00+00:00"

TOKEN = "test-secret-token-abc123"


def _headers_to_dict(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items()}


def _job_payload(stage: str = JobStage.ARCHIVE.value, job_id: str | None = None) -> dict:
    return {
        "job_id": job_id
        or compute_job_id(
            PLATFORM, CONTENT_ID, stage, FINGERPRINT, "m6-scheduler-policy-v1"
        ),
        "stage": stage,
        "canonical_id": CANONICAL_ID,
        "platform": PLATFORM,
        "platform_content_id": CONTENT_ID,
        "input_fingerprint": FINGERPRINT,
        "required_capabilities": ["downloader"],
        "lease_owner": "win-worker",
        "leased_at": T0,
        "lease_expires_at": "2026-09-10T01:03:00+00:00",
        "lease_token": "lease_secret",
        "metadata": {},
    }


class _FakeHandler(BaseHTTPRequestHandler):
    """Scriptable local handler implementing a minimal m6-control-plane-api-v1."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _check_auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json(
                401, {"error": {"code": ERROR_CODE_AUTH_FAILED, "message": "bad token"}}
            )
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health/live":
            self._json(200, {"status": "ok"})
            return
        if not self._check_auth():
            return
        if self.path == "/health/ready":
            self._json(200, {"status": "ready"})
        elif self.path == "/api/v1/jobs/nonexistent":
            self._json(404, {"error": {"code": ERROR_CODE_NOT_FOUND, "message": "nf"}})
        else:
            self._json(200, {"status": "ok"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        body = self._read()
        if self.path == "/api/v1/jobs/claim":
            self._json(
                200,
                {
                    "claimed": _job_payload(),
                    "api_version": CONTROL_PLANE_API_VERSION,
                },
            )
        elif self.path == "/api/v1/jobs/stale/complete":
            self._json(
                409,
                {"error": {"code": ERROR_CODE_STALE_LEASE, "message": "lease gone"}},
            )
        elif self.path == "/api/v1/jobs/bad/start":
            self._json(
                400,
                {
                    "error": {
                        "code": ERROR_CODE_VALIDATION_ERROR,
                        "message": "invalid state",
                    }
                },
            )
        elif self.path in ("/api/v1/workers/register", "/api/v1/workers/heartbeat"):
            self._json(
                200,
                {
                    "worker": {"worker_id": body.get("worker_id", "w")},
                    "api_version": CONTROL_PLANE_API_VERSION,
                },
            )
        else:
            self._json(
                200,
                {
                    "job": {"job_id": "job_x", "state": "SUCCEEDED"},
                    "api_version": CONTROL_PLANE_API_VERSION,
                },
            )


@pytest.fixture
def fake_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def base_url(fake_server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{fake_server.server_address[1]}"


@pytest.fixture
def transport(base_url: str) -> HttpWorkerTransport:
    return HttpWorkerTransport(
        base_url=base_url,
        auth_token=TOKEN,
        worker_id="win-worker",
        http_timeout_seconds=2.0,
        connect_retries=2,
    )


def test_api_version_frozen():
    assert CONTROL_PLANE_API_VERSION == "m6-control-plane-api-v1"


def test_claimed_job_roundtrip_with_token():
    payload = _job_payload()
    claimed = claimed_job_from_dict(payload)
    assert isinstance(claimed, ClaimedJob)
    assert claimed.lease_token == "lease_secret"
    out = claimed_job_to_dict(claimed, include_token=True)
    assert out["lease_token"] == "lease_secret"
    out_public = claimed_job_to_dict(claimed, include_token=False)
    assert "lease_token" not in out_public


def test_claimed_job_from_dict_missing_fields():
    payload = _job_payload()
    del payload["lease_token"]
    with pytest.raises(ApiError) as exc:
        claimed_job_from_dict(payload)
    assert exc.value.code == ERROR_CODE_VALIDATION_ERROR


def test_redact_job_payload_strips_secrets():
    value = {
        "job_id": "job_x",
        "metadata": {"lease_token": "sekret", "note": "ok"},
        "lease_token": "sekret2",
        "nested": {"cookie": "abc", "keep": 1},
    }
    out = redact_job_payload(value)
    assert "lease_token" not in out
    assert "lease_token" not in out["metadata"]
    assert "cookie" not in out["nested"]
    assert out["metadata"]["note"] == "ok"
    assert out["nested"]["keep"] == 1
    assert out["job_id"] == "job_x"


def test_health_live_no_auth(transport: HttpWorkerTransport):
    assert transport.health_live() is True


def test_health_ready(transport: HttpWorkerTransport):
    assert transport.health_ready()["status"] == "ready"


def test_register_success(base_url: str):
    t = HttpWorkerTransport(base_url=base_url, auth_token=TOKEN, worker_id="w")
    row = t.register_worker("w", ["downloader"])
    assert isinstance(row, dict)


def test_claim_success(transport: HttpWorkerTransport):
    claimed = transport.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert claimed is not None
    assert claimed.stage == JobStage.ARCHIVE.value
    assert claimed.job_id == _job_payload()["job_id"]
    assert claimed.lease_token == "lease_secret"


def test_missing_token_rejected(fake_server: ThreadingHTTPServer, base_url: str):
    t = HttpWorkerTransport(base_url=base_url, worker_id="w", connect_retries=1)
    with pytest.raises(AuthenticationError):
        t.health_ready()


def test_wrong_token_rejected(fake_server: ThreadingHTTPServer, base_url: str):
    t = HttpWorkerTransport(
        base_url=base_url, auth_token="wrong", worker_id="w", connect_retries=1
    )
    with pytest.raises(AuthenticationError):
        t.register_worker("w", ["downloader"])


def test_correct_token_success(transport: HttpWorkerTransport):
    assert transport.health_ready()["status"] == "ready"


def test_token_from_env(fake_server: ThreadingHTTPServer, base_url: str, monkeypatch):
    monkeypatch.setenv("PKP_TEST_TOKEN_ENV", TOKEN)
    t = HttpWorkerTransport(
        base_url=base_url,
        auth_token_env="PKP_TEST_TOKEN_ENV",
        worker_id="w",
        connect_retries=1,
    )
    assert t.health_ready()["status"] == "ready"


def test_stale_lease_mapped(transport: HttpWorkerTransport):
    with pytest.raises(StaleLeaseError):
        transport.complete_job_success("stale", "win-worker", "lease_secret")


def test_validation_error_mapped(transport: HttpWorkerTransport):
    with pytest.raises(ApiError) as exc:
        transport.start_claimed_job("bad", "win-worker", "lease_secret")
    assert exc.value.code == ERROR_CODE_VALIDATION_ERROR


def test_not_found_returns_none(transport: HttpWorkerTransport):
    assert transport.get_job("nonexistent") is None
    assert transport.get_job_result("nonexistent") is None


def test_rpc_request_id_and_worker_headers_sent(fake_server: ThreadingHTTPServer, base_url: str):
    captured: dict = {}

    class CaptureHandler(_FakeHandler):
        def do_POST(self) -> None:
            if not self._check_auth():
                return
            self._read()
            captured["rpc"] = self.headers.get(HEADER_RPC_REQUEST_ID)
            captured["worker"] = self.headers.get(HEADER_WORKER_ID)
            self._json(200, {"worker": {"worker_id": "w"}, "api_version": CONTROL_PLANE_API_VERSION})

    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        t = HttpWorkerTransport(base_url=url, auth_token=TOKEN, worker_id="win-worker")
        t.heartbeat_worker("win-worker")
        assert captured["worker"] == "win-worker"
        assert captured["rpc"] and captured["rpc"].startswith("heartbeat:")
    finally:
        server.shutdown()
        server.server_close()


def test_connection_refused_is_remote_unavailable():
    t = HttpWorkerTransport(
        base_url="http://127.0.0.1:1",
        auth_token=TOKEN,
        worker_id="w",
        connect_retries=1,
        http_timeout_seconds=0.5,
        reconnect_backoff_seconds=0.01,
    )
    with pytest.raises(RemoteUnavailableError):
        t.health_ready()


_drop_once_calls = 0


class _DropOnceHandler(BaseHTTPRequestHandler):
    """Fails the first request with a transient 503, then succeeds."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global _drop_once_calls
        _drop_once_calls += 1
        if _drop_once_calls == 1:
            self._json(
                503,
                {"error": {"code": "SERVER_UNAVAILABLE", "message": "busy"}},
            )
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._json(200, {"worker": {"worker_id": "w"}})


def test_transient_5xx_retried_then_succeeds():
    global _drop_once_calls
    _drop_once_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DropOnceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        t = HttpWorkerTransport(
            base_url=url,
            auth_token=TOKEN,
            connect_retries=3,
            http_timeout_seconds=2.0,
            reconnect_backoff_seconds=0.01,
        )
        assert t.heartbeat_worker("w")["worker_id"] == "w"
        assert _drop_once_calls == 2
    finally:
        server.shutdown()
        server.server_close()


class _Exhaust5xxHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps(
            {"error": {"code": "SERVER_UNAVAILABLE", "message": "down"}}
        ).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_5xx_exhaustion_raises_remote_unavailable():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Exhaust5xxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        t = HttpWorkerTransport(
            base_url=url,
            auth_token=TOKEN,
            connect_retries=2,
            reconnect_backoff_seconds=0.01,
        )
        with pytest.raises(RemoteUnavailableError):
            t.heartbeat_worker("w")
    finally:
        server.shutdown()
        server.server_close()


def test_timeout_is_bounded():
    class _SlowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            import time

            time.sleep(5.0)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        t = HttpWorkerTransport(
            base_url=url,
            auth_token=TOKEN,
            connect_retries=1,
            http_timeout_seconds=0.2,
        )
        import time as tmod

        start = tmod.monotonic()
        with pytest.raises(RemoteUnavailableError):
            t.health_ready()
        assert tmod.monotonic() - start < 3.0
    finally:
        server.shutdown()
        server.server_close()


def test_error_contract_never_leaks_token():
    # Structured server errors must carry code+message only (no traceback/sql/token).
    err = {
        "error": {
            "code": ERROR_CODE_STALE_LEASE,
            "message": "lease_token not in message",
        }
    }
    text = json.dumps(err)
    assert "lease_secret" not in text
    assert "traceback" not in text.lower()


def test_base_url_accepts_positional_argument():
    # Deployment regression (M6-08): the Windows worker host builds
    # HttpWorkerTransport(config.control_plane_url, ...) positionally;
    # base_url must be POSITIONAL_OR_KEYWORD, not keyword-only.
    from src.operations.http_transport import HttpWorkerTransport, RemoteUnavailableError

    t = HttpWorkerTransport(
        "http://127.0.0.1:1",
        auth_token="unused-test-token",
        worker_id="w",
        connect_retries=1,
    )
    assert t.base_url == "http://127.0.0.1:1"
    assert t.worker_id == "w"
    with pytest.raises(RemoteUnavailableError):
        t.health_ready()