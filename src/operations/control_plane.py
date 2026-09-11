"""M6-07 NAS control plane: service lifecycle, scheduler, NAS local worker,
and the versioned HTTP API.

Topology
--------
The NAS Control Plane owns the Operations SQLite store, the Scheduler,
startup recovery, retry/reconciliation, observability, the M5 Knowledge Store,
and NAS-local execution stages (KNOWLEDGE_FINALIZE, STORE_INGEST).

Windows workers never open the Operations SQLite or M5 SQLite directly — they
only mutate operations state through the HTTP API (register / heartbeat /
claim / start / renew / complete). The server is the only authority for job
state transitions.

Ownership (frozen)
------------------
NAS Control Plane owns:
  - validate_operations_store / startup_recovery / recover_expired_leases
  - retry requeue / Scheduler.run_once/run_forever
  - Operations SQLite transactions / pipeline state / worker-job-attempt-event
    records
Windows execution worker owns:
  - executing stage handlers (filesystem/model/browser side effects)
  - heartbeat requests / lease renewal requests / completion reports

Stage placement (explicit policy, never a race)
-----------------------------------------------
  WINDOWS: DISCOVER, ARCHIVE, MEDIA_PROCESS, KNOWLEDGE_EXTRACT
  NAS    : KNOWLEDGE_FINALIZE, STORE_INGEST

Placement is enforced server-side at claim time: a worker may only claim
stages in its registered ``allowed_stages`` (persisted at register). The M6-02
all-of capability rule is unchanged; ``allowed_stages`` is purely an additive
execution-placement filter.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from .admin import startup_recovery
from .http_transport import (
    CONTROL_PLANE_API_VERSION,
    ERROR_CODE_AUTH_FAILED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVARIANT_ERROR,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_STALE_LEASE,
    ERROR_CODE_VALIDATION_ERROR,
    claimed_job_to_dict,
    redact_job_payload,
)
from .models import (
    DEFAULT_OPERATIONS_PATH,
    JobStage,
    normalize_capabilities,
    normalize_identity_component,
    utc_now_iso,
)
from .observability import compute_operations_summary
from .scheduler import Scheduler
from .stages import (
    KnowledgeFinalizeAdapter,
    StoreIngestAdapter,
)
from .store import (
    StaleLeaseError,
    claim_next_job,
    complete_job_retryable_failure,
    complete_job_success,
    complete_job_terminal_failure,
    create_operations_store,
    find_active_claim,
    get_job,
    get_job_result,
    get_worker,
    open_operations_store,
    register_worker,
    renew_job_lease,
    replay_completed_job,
    replay_started_job,
    start_claimed_job,
    validate_operations_store,
    worker_heartbeat,
)
from .transport import LocalSQLiteWorkerTransport
from .worker import WorkerRuntime

__all__ = [
    "CONTROL_PLANE_CONFIG_VERSION",
    "CONTROL_PLANE_POLICY_VERSION",
    "WINDOWS_STAGE_ALLOWLIST",
    "NAS_STAGE_ALLOWLIST",
    "DEFAULT_AUTH_TOKEN_ENV",
    "ControlPlaneConfig",
    "ControlPlaneService",
    "run_control_plane",
    "build_nas_local_handlers",
]

CONTROL_PLANE_CONFIG_VERSION = "m6-control-plane-config-v1"
CONTROL_PLANE_POLICY_VERSION = "m6-control-plane-policy-v1"

#: Execution placement allowlists (frozen policy; server-authoritative).
WINDOWS_STAGE_ALLOWLIST: tuple[str, ...] = (
    JobStage.DISCOVER.value,
    JobStage.ARCHIVE.value,
    JobStage.MEDIA_PROCESS.value,
    JobStage.KNOWLEDGE_EXTRACT.value,
)
NAS_STAGE_ALLOWLIST: tuple[str, ...] = (
    JobStage.KNOWLEDGE_FINALIZE.value,
    JobStage.STORE_INGEST.value,
)

DEFAULT_AUTH_TOKEN_ENV = "PKP_CONTROL_PLANE_TOKEN"
DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

#: RPC idempotency records older than this are garbage-collected (bounded
#: growth — records are keyed by rpc_request_id and expire after TTL).
RPC_IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlPlaneConfig:
    """Control-plane configuration (``m6-control-plane-config-v1``).

    Secret policy: no tokens, credentials, cookies, API keys in the config.
    The bearer token is read from ``auth_token_env`` at request time.
    """

    operations_db_path: str = DEFAULT_OPERATIONS_PATH
    knowledge_store_path: str = "data/knowledge/knowledge_store.sqlite3"
    archive_root: str = "data/raw_archive"
    processed_root: str = "data/processed"
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    scheduler_poll_interval_seconds: float = DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS
    worker_stale_threshold_seconds: int = 120
    local_worker_enabled: bool = True
    local_worker_id: str = "nas-local-worker"
    local_worker_capabilities: tuple[str, ...] = ("store_ingest",)
    local_worker_allowed_stages: tuple[str, ...] = NAS_STAGE_ALLOWLIST
    auth_token_env: str = DEFAULT_AUTH_TOKEN_ENV
    log_path: str = "logs/operations/control-plane.log"
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5
    production_mode: bool = True
    schema_version: str = CONTROL_PLANE_CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operations_db_path": self.operations_db_path,
            "knowledge_store_path": self.knowledge_store_path,
            "archive_root": self.archive_root,
            "processed_root": self.processed_root,
            "host": self.host,
            "port": self.port,
            "scheduler_poll_interval_seconds": self.scheduler_poll_interval_seconds,
            "worker_stale_threshold_seconds": self.worker_stale_threshold_seconds,
            "local_worker_enabled": self.local_worker_enabled,
            "local_worker_id": self.local_worker_id,
            "local_worker_capabilities": list(self.local_worker_capabilities),
            "local_worker_allowed_stages": list(self.local_worker_allowed_stages),
            "auth_token_env": self.auth_token_env,
            "log_path": self.log_path,
            "log_max_bytes": self.log_max_bytes,
            "log_backup_count": self.log_backup_count,
            "production_mode": self.production_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlPlaneConfig":
        if not isinstance(data, dict):
            raise ValueError("control plane config must be a JSON object")
        schema = data.get("schema_version")
        if schema not in (None, CONTROL_PLANE_CONFIG_VERSION):
            raise ValueError(
                f"unsupported control plane config schema {schema!r} "
                f"(expected {CONTROL_PLANE_CONFIG_VERSION!r})"
            )
        ops_db = str(data.get("operations_db_path", DEFAULT_OPERATIONS_PATH))
        kstore = str(
            data.get("knowledge_store_path", "data/knowledge/knowledge_store.sqlite3")
        )
        for path, name in ((ops_db, "operations_db_path"), (kstore, "knowledge_store_path")):
            if _is_remote_ops_path(path):
                raise ValueError(
                    f"{name} must be a local filesystem path; remote/SMB paths "
                    f"are forbidden (got {path!r})"
                )
        return cls(
            operations_db_path=str(
                data.get("operations_db_path", DEFAULT_OPERATIONS_PATH)
            ),
            knowledge_store_path=str(
                data.get("knowledge_store_path", "data/knowledge/knowledge_store.sqlite3")
            ),
            archive_root=str(data.get("archive_root", "data/raw_archive")),
            processed_root=str(data.get("processed_root", "data/processed")),
            host=str(data.get("host", DEFAULT_HOST)),
            port=int(data.get("port", DEFAULT_PORT)),
            scheduler_poll_interval_seconds=float(
                data.get(
                    "scheduler_poll_interval_seconds",
                    DEFAULT_SCHEDULER_POLL_INTERVAL_SECONDS,
                )
            ),
            worker_stale_threshold_seconds=int(
                data.get("worker_stale_threshold_seconds", 120)
            ),
            local_worker_enabled=bool(data.get("local_worker_enabled", True)),
            local_worker_id=str(data.get("local_worker_id", "nas-local-worker")),
            local_worker_capabilities=tuple(
                normalize_capabilities(
                    data.get(
                        "local_worker_capabilities",
                        ["store_ingest"],
                    )
                )
            ),
            local_worker_allowed_stages=tuple(
                normalize_identity_component(s)
                for s in data.get(
                    "local_worker_allowed_stages",
                    list(NAS_STAGE_ALLOWLIST),
                )
            ),
            auth_token_env=str(
                data.get("auth_token_env", DEFAULT_AUTH_TOKEN_ENV)
            ),
            log_path=str(data.get("log_path", "logs/operations/control-plane.log")),
            log_max_bytes=int(data.get("log_max_bytes", 10_000_000)),
            log_backup_count=int(data.get("log_backup_count", 5)),
            production_mode=bool(data.get("production_mode", True)),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ControlPlaneConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


def _is_remote_ops_path(path: str) -> bool:
    """True for UNC (\\\\host\\...) or URL (scheme://...) paths — both are
    forbidden for the Operations/M5 SQLite files in a production topology."""
    if path.startswith("\\\\"):
        return True
    return "://" in path


def _validate_local_ops_path(path: str, *, name: str) -> list[str]:
    if _is_remote_ops_path(path):
        return [
            f"{name} must be a local filesystem path; remote/SMB paths are "
            f"forbidden (got {path!r})"
        ]
    return []


# ---------------------------------------------------------------------------
# NAS-local handlers
# ---------------------------------------------------------------------------


def build_nas_local_handlers(
    *,
    processed_root: Optional[str | Path] = None,
    knowledge_store_path: Optional[str | Path] = None,
    render_config=None,
    policy_version: str = CONTROL_PLANE_POLICY_VERSION,
) -> dict[str, Callable[[Any], Any]]:
    """Build handler mapping for NAS-local stages (KNOWLEDGE_FINALIZE +
    STORE_INGEST). Tests inject fakes via ``adapters`` instead.

    Returns a dict keyed by stage string -> handler callable. Lazy imports keep
    the control plane free of heavy M2-M5 dependencies at import time.
    """
    handlers: dict[str, Callable[[Any], Any]] = {}
    finalize = KnowledgeFinalizeAdapter(
        processed_root=Path(processed_root) if processed_root else None,
        render_config=render_config,
        policy_version=policy_version,
    )
    ingest = StoreIngestAdapter(
        knowledge_store_path=(
            Path(knowledge_store_path) if knowledge_store_path else None
        ),
        processed_root=Path(processed_root) if processed_root else None,
        policy_version=policy_version,
    )
    handlers[JobStage.KNOWLEDGE_FINALIZE.value] = finalize.to_handler()
    handlers[JobStage.STORE_INGEST.value] = ingest.to_handler()
    return handlers


# ---------------------------------------------------------------------------
# RPC idempotency (additive, owned by the control plane)
# ---------------------------------------------------------------------------


def _ensure_rpc_idempotency_table(db_path: Path) -> None:
    conn = open_operations_store(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rpc_idempotency (
                rpc_request_id TEXT PRIMARY KEY,
                request_type TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _rpc_idempotency_lookup(db_path: Path, rpc_request_id: str) -> Optional[dict[str, Any]]:
    conn = open_operations_store(db_path)
    try:
        row = conn.execute(
            "SELECT response_json FROM rpc_idempotency WHERE rpc_request_id=?",
            (rpc_request_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["response_json"])
    finally:
        conn.close()


def _rpc_idempotency_store(
    db_path: Path,
    rpc_request_id: str,
    request_type: str,
    response: dict[str, Any],
    *,
    now: str,
) -> None:
    conn = open_operations_store(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cutoff = _subtract_seconds(now, RPC_IDEMPOTENCY_TTL_SECONDS)
            conn.execute(
                "DELETE FROM rpc_idempotency WHERE created_at < ?", (cutoff,)
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO rpc_idempotency(
                    rpc_request_id, request_type, response_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (rpc_request_id, request_type, json.dumps(response), now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _subtract_seconds(iso: str, seconds: int) -> str:
    from .models import format_iso, parse_iso
    from datetime import timedelta

    return format_iso(parse_iso(iso) - timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ControlPlaneService:
    """Lifecycle owner of the NAS control plane.

    Startup sequence (frozen): load config -> configure logging -> open/create
    local Ops DB -> validate_operations_store -> verify M5 store path local ->
    startup_recovery -> create scheduler -> create NAS local worker -> start
    scheduler loop -> start local worker loop -> expose HTTP ready.

    ``ready`` is only True after recovery/initialization has run.
    """

    def __init__(
        self,
        config: ControlPlaneConfig,
        *,
        token: Optional[str] = None,
        clock: Optional[Callable[[], str]] = None,
        scheduler: Optional[Scheduler] = None,
        local_worker: Optional[WorkerRuntime] = None,
        local_handlers: Optional[dict[str, Callable[[Any], Any]]] = None,
        server_factory: Optional[Callable[..., ThreadingHTTPServer]] = None,
        handler_class: Optional[type] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.operations_db_path = Path(config.operations_db_path)
        self._token_override = token
        self._clock = clock or utc_now_iso
        self._logger = logger or logging.getLogger("m6.control_plane")
        self._scheduler = scheduler
        self._local_worker = local_worker
        self._local_handlers = local_handlers
        self._server_factory = server_factory
        self._handler_class = handler_class

        self._stop_event = threading.Event()
        self._server: Optional[ThreadingHTTPServer] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._local_worker_thread: Optional[threading.Thread] = None
        self._ready = False
        self._ready_errors: list[str] = []

    # -- auth ---------------------------------------------------------------

    def _token(self) -> Optional[str]:
        if self._token_override is not None:
            return self._token_override
        return os.environ.get(self.config.auth_token_env)

    def check_auth(self, authorization: Optional[str]) -> bool:
        token = self._token()
        if token is None:
            return False
        if not authorization or not authorization.startswith("Bearer "):
            return False
        provided = authorization[len("Bearer "):].strip()
        return hmac.compare_digest(provided, token)

    # -- readiness ----------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready

    def ready_errors(self) -> list[str]:
        return list(self._ready_errors)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "ControlPlaneService":
        """Run the frozen startup sequence and bring the HTTP API up."""
        now = self._clock()
        errors: list[str] = []
        errors += _validate_local_ops_path(
            str(self.operations_db_path), name="operations_db_path"
        )
        errors += _validate_local_ops_path(
            str(self.config.knowledge_store_path), name="knowledge_store_path"
        )
        if errors:
            self._ready_errors = errors
            self._logger.error("control plane startup failed: %s", "; ".join(errors))
            return self

        create_operations_store(self.operations_db_path)
        validation = validate_operations_store(self.operations_db_path, now=now)
        if not validation.valid:
            self._ready_errors = [
                f"operations store invalid: {v}" for v in validation.violations[:5]
            ]
            self._logger.error(
                "operations store invalid: %s", "; ".join(validation.violations[:5])
            )
            return self

        _ensure_rpc_idempotency_table(self.operations_db_path)

        if self._scheduler is None:
            self._scheduler = Scheduler(
                self.operations_db_path,
                policy_version="m6-scheduler-policy-v1",
                now=self._clock,
            )
        startup_recovery(
            self.operations_db_path,
            now=self._clock(),
            scheduler=self._scheduler,
        )

        if self.config.local_worker_enabled and self._local_worker is None:
            if self._local_handlers is None:
                self._local_handlers = build_nas_local_handlers(
                    processed_root=self.config.processed_root,
                    knowledge_store_path=self.config.knowledge_store_path,
                )
            transport = LocalSQLiteWorkerTransport(self.operations_db_path)
            self._local_worker = WorkerRuntime(
                transport=transport,
                worker_id=self.config.local_worker_id,
                display_name="NAS local worker",
                hostname=socket.gethostname(),
                capabilities=list(self.config.local_worker_capabilities),
                handlers=self._local_handlers,
                allowed_stages=list(self.config.local_worker_allowed_stages),
                now=self._clock,
            )
            try:
                self._local_worker.register()
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("NAS local worker register failed: %s", exc)

        self._ready = True
        self._start_loops()
        self._start_http_server()
        self._logger.info(
            "control plane ready on %s:%s (api=%s)",
            self.config.host,
            self.config.port,
            CONTROL_PLANE_API_VERSION,
        )
        return self

    def _start_loops(self) -> None:
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="control-plane-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        if self._local_worker is not None:
            self._local_worker_thread = threading.Thread(
                target=self._local_worker_loop,
                name="control-plane-local-worker",
                daemon=True,
            )
            self._local_worker_thread.start()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scheduler.run_once(now=self._clock())
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.error("scheduler cycle failed: %s", exc)
            self._stop_event.wait(self.config.scheduler_poll_interval_seconds)

    def _local_worker_loop(self) -> None:
        if self._local_worker is None:
            return
        while not self._stop_event.is_set():
            try:
                self._local_worker.run_once()
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.error("NAS local worker cycle failed: %s", exc)
            self._stop_event.wait(0.5)

    def _start_http_server(self) -> None:
        handler_cls = self._handler_class or ControlPlaneHttpHandler
        factory = self._server_factory or ThreadingHTTPServer

        class ServiceHandler(handler_cls):  # type: ignore[valid-type, misc]
            service = self  # type: ignore[assignment]

        self._server = factory(
            (self.config.host, self.config.port), ServiceHandler
        )
        thread = threading.Thread(
            target=self._server.serve_forever,
            name="control-plane-http",
            daemon=True,
        )
        thread.start()
        self.server_address = self._server.server_address
        self.http_url = f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        """Graceful shutdown: stop loops, stop the HTTP server."""
        self._stop_event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for thread in (
            self._scheduler_thread,
            self._local_worker_thread,
        ):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
        self._scheduler_thread = None
        self._local_worker_thread = None
        self._ready = False
        self._logger.info("control plane stopped")

    def __enter__(self) -> "ControlPlaneService":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class ControlPlaneHttpHandler(BaseHTTPRequestHandler):
    """Versioned ``m6-control-plane-api-v1`` handler.

    Route table
    -----------
    GET  /health/live                 (no auth; process alive)
    GET  /health/ready                (auth; DB+schema+recovery ready)
    POST /api/v1/workers/register
    POST /api/v1/workers/heartbeat
    POST /api/v1/jobs/claim
    POST /api/v1/jobs/{id}/start
    POST /api/v1/jobs/{id}/renew
    POST /api/v1/jobs/{id}/complete
    GET  /api/v1/jobs/{id}
    GET  /api/v1/jobs/{id}/result
    GET  /api/v1/status

    Every response body is JSON. Errors use the frozen contract
    ``{"error": {"code", "message"}}``. Python tracebacks, SQL, and tokens are
    never returned.
    """

    protocol_version = "HTTP/1.1"
    service: "ControlPlaneService"

    # -- logging ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep HTTP access logs minimal and secret-free.
        self.service._logger.debug("http: " + fmt, *args)

    # -- helpers ------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: str, message: str, http_status: int = 400) -> None:
        self._send_json(http_status, {"error": {"code": code, "message": message}})

    def _read_body(self) -> Optional[dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR, "invalid JSON body", http_status=400
            )
            return None
        if not isinstance(data, dict):
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR,
                "request body must be a JSON object",
                http_status=400,
            )
            return None
        return data

    def _path_parts(self) -> list[str]:
        parsed = urllib.parse.urlparse(self.path)
        return [p for p in parsed.path.split("/") if p]

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:
        parts = self._path_parts()
        if parts == ["health", "live"]:
            self._send_json(200, {"status": "ok"})
            return
        if not self._require_auth():
            return
        if parts == ["health", "ready"]:
            self._handle_health_ready()
        elif parts == ["api", "v1", "status"]:
            self._handle_status()
        elif len(parts) == 4 and parts[:2] == ["api", "v1"] and parts[2] == "jobs":
            self._handle_get_job(parts[3])
        elif (
            len(parts) == 5
            and parts[:2] == ["api", "v1"]
            and parts[2] == "jobs"
            and parts[4] == "result"
        ):
            self._handle_get_job_result(parts[3])
        else:
            self._send_error(ERROR_CODE_NOT_FOUND, "not found", http_status=404)

    def do_POST(self) -> None:
        parts = self._path_parts()
        if not self._require_auth():
            return
        if parts == ["api", "v1", "workers", "register"]:
            self._handle_register()
        elif parts == ["api", "v1", "workers", "heartbeat"]:
            self._handle_heartbeat()
        elif parts == ["api", "v1", "jobs", "claim"]:
            self._handle_claim()
        elif len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"]:
            self._handle_job_action(parts[3], parts[4])
        else:
            self._send_error(ERROR_CODE_NOT_FOUND, "not found", http_status=404)

    def _require_auth(self) -> bool:
        if not self.service.check_auth(self.headers.get("Authorization")):
            self._send_error(
                ERROR_CODE_AUTH_FAILED,
                "missing or invalid bearer token",
                http_status=401,
            )
            return False
        return True

    # -- handlers -----------------------------------------------------------

    def _handle_health_ready(self) -> None:
        payload = {
            "status": "ready" if self.service.ready else "not_ready",
            "api_version": CONTROL_PLANE_API_VERSION,
            "operations_store_valid": True,
            "scheduler_initialized": self.service._scheduler is not None,
            "local_worker_initialized": (
                self.service.config.local_worker_enabled
                and self.service._local_worker is not None
            ),
            "errors": self.service.ready_errors(),
        }
        status = 200 if self.service.ready else 503
        self._send_json(status, payload)

    def _handle_register(self) -> None:
        body = self._read_body()
        if body is None:
            return
        worker_id = normalize_identity_component(str(body.get("worker_id", "")))
        if not worker_id:
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR, "worker_id is required"
            )
            return
        caps = normalize_capabilities(body.get("capabilities") or [])
        if not caps:
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR, "at least one capability is required"
            )
            return
        allowed_stages = body.get("allowed_stages")
        if allowed_stages is not None:
            allowed_stages = [
                normalize_identity_component(s) for s in allowed_stages
            ]
        try:
            row = register_worker(
                self.service.operations_db_path,
                worker_id,
                caps,
                display_name=body.get("display_name"),
                hostname=body.get("hostname"),
                metadata={"allowed_stages": allowed_stages},
                now=self.service._clock(),
            )
        except Exception as exc:
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR, str(exc), http_status=400
            )
            return
        self._send_json(
            200, {"worker": redact_job_payload(row), "api_version": CONTROL_PLANE_API_VERSION}
        )

    def _handle_heartbeat(self) -> None:
        body = self._read_body()
        if body is None:
            return
        worker_id = normalize_identity_component(str(body.get("worker_id", "")))
        if not worker_id:
            self._send_error(ERROR_CODE_VALIDATION_ERROR, "worker_id is required")
            return
        try:
            row = worker_heartbeat(
                self.service.operations_db_path,
                worker_id,
                capabilities=body.get("capabilities"),
                status=body.get("status"),
                metadata=body.get("metadata"),
                now=self.service._clock(),
            )
        except Exception as exc:
            self._send_error(ERROR_CODE_VALIDATION_ERROR, str(exc), http_status=400)
            return
        self._send_json(
            200, {"worker": redact_job_payload(row), "api_version": CONTROL_PLANE_API_VERSION}
        )

    def _handle_claim(self) -> None:
        body = self._read_body()
        if body is None:
            return
        worker_id = normalize_identity_component(str(body.get("worker_id", "")))
        caps = normalize_capabilities(body.get("capabilities") or [])
        lease_duration = int(body.get("lease_duration_seconds", 120))
        if not worker_id or not caps:
            self._send_error(
                ERROR_CODE_VALIDATION_ERROR,
                "worker_id and capabilities are required for claim",
            )
            return
        # Server-authoritative placement (M6-07 §27): the claim filter comes
        # from the worker's registered allowed_stages, never the request body.
        worker = get_worker(self.service.operations_db_path, worker_id)
        registered_stages = None
        if worker is not None:
            worker_meta = worker.get("metadata_json") or "{}"
            try:
                registered_stages = json.loads(worker_meta).get("allowed_stages")
            except (TypeError, ValueError):
                registered_stages = None
        requested = body.get("allowed_stages")
        if requested is not None:
            requested = [normalize_identity_component(s) for s in requested]
            if registered_stages is None:
                registered_stages = requested
        try:
            active = find_active_claim(
                self.service.operations_db_path,
                worker_id,
                now=self.service._clock(),
            )
            if active is not None:
                self._send_json(
                    200,
                    {
                        "claimed": claimed_job_to_dict(active, include_token=True),
                        "api_version": CONTROL_PLANE_API_VERSION,
                        "idempotent_replay": True,
                    },
                )
                return
            claimed = claim_next_job(
                self.service.operations_db_path,
                worker_id,
                caps,
                allowed_stages=registered_stages,
                lease_duration_seconds=lease_duration,
                now=self.service._clock(),
            )
        except Exception as exc:
            self._send_error(ERROR_CODE_VALIDATION_ERROR, str(exc), http_status=400)
            return
        if claimed is None:
            self._send_json(200, {"claimed": None})
            return
        self._send_json(
            200,
            {
                "claimed": claimed_job_to_dict(claimed, include_token=True),
                "api_version": CONTROL_PLANE_API_VERSION,
            },
        )

    def _handle_job_action(self, job_id: str, action: str) -> None:
        body = self._read_body()
        if body is None:
            return
        job_id = normalize_identity_component(job_id)
        if not job_id:
            self._send_error(ERROR_CODE_VALIDATION_ERROR, "job_id is required")
            return
        worker_id = normalize_identity_component(str(body.get("worker_id", "")))
        lease_token = str(body.get("lease_token", ""))
        try:
            if action == "start":
                replayed = replay_started_job(
                    self.service.operations_db_path,
                    job_id,
                    worker_id,
                    lease_token,
                )
                if replayed is not None:
                    self._send_json(
                        200,
                        {
                            "job": redact_job_payload(replayed),
                            "api_version": CONTROL_PLANE_API_VERSION,
                            "idempotent_replay": True,
                        },
                    )
                    return
                job = start_claimed_job(
                    self.service.operations_db_path,
                    job_id,
                    worker_id,
                    lease_token,
                    now=self.service._clock(),
                    metadata=body.get("metadata"),
                )
            elif action == "renew":
                job = renew_job_lease(
                    self.service.operations_db_path,
                    job_id,
                    worker_id,
                    lease_token,
                    lease_duration_seconds=int(
                        body.get("lease_duration_seconds", 120)
                    ),
                    now=self.service._clock(),
                )
            elif action == "complete":
                outcome = body.get("outcome")
                if outcome not in (
                    "succeeded",
                    "retryable_failure",
                    "terminal_failure",
                ):
                    self._send_error(
                        ERROR_CODE_VALIDATION_ERROR,
                        f"unknown completion outcome {outcome!r}",
                    )
                    return
                replayed = replay_completed_job(
                    self.service.operations_db_path,
                    job_id,
                    worker_id,
                    outcome,
                )
                if replayed is not None:
                    self._send_json(
                        200,
                        {
                            "job": redact_job_payload(replayed),
                            "api_version": CONTROL_PLANE_API_VERSION,
                            "idempotent_replay": True,
                        },
                    )
                    return
                if outcome == "succeeded":
                    job = complete_job_success(
                        self.service.operations_db_path,
                        job_id,
                        worker_id,
                        lease_token,
                        now=self.service._clock(),
                        metadata=body.get("metadata"),
                    )
                elif outcome == "retryable_failure":
                    job = complete_job_retryable_failure(
                        self.service.operations_db_path,
                        job_id,
                        worker_id,
                        lease_token,
                        error_class=str(body.get("error_class", "RemoteError")),
                        error_message=str(body.get("error_message", "")),
                        now=self.service._clock(),
                        metadata=body.get("metadata"),
                    )
                else:
                    job = complete_job_terminal_failure(
                        self.service.operations_db_path,
                        job_id,
                        worker_id,
                        lease_token,
                        error_class=str(body.get("error_class", "RemoteError")),
                        error_message=str(body.get("error_message", "")),
                        now=self.service._clock(),
                        metadata=body.get("metadata"),
                    )
            else:
                self._send_error(ERROR_CODE_NOT_FOUND, "not found", http_status=404)
                return
        except StaleLeaseError as exc:
            self._send_error(
                ERROR_CODE_STALE_LEASE, str(exc), http_status=409
            )
            return
        except Exception as exc:
            self._send_error(ERROR_CODE_VALIDATION_ERROR, str(exc), http_status=400)
            return
        self._send_json(
            200, {"job": redact_job_payload(job), "api_version": CONTROL_PLANE_API_VERSION}
        )

    def _handle_get_job(self, job_id: str) -> None:
        job_id = normalize_identity_component(job_id)
        job = get_job(self.service.operations_db_path, job_id)
        if job is None:
            self._send_error(ERROR_CODE_NOT_FOUND, "job not found", http_status=404)
            return
        self._send_json(
            200, {"job": redact_job_payload(job), "api_version": CONTROL_PLANE_API_VERSION}
        )

    def _handle_get_job_result(self, job_id: str) -> None:
        job_id = normalize_identity_component(job_id)
        result = get_job_result(self.service.operations_db_path, job_id)
        if result is None:
            self._send_error(ERROR_CODE_NOT_FOUND, "job result not found", http_status=404)
            return
        self._send_json(
            200,
            {"result": redact_job_payload(result), "api_version": CONTROL_PLANE_API_VERSION},
        )

    def _handle_status(self) -> None:
        summary = compute_operations_summary(
            self.service.operations_db_path,
            now=self.service._clock(),
            stale_threshold_seconds=self.service.config.worker_stale_threshold_seconds,
        )
        self._send_json(
            200,
            {
                "status": summary.to_dict(),
                "api_version": CONTROL_PLANE_API_VERSION,
            },
        )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control-plane",
        description="M6-07 NAS control plane",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="path to m6-control-plane-config-v1 JSON",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="override listen host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="override listen port",
    )
    return parser


def run_control_plane(
    config: ControlPlaneConfig,
    *,
    token: Optional[str] = None,
    clock: Optional[Callable[[], str]] = None,
) -> int:
    """Run the control plane in the foreground until SIGINT/SIGTERM."""
    service = ControlPlaneService(config, token=token, clock=clock)
    service.start()
    if not service.ready:
        service.stop()
        return 3
    logger = logging.getLogger("m6.control_plane")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("SIGINT received; shutting down")
    finally:
        service.stop()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = ControlPlaneConfig.load(args.config)
    if args.host:
        config = ControlPlaneConfig(
            **{**config.to_dict(), "host": args.host, "port": args.port or config.port}
        )
    elif args.port:
        config = ControlPlaneConfig(
            **{**config.to_dict(), "port": args.port}
        )
    return run_control_plane(config)


if __name__ == "__main__":
    import sys

    sys.exit(main())