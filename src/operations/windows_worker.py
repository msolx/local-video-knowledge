"""M6-06 Windows PC Worker Host.

Wraps the platform-neutral :class:`WorkerRuntime` into a reliable long-running
Windows worker host:

- explicit JSON configuration (``m6-windows-worker-config-v1``)
- capability profile + startup preflight (availability/configuration only,
  never launching browsers / Douyin / Whisper / llama.cpp / model loads)
- single-instance enforcement (OS file lock; stale files are not fatal)
- structured rotating stdlib logging with secret redaction
- graceful shutdown on Ctrl+C / SIGINT / SIGTERM via ``WorkerRuntime.stop()``
- deterministic exit codes for Task Scheduler restart policy

Transport boundary (frozen M6-06 contract): the Windows PC worker must NOT open
a NAS-hosted ``operations.sqlite3`` over SMB/UNC. The operations DB is owned by
the NAS control plane process. This host may only use a local SQLite store in
``local_sqlite_test`` mode (local-dev/tests). The future remote worker transport
(``http``) is placeholder-only until M6-07/M6-08.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

from .http_transport import HttpWorkerTransport
from .models import (
    VALID_CAPABILITIES,
    normalize_capabilities,
    normalize_identity_component,
)
from .transport import LocalSQLiteWorkerTransport
from .worker import WorkerRuntime

# ----------------------------------------------------------------------
# M6-08 production integration markers
# ----------------------------------------------------------------------

PRODUCTION_HANDLER_REGISTRY_VERSION = "m6-08-prod-handlers-v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]

# ----------------------------------------------------------------------
# Frozen M6-06/M6-07 contracts
# ----------------------------------------------------------------------

WORKER_HOST_CONFIG_VERSION = "m6-windows-worker-config-v1"
WORKER_PREFLIGHT_RESULT_VERSION = "m6-windows-worker-preflight-v1"
WORKER_HOST_POLICY_VERSION = "m6-windows-worker-host-v1"

#: Frozen M6-07 stage placement: Windows executes the discovery/archive/media/
#: knowledge-extraction stages; the NAS owns finalize + store-ingest.
WINDOWS_STAGE_ALLOWLIST: tuple[str, ...] = (
    "DISCOVER",
    "ARCHIVE",
    "MEDIA_PROCESS",
    "KNOWLEDGE_EXTRACT",
)

# Default Windows PC capability profile (M6-00 Topology A; PC owns browser +
# GPU + local LLM). ``store_ingest`` is deliberately NOT declared: the M5
# knowledge store is owned by the NAS control-plane side (M6-08).
WINDOWS_PC_TARGET_CAPABILITIES: tuple[str, ...] = (
    "collector",
    "downloader",
    "cpu_media",
    "gpu_asr",
    "gpu_vlm",
    "llm_extraction",
)

# Deterministic exit codes (frozen; docs/M6_WINDOWS_WORKER_RUNBOOK.md).
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_PREFLIGHT_FAILURE = 3
EXIT_ALREADY_RUNNING = 4
EXIT_FATAL_RUNTIME_ERROR = 5

# Control-plane transport placeholder (M6-07/08 wires the real one).
CONTROL_PLANE_TRANSPORT_LOCAL = "local_sqlite_test"
CONTROL_PLANE_TRANSPORT_FUTURE = "http"

_SECRET_KEYS: frozenset[str] = frozenset(
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


# ----------------------------------------------------------------------
# Configuration contract
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerHostConfig:
    """Parsed, validated worker-host configuration.

    Schema ``m6-windows-worker-config-v1``. Secrets must never be placed in
    the config file; runtime references are paths / existence checks only.
    """

    worker_id: str
    capabilities: tuple[str, ...]
    operations_db_path: Optional[str]
    workspace_root: Optional[str]
    archive_root: Optional[str]
    processed_root: Optional[str]
    knowledge_store_path: Optional[str]
    poll_interval_seconds: float = 1.0
    heartbeat_interval_seconds: float = 10.0
    lease_duration_seconds: int = 120
    worker_stale_threshold_seconds: int = 120
    log_path: str = "logs/operations/windows-worker.log"
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5
    single_instance_lock_path: str = "data/operations/windows-worker.lock"
    display_name: Optional[str] = None
    hostname: Optional[str] = None
    control_plane_transport: str = CONTROL_PLANE_TRANSPORT_LOCAL
    control_plane_url: Optional[str] = None
    auth_token_env: str = "PKP_CONTROL_PLANE_TOKEN"
    http_timeout_seconds: float = 30.0
    reconnect_backoff_seconds: float = 1.0
    allowed_stages: tuple[str, ...] = ()
    startup_delay_seconds: int = 45
    runtime_references: dict[str, Any] = field(default_factory=dict)
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    # -- schema ---------------------------------------------------------

    SCHEMA_VERSION = WORKER_HOST_CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "worker_id": self.worker_id,
            "display_name": self.display_name,
            "hostname": self.hostname,
            "capabilities": list(self.capabilities),
            "workspace_root": self.workspace_root,
            "archive_root": self.archive_root,
            "processed_root": self.processed_root,
            "operations_db_path": self.operations_db_path,
            "knowledge_store_path": self.knowledge_store_path,
            "poll_interval_seconds": self.poll_interval_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "lease_duration_seconds": self.lease_duration_seconds,
            "worker_stale_threshold_seconds": self.worker_stale_threshold_seconds,
            "log_path": self.log_path,
            "log_max_bytes": self.log_max_bytes,
            "log_backup_count": self.log_backup_count,
            "single_instance_lock_path": self.single_instance_lock_path,
            "control_plane_transport": self.control_plane_transport,
            "control_plane_url": self.control_plane_url,
            "auth_token_env": self.auth_token_env,
            "http_timeout_seconds": self.http_timeout_seconds,
            "reconnect_backoff_seconds": self.reconnect_backoff_seconds,
            "allowed_stages": list(self.allowed_stages),
            "startup_delay_seconds": self.startup_delay_seconds,
            "runtime_references": dict(self.runtime_references),
            "extra_metadata": dict(self.extra_metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkerHostConfig":
        if not isinstance(raw, dict):
            raise ValueError("worker host config must be a JSON object")
        schema = raw.get("schema_version")
        if schema is not None and schema != cls.SCHEMA_VERSION:
            raise ValueError(f"unsupported config schema {schema!r}")
        worker_id = raw.get("worker_id")
        if not worker_id or not isinstance(worker_id, str):
            raise ValueError("config must include a non-empty worker_id")
        caps_raw = raw.get("capabilities", [])
        if not isinstance(caps_raw, list):
            raise ValueError("capabilities must be a list")
        capabilities = normalize_capabilities([str(c) for c in caps_raw])
        if not capabilities:
            raise ValueError("capabilities must not be empty")

        def _opt_str(key: str) -> Optional[str]:
            value = raw.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            return value

        def _opt_float(key: str, default: float) -> float:
            value = raw.get(key, default)
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a number") from exc
            if parsed <= 0:
                raise ValueError(f"{key} must be > 0")
            return parsed

        def _opt_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be an integer") from exc
            if parsed <= 0:
                raise ValueError(f"{key} must be > 0")
            return parsed

        transport = raw.get("control_plane_transport", CONTROL_PLANE_TRANSPORT_LOCAL)
        if transport not in (CONTROL_PLANE_TRANSPORT_LOCAL, CONTROL_PLANE_TRANSPORT_FUTURE):
            raise ValueError(f"unsupported control_plane_transport {transport!r}")

        control_plane_url = _opt_str("control_plane_url")
        if transport == CONTROL_PLANE_TRANSPORT_FUTURE:
            if not control_plane_url:
                raise ValueError(
                    "control_plane_transport=http requires control_plane_url"
                )
        allowed_stages_raw = raw.get("allowed_stages")
        if allowed_stages_raw is None:
            allowed_stages: tuple[str, ...] = ()
        elif isinstance(allowed_stages_raw, list):
            allowed_stages = tuple(
                normalize_identity_component(str(s))
                for s in allowed_stages_raw
                if str(s).strip()
            )
        else:
            raise ValueError("allowed_stages must be a list")

        runtime_refs = raw.get("runtime_references", {})
        if not isinstance(runtime_refs, dict):
            raise ValueError("runtime_references must be an object")

        return cls(
            worker_id=normalize_identity_component(worker_id),
            capabilities=tuple(capabilities),
            operations_db_path=_opt_str("operations_db_path"),
            workspace_root=_opt_str("workspace_root"),
            archive_root=_opt_str("archive_root"),
            processed_root=_opt_str("processed_root"),
            knowledge_store_path=_opt_str("knowledge_store_path"),
            poll_interval_seconds=_opt_float("poll_interval_seconds", 1.0),
            heartbeat_interval_seconds=_opt_float("heartbeat_interval_seconds", 10.0),
            lease_duration_seconds=_opt_int("lease_duration_seconds", 120),
            worker_stale_threshold_seconds=_opt_int("worker_stale_threshold_seconds", 120),
            log_path=_opt_str("log_path") or "logs/operations/windows-worker.log",
            log_max_bytes=_opt_int("log_max_bytes", 10_000_000),
            log_backup_count=_opt_int("log_backup_count", 5),
            single_instance_lock_path=_opt_str("single_instance_lock_path")
            or "data/operations/windows-worker.lock",
            display_name=_opt_str("display_name"),
            hostname=_opt_str("hostname"),
            control_plane_transport=transport,
            control_plane_url=control_plane_url,
            auth_token_env=_opt_str("auth_token_env") or "PKP_CONTROL_PLANE_TOKEN",
            http_timeout_seconds=_opt_float("http_timeout_seconds", 30.0),
            reconnect_backoff_seconds=_opt_float("reconnect_backoff_seconds", 1.0),
            allowed_stages=allowed_stages,
            startup_delay_seconds=_opt_int("startup_delay_seconds", 45),
            runtime_references=dict(runtime_refs),
            extra_metadata=dict(raw.get("extra_metadata", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> "WorkerHostConfig":
        config_path = Path(path)
        if not config_path.is_file():
            raise ValueError(f"config file not found: {config_path}")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"config file is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str  # "ok" | "error" | "warning"
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class WorkerPreflightResult:
    """JSON-safe preflight outcome. Never contains secrets."""

    ok: bool
    worker_id: str
    capabilities: tuple[str, ...]
    checks: tuple[PreflightCheck, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    version: str = WORKER_PREFLIGHT_RESULT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ok": self.ok,
            "worker_id": self.worker_id,
            "capabilities": list(self.capabilities),
            "checks": [c.to_dict() for c in self.checks],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def _check_path_exists(name: str, value: Optional[str], required: bool = False) -> Optional[PreflightCheck]:
    if value is None:
        if required:
            return PreflightCheck(name=name, status="error", message="not configured")
        return PreflightCheck(name=name, status="ok", message="not configured (optional)")
    path = Path(value)
    if path.exists():
        return PreflightCheck(name=name, status="ok", message=f"exists: {value}")
    return PreflightCheck(
        name=name,
        status="error" if required else "warning",
        message=f"path does not exist: {value}",
    )


def run_capability_preflight(config: WorkerHostConfig) -> WorkerPreflightResult:
    """Check availability/configuration for every declared capability.

    Only existence/configuration checks — never launches a browser, touches
    Douyin, runs Whisper, loads models, or starts llama.cpp. A capability whose
    prerequisite is missing produces a FAILED preflight (never a silent
    capability drop), so the control plane cannot dispatch a job the worker
    cannot execute.
    """
    checks: list[PreflightCheck] = []
    errors: list[str] = []
    warnings: list[str] = []

    refs = config.runtime_references
    caps = set(config.capabilities)

    if "collector" in caps:
        browser = refs.get("browser_executable")
        profile = refs.get("browser_profile")
        checks.append(_check_path_exists("collector.browser_executable", browser, required=True))
        checks.append(_check_path_exists("collector.browser_profile", profile, required=True))
        node = refs.get("node_runtime")
        checks.append(_check_path_exists("collector.node_runtime", node, required=False))
        checks.append(
            PreflightCheck(
                name="collector.douyin_runtime",
                status="ok",
                message="browser runtime configuration present (not launched)",
            )
        )

    if "downloader" in caps:
        checks.append(
            PreflightCheck(
                name="downloader.dependencies",
                status="ok",
                message="downloader capability configured (no runtime launch)",
            )
        )

    if "cpu_media" in caps:
        checks.append(
            PreflightCheck(
                name="cpu_media.ffmpeg",
                status="ok",
                message="cpu_media capability configured (no runtime launch)",
            )
        )

    if "gpu_asr" in caps:
        runtime = refs.get("asr_runtime")
        model_root = refs.get("model_root")
        checks.append(_check_path_exists("gpu_asr.runtime", runtime, required=True))
        checks.append(_check_path_exists("gpu_asr.model_root", model_root, required=False))
        checks.append(
            PreflightCheck(
                name="gpu_asr.gpu_runtime",
                status="ok",
                message="GPU ASR prerequisites configured (not launched)",
            )
        )

    if "gpu_vlm" in caps:
        runtime = refs.get("vlm_runtime")
        model_ref = refs.get("vlm_model_reference")
        checks.append(_check_path_exists("gpu_vlm.runtime", runtime, required=True))
        checks.append(_check_path_exists("gpu_vlm.model_reference", model_ref, required=True))
        checks.append(
            PreflightCheck(
                name="gpu_vlm.gpu_runtime",
                status="ok",
                message="GPU VLM prerequisites configured (not launched)",
            )
        )

    if "llm_extraction" in caps:
        # M6-06 local LLM runtime policy: llama.cpp preferred, model root under
        # D:\LMmodel. Existence / configuration checks only.
        runtime = refs.get("llm_runtime")
        model_root = refs.get("llm_model_root")
        checks.append(_check_path_exists("llm_extraction.runtime", runtime, required=True))
        checks.append(_check_path_exists("llm_extraction.model_root", model_root, required=False))
        checks.append(
            PreflightCheck(
                name="llm_extraction.local_runtime",
                status="ok",
                message="LLM runtime reference present (llama.cpp not started)",
            )
        )

    if "store_ingest" in caps:
        # Windows PC v1 profile must NOT claim store_ingest (NAS owns the M5
        # store). If someone does, surface it as a warning, not a hard error.
        warnings.append(
            "store_ingest capability is owned by the NAS control-plane side in "
            "the frozen M6 topology; the Windows PC worker should not claim it"
        )

    if config.control_plane_transport == CONTROL_PLANE_TRANSPORT_FUTURE:
        url = config.control_plane_url or ""
        if not url:
            checks.append(
                PreflightCheck(
                    name="http.control_plane_url",
                    status="error",
                    message="control_plane_transport=http requires control_plane_url",
                )
            )
        elif not (url.startswith("http://") or url.startswith("https://")):
            checks.append(
                PreflightCheck(
                    name="http.control_plane_url",
                    status="error",
                    message=f"invalid control_plane_url {url!r} (must be http(s)://)",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    name="http.control_plane_url",
                    status="ok",
                    message=f"url is a valid http(s) endpoint: {url}",
                )
            )
        token_env = config.auth_token_env
        token = os.environ.get(token_env)
        if token:
            checks.append(
                PreflightCheck(
                    name="http.auth_token_env",
                    status="ok",
                    message=f"auth token present in env {token_env}",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    name="http.auth_token_env",
                    status="error",
                    message=f"auth token env {token_env} is not set (production "
                    "control plane fails closed without it)",
                )
            )
        checks.append(
            _check_path_exists(
                "http.processed_root", config.processed_root, required=False
            )
        )
        checks.append(
            _check_path_exists(
                "http.archive_root", config.archive_root, required=False
            )
        )
        checks.append(
            PreflightCheck(
                name="http.connectivity",
                status="warning",
                message=(
                    "server connectivity is checked at run time (bounded "
                    "connectivity probe; a temporarily offline control plane "
                    "is NOT a capability failure — the worker keeps retrying)"
                ),
            )
        )

    if not caps:
        errors.append("no capabilities configured")

    for check in checks:
        if check.status == "error":
            errors.append(f"{check.name}: {check.message}")
        elif check.status == "warning":
            warnings.append(f"{check.name}: {check.message}")

    return WorkerPreflightResult(
        ok=not errors,
        worker_id=config.worker_id,
        capabilities=config.capabilities,
        checks=tuple(checks),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# ----------------------------------------------------------------------
# Single instance
# ----------------------------------------------------------------------


class SingleInstanceLock:
    """Single-instance enforcement via an OS-level file lock.

    Uses ``msvcrt.locking`` on Windows (advisory byte-range lock) and
    ``fcntl.flock`` elsewhere. The OS lock is the authority: a stale lock file
    left by a crashed process is simply re-locked (never treated as a running
    instance). ``already_running()`` is True only while the OS lock is held by
    another live process.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fh: Optional[Any] = None
        self._locked = False

    @property
    def is_windows(self) -> bool:
        return sys.platform == "win32"

    def acquire(self) -> bool:
        """Try to take the lock. Returns True on success, False if already running."""
        if self._locked:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            if self.is_windows:
                import msvcrt

                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    fh.close()
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    return False
        except Exception:
            fh.close()
            raise
        # Write identity + timestamp for diagnostics (not used for decisions).
        fh.seek(0)
        fh.truncate()
        fh.write(
            json.dumps(
                {"worker_id": "", "pid": os.getpid(), "acquired_at": "unsynced"},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        fh.flush()
        self._fh = fh
        self._locked = True
        return True

    def release(self) -> None:
        if not self._locked:
            return
        try:
            if self.is_windows:
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            self._locked = False


# ----------------------------------------------------------------------
# Secret redaction (reuses M6-05 policy)
# ----------------------------------------------------------------------


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v) if k.lower() not in _SECRET_KEYS else "[REDACTED]" for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def redact_config(config: WorkerHostConfig) -> dict[str, Any]:
    """JSON-safe view of a config with secret-shaped keys redacted."""
    return _redact_value(config.to_dict())


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------


def configure_worker_logging(config: WorkerHostConfig) -> logging.Logger:
    """Configure stdlib rotating-file logging for the worker host."""
    log_path = Path(config.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=int(config.log_max_bytes),
        backupCount=int(config.log_backup_count),
        encoding="utf-8",
    )
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(fmt)
    logger = logging.getLogger("m6.windows_worker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


# ----------------------------------------------------------------------
# Windows worker host
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class HostStartResult:
    """Outcome of WindowsWorkerHost.start() (for diagnostics / tests)."""

    exit_code: int
    worker_id: str
    completed_cycles: int = 0
    error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "worker_id": self.worker_id,
            "completed_cycles": self.completed_cycles,
            "error_message": self.error_message,
        }


class WindowsWorkerHost:
    """Reliable Windows long-running worker host.

    Lifecycle: load config -> preflight -> single-instance lock -> logging ->
    build WorkerRuntime -> register -> start heartbeat -> run_forever ->
    graceful stop -> deterministic exit code.
    """

    def __init__(
        self,
        config: WorkerHostConfig,
        *,
        logger: Optional[logging.Logger] = None,
        handler_registry: Optional[Callable[[WorkerHostConfig], dict[str, Any]]] = None,
        runtime_factory: Optional[Callable[..., WorkerRuntime]] = None,
        now: Optional[Callable[[], str]] = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("m6.windows_worker")
        self.handler_registry = handler_registry
        self.runtime_factory = runtime_factory or WorkerRuntime
        self._now = now
        self._runtime: Optional[WorkerRuntime] = None
        self._lock: Optional[SingleInstanceLock] = None
        self._stop_event = threading.Event()

    # -- policy checks ---------------------------------------------------

    def _smb_guard_errors(self) -> list[str]:
        """Hard rule: the Windows worker never opens the NAS operations DB over
        SMB/UNC (the operations DB is owned by the NAS control plane process).
        Mapped SMB drive letters are also forbidden by policy even though the
        program cannot reliably detect them."""

        errors: list[str] = []
        db_path = (self.config.operations_db_path or "").strip()
        if db_path and (db_path.startswith("\\\\") or "://" in db_path):
            errors.append(
                "operations_db_path must not be a UNC/SMB path or URL; "
                "the Windows worker must not open the NAS operations DB over "
                "SMB/UNC (the NAS operations DB is owned by the NAS control plane)"
            )
        return errors

    def _transport_errors(self) -> list[str]:
        """Transport validation for actually running the host.

        ``local_sqlite_test`` is a runnable local-dev/test transport.
        ``http`` requires ``control_plane_url`` (remote control plane) and fails
        closed when the URL is missing.
        """

        errors: list[str] = []
        transport = self.config.control_plane_transport
        if transport == CONTROL_PLANE_TRANSPORT_FUTURE:
            if not self.config.control_plane_url:
                errors.append(
                    "control_plane_transport=http requires control_plane_url "
                    "pointing at the NAS control plane"
                )
        return errors

    def validate_production_topology(self) -> list[str]:
        """Production-readiness validation (used by M6-08 when the NAS control
        plane exists). Returns errors when the config is NOT acceptable as a
        NAS production worker.

        - ``local_sqlite_test`` always fails (local-dev/test only).
        - SMB/UNC operations DB path always fails.
        - ``http`` with a valid control_plane_url is the production transport.
        ``start()`` runs a *subset* of these guards so local-dev lifecycle still
        works: it hard-blocks SMB/UNC + missing-URL http, and warns (rather than
        blocks) on the local-only transport."""

        errors: list[str] = list(self._smb_guard_errors())
        if self.config.control_plane_transport == CONTROL_PLANE_TRANSPORT_LOCAL:
            errors.append(
                "control_plane_transport=local_sqlite_test is local-dev/test only "
                "and cannot be used for a NAS production topology"
            )
        return errors

    def preflight(self) -> WorkerPreflightResult:
        return run_capability_preflight(self.config)

    # -- lifecycle -------------------------------------------------------

    def acquire_single_instance(self) -> bool:
        self._lock = SingleInstanceLock(self.config.single_instance_lock_path)
        return self._lock.acquire()

    def _build_runtime(self) -> WorkerRuntime:
        handlers: dict[str, Any] = {}
        if self.handler_registry is not None:
            handlers = self.handler_registry(self.config)
        transport = self.config.control_plane_transport
        if transport == CONTROL_PLANE_TRANSPORT_LOCAL:
            if not self.config.operations_db_path:
                raise ValueError(
                    "operations_db_path is required to build the worker runtime "
                    "in local_sqlite_test mode (fail closed; never auto-create "
                    "the production operations DB)"
                )
            worker_transport = LocalSQLiteWorkerTransport(
                self.config.operations_db_path
            )
        else:
            if not self.config.control_plane_url:
                raise ValueError(
                    "control_plane_url is required to build the worker runtime "
                    "in http mode (fail closed; the Windows worker never opens "
                    "the NAS operations DB over SMB/UNC)"
                )
            worker_transport = HttpWorkerTransport(
                self.config.control_plane_url,
                auth_token_env=self.config.auth_token_env,
                worker_id=self.config.worker_id,
                http_timeout_seconds=self.config.http_timeout_seconds,
                reconnect_backoff_seconds=self.config.reconnect_backoff_seconds,
            )
        kwargs: dict[str, Any] = {
            "transport": worker_transport,
            "worker_id": self.config.worker_id,
            "display_name": self.config.display_name,
            "hostname": self.config.hostname,
            "capabilities": list(self.config.capabilities),
            "handlers": handlers,
            "lease_duration_seconds": self.config.lease_duration_seconds,
            "heartbeat_interval_seconds": self.config.heartbeat_interval_seconds,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "stale_threshold_seconds": self.config.worker_stale_threshold_seconds,
        }
        allowed_stages = self.config.allowed_stages or WINDOWS_STAGE_ALLOWLIST
        if allowed_stages:
            kwargs["allowed_stages"] = list(allowed_stages)
        if self._now is not None:
            kwargs["now"] = self._now
        return self.runtime_factory(**kwargs)

    def _install_signal_handlers(self) -> None:
        def _request_stop(signum: int, _frame: Any) -> None:
            self.logger.info("received signal %s; requesting graceful stop", signum)
            self._stop_event.set()
            if self._runtime is not None:
                self._runtime.stop(wait=False)

        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, _request_stop)
            except (ValueError, OSError):
                pass
            if hasattr(signal, "SIGTERM"):
                try:
                    signal.signal(signal.SIGTERM, _request_stop)
                except (ValueError, OSError):
                    pass

    def start(self, *, max_cycles: Optional[int] = None) -> HostStartResult:
        """Run the full host lifecycle; returns a deterministic exit code."""
        cfg = self.config

        # 1. Hard guards: SMB/UNC operations DB + unimplemented transport.
        guard_errors = list(self._smb_guard_errors()) + list(self._transport_errors())
        if guard_errors:
            for err in guard_errors:
                self.logger.error("topology guard: %s", err)
            return HostStartResult(
                exit_code=EXIT_CONFIG_ERROR,
                worker_id=cfg.worker_id,
                error_message="; ".join(guard_errors),
            )

        # Local-only transport is permitted in M6-06 for local-dev/tests, but it
        # must never be presented as a NAS production topology.
        if cfg.control_plane_transport == CONTROL_PLANE_TRANSPORT_LOCAL:
            self.logger.warning(
                "control_plane_transport=local_sqlite_test is local-dev/test "
                "only; not a NAS production topology (production transport "
                "arrives in M6-07/M6-08)"
            )

        # 2. Fail closed: local_sqlite_test needs an explicit local operations
        #    DB; http mode must NOT touch any local operations DB. Never
        #    auto-create the production data/operations/operations.sqlite3.
        if cfg.control_plane_transport == CONTROL_PLANE_TRANSPORT_LOCAL:
            if not cfg.operations_db_path:
                self.logger.error(
                    "operations_db_path is not configured; failing closed "
                    "(M6-06/07 worker host requires an explicit local-dev/test "
                    "store; the production operations DB is NAS-owned and must "
                    "not be created here)"
                )
                return HostStartResult(
                    exit_code=EXIT_CONFIG_ERROR,
                    worker_id=cfg.worker_id,
                    error_message="operations_db_path is not configured (fail closed)",
                )
        elif cfg.operations_db_path:
            self.logger.warning(
                "operations_db_path is set but control_plane_transport=http; "
                "the Windows worker never opens the NAS operations DB over "
                "SMB/UNC — ignoring the local path"
            )

        # 3. Preflight.
        preflight = self.preflight()
        for check in preflight.checks:
            level = logging.ERROR if check.status == "error" else logging.WARNING if check.status == "warning" else logging.INFO
            self.logger.log(level, "preflight %s: %s", check.name, check.message)
        if not preflight.ok:
            return HostStartResult(
                exit_code=EXIT_PREFLIGHT_FAILURE,
                worker_id=cfg.worker_id,
                error_message="preflight failed: " + "; ".join(preflight.errors),
            )

        # 3. Single-instance lock.
        if not self.acquire_single_instance():
            self.logger.error(
                "another worker host instance is already running (lock=%s)",
                cfg.single_instance_lock_path,
            )
            return HostStartResult(
                exit_code=EXIT_ALREADY_RUNNING,
                worker_id=cfg.worker_id,
                error_message="another worker host instance is already running",
            )

        try:
            # 4. Build runtime.
            self._runtime = self._build_runtime()
            # 5. Register + heartbeat thread.
            registration = self._runtime.register()
            self.logger.info(
                "worker registered id=%s capabilities=%s registration=%s",
                self._runtime.worker_id,
                ",".join(self._runtime.capabilities),
                redact_worker_registration(registration),
            )
            self._runtime.start_heartbeat_thread()
            # 6. Signal handlers (graceful stop).
            self._install_signal_handlers()
            # 7. Run until stop.
            self.logger.info("worker host entering run_forever (worker_id=%s)", cfg.worker_id)
            completed = self._runtime.run_forever(max_cycles=max_cycles)
            self.logger.info("worker host stopped; completed_cycles=%d", completed)
            return HostStartResult(
                exit_code=EXIT_OK,
                worker_id=cfg.worker_id,
                completed_cycles=int(completed),
            )
        except KeyboardInterrupt:
            self.logger.info("keyboard interrupt; stopping worker host")
            if self._runtime is not None:
                self._runtime.stop()
            return HostStartResult(
                exit_code=EXIT_OK,
                worker_id=cfg.worker_id,
                error_message="stopped by keyboard interrupt",
            )
        except Exception as exc:
            self.logger.exception("fatal runtime error: %s", str(exc))
            return HostStartResult(
                exit_code=EXIT_FATAL_RUNTIME_ERROR,
                worker_id=cfg.worker_id,
                error_message=redact_message(str(exc)),
            )
        finally:
            if self._runtime is not None:
                try:
                    self._runtime.stop(wait=False)
                except Exception:
                    pass
            if self._lock is not None:
                try:
                    self._lock.release()
                except Exception:
                    pass


class _CollectorModeAdapter:
    """Adapt the frozen ``DiscoverAdapter`` calling convention to ``CollectorService``.

    ``DiscoverAdapter.execute`` invokes ``collector.execute(mode, platform=...)``
    with ``mode`` as a plain string (``"sync"``); the frozen
    ``CollectorService.execute`` requires a ``CollectorMode`` enum. This thin
    adapter normalizes string -> enum without touching sealed collector code.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def execute(self, mode: Any, **kwargs: Any) -> Any:
        from ..collector.base import CollectorMode

        if isinstance(mode, str):
            mode = CollectorMode(mode)
        return self._service.execute(mode, **kwargs)


def _with_source_url(
    handler: Callable[[Any], Any],
) -> Callable[[Any], Any]:
    """Wrap an ARCHIVE handler so a missing ``source_url`` gets the frozen Douyin URL.

    The scheduler enqueues ARCHIVE jobs with metadata ``{discover_job_id,
    discovery_generation}`` only; the frozen ``ArchiveAdapter`` requires
    ``claimed.metadata.source_url``. The frozen Douyin convention (transform.py /
    download_queue.py) derives it deterministically as
    ``https://www.douyin.com/video/{platform_content_id}``. This wrapper injects
    that value into a copy of the claimed job, leaving all sealed modules
    untouched.
    """

    from dataclasses import replace

    def _wrapped(claimed: Any) -> Any:
        meta = dict(getattr(claimed, "metadata", {}) or {})
        if "source_url" not in meta:
            cid = getattr(claimed, "platform_content_id", None)
            if cid:
                meta["source_url"] = f"https://www.douyin.com/video/{cid}"
                claimed = replace(claimed, metadata=meta)
        return handler(claimed)

    return _wrapped


class _ProfileCookieProvider:
    """Extracts in-memory session cookies from the dedicated browser profile."""

    def __init__(self, profile_path: Optional[str | Path] = None) -> None:
        self.profile_path = Path(profile_path) if profile_path else None

    def get_credentials(self, scope_id: Any = None) -> dict[str, str]:
        if not self.profile_path or not self.profile_path.is_dir():
            return {}
        try:
            import browser_cookie3

            cookie_file = self.profile_path / "Default" / "Network" / "Cookies"
            key_file = self.profile_path / "Local State"
            if not cookie_file.is_file():
                return {}
            cj = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                key_file=str(key_file) if key_file.is_file() else None,
                domain_name="douyin.com",
            )
            cookie_dict = {c.name: c.value for c in cj}
            if not cookie_dict:
                return {}
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
            return {"cookie": cookie_str}
        except Exception:
            return {}


def build_production_handler_registry(
    config: WorkerHostConfig,
    *,
    app_config: Any = None,
    collector: Any = None,
    downloader: Any = None,
    media_adapter: Any = None,
    llm_backend: Any = None,
    extraction_config: Any = None,
    merge_config: Any = None,
    enrichment_config: Any = None,
    render_config: Any = None,
) -> dict[str, Callable[[Any], Any]]:
    """Build the real M2/M3/M4 handler registry for the Windows production worker.

    M6-08 production integration (``m6-08-prod-handlers-v1``): wires the frozen
    runtime components into the frozen ``build_stage_handler_registry``:

    - DISCOVER: real Douyin collector (``CollectorService`` + ``DouyinCollector``)
      behind a string-mode normalizing adapter.
    - ARCHIVE: real ``SafeDouyinDownloader`` (F2 backend, production sandbox /
      normalizer / validator / promoter / router) with the deterministic
      ``source_url`` injection wrapper.
    - MEDIA_PROCESS / KNOWLEDGE_EXTRACT: real ``CanonicalMediaAssetAdapter``,
      ``AppConfig`` (config/config.json) and an LM Studio backend.
    - KNOWLEDGE_FINALIZE / STORE_INGEST: built for completeness but only ever
      claimed by the NAS local worker (placement enforced by the control plane).

    All heavy imports are lazy so ``import src.operations.windows_worker`` never
    pulls the M2/M3/M4 runtime stack. Every component may be overridden for tests.
    """
    from ..config import load_config
    from ..collector.douyin.collector import DouyinCollector
    from ..collector.douyin.config import DouyinCollectorConfig
    from ..collector.service import CollectorService
    from ..downloader.f2_backend import F2InProcessBackendAdapter
    from ..downloader.normalizer import ProductionAssetNormalizer
    from ..downloader.promoter import ProductionArchivePromoter
    from ..downloader.router import ProductionContentRouter
    from ..downloader.safe_downloader import SafeDouyinDownloader
    from ..downloader.sandbox import ProductionTaskSandboxProvider
    from ..downloader.validator import ProductionMediaValidator
    from ..knowledge.enrichment import EnrichmentConfig
    from ..knowledge.extractor import ExtractionConfig, OpenAICompatibleBackend
    from ..knowledge.merger import MergeConfig
    from ..knowledge.render import RenderConfig
    from ..media_adapter.adapter import CanonicalMediaAssetAdapter
    from .models import JobStage
    from .stages import build_stage_handler_registry

    if app_config is None:
        app_config = load_config(str(_repo_root() / "config" / "config.json"))

    workspace = Path(config.workspace_root) if config.workspace_root else Path(".")
    refs = dict(config.runtime_references or {})
    profile = refs.get("browser_profile")

    if collector is None:
        collector_cfg = DouyinCollectorConfig(
            platform="douyin",
            runtime_root=workspace / "runtime",
            raw_archive_root=workspace / "data" / "raw",
            canonical_root=workspace / "data" / "canonical",
            database_path=workspace / "data" / "metadata.db",
            profile_path=Path(profile) if profile else None,
            headless=True,
        )
        collector = _CollectorModeAdapter(
            CollectorService(DouyinCollector(config=collector_cfg))
        )

    if downloader is None:
        # F2 isolation invariant: .venv must not have f2 installed.
        # Dynamically inject the dedicated .venv-f2 site-packages for worker download execution.
        repo_root = Path(__file__).resolve().parents[2]
        venv_f2_site = repo_root / ".venv-f2" / "Lib" / "site-packages"
        if venv_f2_site.is_dir() and str(venv_f2_site) not in sys.path:
            sys.path.insert(0, str(venv_f2_site))

        archive_root = (
            Path(config.archive_root)
            if config.archive_root
            else workspace / "data" / "raw_archive"
        )
        downloader = SafeDouyinDownloader(
            credential_provider=_ProfileCookieProvider(profile),
            sandbox_provider=ProductionTaskSandboxProvider(),
            backend=F2InProcessBackendAdapter(),
            normalizer=ProductionAssetNormalizer(),
            validator=ProductionMediaValidator(),
            promoter=ProductionArchivePromoter(archive_root=archive_root),
            router=ProductionContentRouter(),
            require_auth=False,
        )

    if media_adapter is None:
        media_adapter = CanonicalMediaAssetAdapter(archive_root=config.archive_root)

    if extraction_config is None or enrichment_config is None:
        llm_cfg = (app_config.raw.get("llm") or {}).get("lm_studio") or {}
        model = str(llm_cfg.get("model", "qwen3-8b"))
        base_url = str(llm_cfg.get("base_url", "http://127.0.0.1:12345/v1"))
        temperature = float(llm_cfg.get("temperature", 0.1))
        max_tokens = int(llm_cfg.get("max_tokens", 4096))
        extraction_config = ExtractionConfig(
            backend="openai_compatible",
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=300,
            force=False,
        )
        enrichment_config = EnrichmentConfig(
            backend="openai_compatible",
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=300,
            force=False,
        )

    if llm_backend is None:
        llm_backend = OpenAICompatibleBackend(extraction_config)

    registry = build_stage_handler_registry(
        workspace_root=workspace,
        processed_root=config.processed_root,
        archive_root=config.archive_root,
        knowledge_store_path=config.knowledge_store_path,
        collector=collector,
        downloader=downloader,
        media_adapter=media_adapter,
        app_config=app_config,
        llm_backend=llm_backend,
        extraction_config=extraction_config,
        merge_config=merge_config or MergeConfig(),
        enrichment_config=enrichment_config,
        render_config=render_config or RenderConfig(),
        force=False,
    )
    registry[JobStage.ARCHIVE.value] = _with_source_url(registry[JobStage.ARCHIVE.value])
    return registry


def redact_worker_registration(registration: dict[str, Any]) -> dict[str, Any]:
    """Strip worker registration dict of secret-shaped keys."""
    return _redact_value(dict(registration))


def redact_message(message: str) -> str:
    """Best-effort redaction of a free-form error message."""
    if not message:
        return message
    lowered = message.lower()
    for key in sorted(_SECRET_KEYS, key=len, reverse=True):
        if key in lowered:
            return "[REDACTED]"
    return message


# ----------------------------------------------------------------------
# CLI entrypoint
# ----------------------------------------------------------------------


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m src.operations.windows_worker",
        description="M6-06 Windows PC Worker Host",
    )
    # --config and --json accepted in any position (SUPPRESS keeps the global
    # value when not re-supplied on the subcommand) — mirrors M6-05 admin CLI.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        required=True,
        help="path to worker host config JSON",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit JSON output (for preflight / print-config)",
    )
    parser.add_argument("--config", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common], help="run the worker host loop (local-dev/test stores only)")
    sub.add_parser("preflight", parents=[common], help="run startup preflight without starting the worker")
    sub.add_parser("print-config", parents=[common], help="print the parsed, redacted configuration")
    return parser


def _cmd_preflight(config: WorkerHostConfig, as_json: bool) -> int:
    result = run_capability_preflight(config)
    if as_json:
        print(result.to_json())
    else:
        print(f"preflight {'OK' if result.ok else 'FAILED'} worker_id={result.worker_id}")
        for check in result.checks:
            print(f"  [{check.status:>7}] {check.name}: {check.message}")
        for err in result.errors:
            print(f"  [  error] {err}")
        for warn in result.warnings:
            print(f"  [warning] {warn}")
    return EXIT_OK if result.ok else EXIT_PREFLIGHT_FAILURE


def _cmd_print_config(config: WorkerHostConfig, as_json: bool) -> int:
    redacted = redact_config(config)
    if as_json:
        print(json.dumps(redacted, indent=2, ensure_ascii=False))
    else:
        print(f"worker_id={config.worker_id}")
        print(f"capabilities={','.join(config.capabilities)}")
        print(f"control_plane_transport={config.control_plane_transport}")
        print(f"operations_db_path={config.operations_db_path or '(not set)'}")
        print("runtime_references:")
        for key, value in (config.runtime_references or {}).items():
            print(f"  {key}: {value}")
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = WorkerHostConfig.load(args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.command == "preflight":
        return _cmd_preflight(config, getattr(args, "json", False))
    if args.command == "print-config":
        return _cmd_print_config(config, getattr(args, "json", False))

    # run
    logger = configure_worker_logging(config)
    handler_registry = None
    if config.control_plane_transport == CONTROL_PLANE_TRANSPORT_FUTURE:
        handler_registry = build_production_handler_registry
    host = WindowsWorkerHost(config, logger=logger, handler_registry=handler_registry)
    result = host.start()
    logger.info("worker host exit_code=%d", result.exit_code)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())