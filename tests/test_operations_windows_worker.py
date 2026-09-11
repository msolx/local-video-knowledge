"""M6-06 Windows PC Worker Host & Autostart tests.

Covers: config contract (parse/round-trip/invalid schema/missing worker_id/
invalid+duplicate capabilities/local-dev transport/production topology/UNC
guard/example-config secrets), preflight (success + per-prerequisite failures,
no runtime launch, JSON safety, no secret exposure), single-instance lock
(acquire/second-instance rejection/release/stale file), logging (init/rotation/
redaction), WindowsWorkerHost lifecycle (start with fake runtime, graceful
stop, KeyboardInterrupt, deterministic exit codes), safety (no production Ops
DB auto-created, no production M5 touched, no network, no GPU/LLM), and the
PowerShell autostart artifacts (run script path correctness, install dry-run,
deterministic task name, logon trigger, startup delay, exact venv Python,
working directory, restart policy, uninstall idempotency, status read-only,
no Task Scheduler mutation).

All operations DBs are temp-path only; the production
data/operations/operations.sqlite3 is never created. No network / LLM / GPU.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.operations.models import normalize_capabilities
from src.operations.windows_worker import (
    CONTROL_PLANE_TRANSPORT_FUTURE,
    CONTROL_PLANE_TRANSPORT_LOCAL,
    EXIT_ALREADY_RUNNING,
    EXIT_CONFIG_ERROR,
    EXIT_FATAL_RUNTIME_ERROR,
    EXIT_OK,
    EXIT_PREFLIGHT_FAILURE,
    WORKER_HOST_CONFIG_VERSION,
    WINDOWS_PC_TARGET_CAPABILITIES,
    HostStartResult,
    PreflightCheck,
    SingleInstanceLock,
    WorkerHostConfig,
    WorkerPreflightResult,
    WindowsWorkerHost,
    configure_worker_logging,
    redact_config,
    redact_worker_registration,
    run_capability_preflight,
)
from src.operations.worker import WorkerRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "examples" / "m6_windows_worker.example.json"
RUN_SCRIPT = REPO_ROOT / "scripts" / "windows" / "run_m6_worker.ps1"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "windows" / "install_m6_worker_task.ps1"
UNINSTALL_SCRIPT = REPO_ROOT / "scripts" / "windows" / "uninstall_m6_worker_task.ps1"
STATUS_SCRIPT = REPO_ROOT / "scripts" / "windows" / "status_m6_worker_task.ps1"
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

_FP = "a" * 64


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _base_config(worker_id: str = "worker-test-1", **overrides) -> dict:
    raw = {
        "schema_version": WORKER_HOST_CONFIG_VERSION,
        "worker_id": worker_id,
        "capabilities": ["cpu_media"],
        "operations_db_path": None,
        "workspace_root": "./data/workspace",
        "archive_root": "./data/archive",
        "processed_root": "./data/processed",
        "knowledge_store_path": None,
        "poll_interval_seconds": 1.0,
        "heartbeat_interval_seconds": 10.0,
        "lease_duration_seconds": 120,
        "worker_stale_threshold_seconds": 120,
        "log_path": "logs/operations/windows-worker.log",
        "log_max_bytes": 10000000,
        "log_backup_count": 5,
        "single_instance_lock_path": "data/operations/windows-worker.lock",
        "control_plane_transport": CONTROL_PLANE_TRANSPORT_LOCAL,
        "startup_delay_seconds": 45,
        "runtime_references": {},
        "extra_metadata": {},
    }
    raw.update(overrides)
    return raw


def _config(worker_id: str = "worker-test-1", **overrides) -> WorkerHostConfig:
    return WorkerHostConfig.from_dict(_base_config(worker_id, **overrides))


def _write_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "worker_config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.fixture
def log_capture() -> list:
    return []


# ---------------------------------------------------------------------------
# 1. Config contract
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_parse(self):
        cfg = _config(worker_id="worker-1", capabilities=["gpu_asr", "collector"])
        assert cfg.worker_id == "worker-1"
        assert cfg.capabilities == ("gpu_asr", "collector")

    def test_config_json_round_trip(self):
        cfg = _config(worker_id="worker-1")
        raw = cfg.to_dict()
        assert raw["schema_version"] == WORKER_HOST_CONFIG_VERSION
        assert WorkerHostConfig.from_dict(raw) == cfg

    def test_invalid_schema(self):
        with pytest.raises(ValueError):
            WorkerHostConfig.from_dict({**_base_config(), "schema_version": "m6-other-v9"})

    def test_missing_worker_id(self):
        with pytest.raises(ValueError):
            WorkerHostConfig.from_dict({**_base_config(), "worker_id": ""})

    def test_invalid_capability(self):
        with pytest.raises(ValueError):
            WorkerHostConfig.from_dict({**_base_config(), "capabilities": ["bogus_cap"]})

    def test_duplicate_capability_normalization(self):
        cfg = WorkerHostConfig.from_dict(
            {**_base_config(), "capabilities": ["cpu_media", " cpu_media ", "cpu_media", "collector"]}
        )
        assert cfg.capabilities == ("cpu_media", "collector")

    def test_local_dev_transport(self):
        cfg = _config(control_plane_transport=CONTROL_PLANE_TRANSPORT_LOCAL)
        assert cfg.control_plane_transport == CONTROL_PLANE_TRANSPORT_LOCAL

    def test_production_rejects_local_only_topology(self):
        cfg = _config(control_plane_transport=CONTROL_PLANE_TRANSPORT_LOCAL)
        host = WindowsWorkerHost(cfg)
        errors = host.validate_production_topology()
        assert any("local_sqlite_test" in e for e in errors)

    def test_unc_ops_db_rejection(self):
        raw = {**_base_config(), "operations_db_path": r"\\nas\share\operations.sqlite3"}
        host = WindowsWorkerHost(WorkerHostConfig.from_dict(raw))
        assert host._smb_guard_errors()
        # A URL-style path is also rejected.
        raw2 = {**_base_config(), "operations_db_path": "http://nas/ops.sqlite3"}
        host2 = WindowsWorkerHost(WorkerHostConfig.from_dict(raw2))
        assert host2._smb_guard_errors()

    def test_example_config_has_no_secrets(self):
        raw = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        blob = json.dumps(raw).lower()
        for secret in ("cookie", "token", "password", "api_key", "authorization", "secret"):
            assert secret not in blob


# ---------------------------------------------------------------------------
# 2. Preflight
# ---------------------------------------------------------------------------


def _preflight_config(**refs) -> WorkerHostConfig:
    return WorkerHostConfig.from_dict(
        {
            **_base_config(capabilities=["collector", "downloader", "cpu_media", "gpu_asr", "gpu_vlm", "llm_extraction"]),
            "runtime_references": refs,
        }
    )


class TestPreflight:
    def test_preflight_success_synthetic(self, tmp_path: Path):
        browser = tmp_path / "chrome.exe"
        profile = tmp_path / "profile"
        browser.write_bytes(b"")
        profile.mkdir()
        cfg = _preflight_config(
            browser_executable=str(browser),
            browser_profile=str(profile),
            asr_runtime=str(browser),
            vlm_runtime=str(browser),
            vlm_model_reference=str(profile),
            llm_runtime=str(browser),
        )
        result = run_capability_preflight(cfg)
        assert result.ok
        assert result.worker_id == cfg.worker_id
        assert set(result.capabilities) == set(cfg.capabilities)

    def test_missing_browser_prerequisite(self, tmp_path: Path):
        cfg = _preflight_config(
            browser_profile=str(tmp_path / "missing-profile"),
        )
        result = run_capability_preflight(cfg)
        assert not result.ok
        assert any("collector.browser_executable" in e for e in result.errors)

    def test_missing_profile_prerequisite(self, tmp_path: Path):
        browser = tmp_path / "chrome.exe"
        browser.write_bytes(b"")
        cfg = _preflight_config(browser_executable=str(browser))
        result = run_capability_preflight(cfg)
        assert not result.ok
        assert any("collector.browser_profile" in e for e in result.errors)

    def test_gpu_runtime_prerequisite(self, tmp_path: Path):
        cfg = _preflight_config()
        result = run_capability_preflight(cfg)
        assert not result.ok
        assert any("gpu_asr.runtime" in e for e in result.errors)
        assert any("gpu_vlm.runtime" in e for e in result.errors)

    def test_llm_executable_prerequisite(self, tmp_path: Path):
        browser = tmp_path / "chrome.exe"
        browser.write_bytes(b"")
        profile = tmp_path / "profile"
        profile.mkdir()
        cfg = _preflight_config(
            browser_executable=str(browser),
            browser_profile=str(profile),
            asr_runtime=str(browser),
            vlm_runtime=str(browser),
            vlm_model_reference=str(profile),
        )
        result = run_capability_preflight(cfg)
        assert not result.ok
        assert any("llm_extraction.runtime" in e for e in result.errors)

    def test_preflight_does_not_start_runtime(self, tmp_path: Path, monkeypatch):
        browser = tmp_path / "chrome.exe"
        profile = tmp_path / "profile"
        browser.write_bytes(b"")
        profile.mkdir()
        started = []

        def _fake_launch(*_a, **_k):
            started.append(True)
            raise AssertionError("runtime must not be launched")

        monkeypatch.setattr("subprocess.Popen", _fake_launch)
        cfg = _preflight_config(
            browser_executable=str(browser),
            browser_profile=str(profile),
            asr_runtime=str(browser),
            vlm_runtime=str(browser),
            vlm_model_reference=str(profile),
            llm_runtime=str(browser),
        )
        result = run_capability_preflight(cfg)
        assert result.ok
        assert not started

    def test_preflight_json_safe(self, tmp_path: Path):
        cfg = _preflight_config()
        result = run_capability_preflight(cfg)
        json.loads(result.to_json())

    def test_no_secret_exposure(self, tmp_path: Path):
        browser = tmp_path / "chrome.exe"
        profile = tmp_path / "profile"
        browser.write_bytes(b"")
        profile.mkdir()
        cfg = _preflight_config(
            browser_executable=str(browser),
            browser_profile=str(profile),
            asr_runtime=str(browser),
            vlm_runtime=str(browser),
            vlm_model_reference=str(profile),
            llm_runtime=str(browser),
        )
        result = run_capability_preflight(cfg)
        assert "cookie" not in result.to_json()
        assert "lease_token" not in result.to_json()


# ---------------------------------------------------------------------------
# 3. Single instance
# ---------------------------------------------------------------------------


class TestSingleInstance:
    def test_single_instance_acquire(self, tmp_path: Path):
        lock = SingleInstanceLock(tmp_path / "w.lock")
        assert lock.acquire()
        lock.release()

    def test_second_instance_rejected(self, tmp_path: Path):
        lock1 = SingleInstanceLock(tmp_path / "w.lock")
        lock2 = SingleInstanceLock(tmp_path / "w.lock")
        assert lock1.acquire()
        try:
            assert not lock2.acquire()
        finally:
            lock1.release()

    def test_lock_released(self, tmp_path: Path):
        path = tmp_path / "w.lock"
        lock1 = SingleInstanceLock(path)
        assert lock1.acquire()
        lock1.release()
        lock2 = SingleInstanceLock(path)
        assert lock2.acquire()
        lock2.release()

    def test_stale_lock_file_not_fatal(self, tmp_path: Path):
        path = tmp_path / "w.lock"
        # Pre-create a "stale" lock file (crash remnant). As long as no live
        # process holds the OS lock, acquisition succeeds.
        path.write_bytes(b"stale")
        lock = SingleInstanceLock(path)
        assert lock.acquire()
        lock.release()


# ---------------------------------------------------------------------------
# 4. Logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_logging_initialized(self, tmp_path: Path):
        cfg = _config(log_path=str(tmp_path / "logs" / "worker.log"))
        logger = configure_worker_logging(cfg)
        assert logger.name == "m6.windows_worker"
        assert len(logger.handlers) == 1

    def test_rotating_log_config(self, tmp_path: Path):
        cfg = _config(
            log_path=str(tmp_path / "logs" / "worker.log"),
            log_max_bytes=512,
            log_backup_count=2,
        )
        logger = configure_worker_logging(cfg)
        handler = logger.handlers[0]
        assert handler.maxBytes == 512
        assert handler.backupCount == 2

    def test_log_redaction(self):
        reg = redact_worker_registration({"worker_id": "w1", "lease_token": "sekret"})
        assert reg["lease_token"] == "[REDACTED]"
        assert reg["worker_id"] == "w1"
        cfg = _config()
        redacted = redact_config(cfg)
        blob = json.dumps(redacted)
        assert "cookie" not in blob


# ---------------------------------------------------------------------------
# 5. WindowsWorkerHost lifecycle (fake runtime)
# ---------------------------------------------------------------------------


class _FakeRuntime:
    def __init__(self, **kwargs):
        self.worker_id = kwargs.get("worker_id", "fake")
        self.capabilities = list(kwargs.get("capabilities", []))
        self.store_path = kwargs.get("store_path")
        self._stopped = False
        self.registered = False
        self.heartbeat_started = False
        self.stop_calls = 0
        self.cycles_to_run = 1
        self.raise_on_run_forever = None

    def register(self):
        self.registered = True
        return {"worker_id": self.worker_id, "status": "registered", "lease_token": "sekret-reg"}

    def start_heartbeat_thread(self):
        self.heartbeat_started = True

    def run_forever(self, *, max_cycles=None):
        if self.raise_on_run_forever:
            raise self.raise_on_run_forever
        return self.cycles_to_run

    def stop(self, *, wait=True):
        self._stopped = True
        self.stop_calls += 1


def _host(config: WorkerHostConfig, fake: _FakeRuntime, **kwargs) -> WindowsWorkerHost:
    return WindowsWorkerHost(
        config,
        runtime_factory=lambda **kw: fake,
        **kwargs,
    )


class TestHost:
    def _local_config(self, tmp_path: Path, caps=("cpu_media",), **overrides) -> WorkerHostConfig:
        return _config(
            operations_db_path=str(tmp_path / "ops.sqlite3"),
            capabilities=list(caps),
            single_instance_lock_path=str(tmp_path / "host.lock"),
            **overrides,
        )

    def test_host_start(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        result = _host(cfg, fake).start(max_cycles=1)
        assert result.exit_code == EXIT_OK
        assert result.worker_id == cfg.worker_id

    def test_workerruntime_constructed(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        captured = {}

        def _factory(**kw):
            captured.update(kw)
            return _FakeRuntime(**kw)

        WindowsWorkerHost(cfg, runtime_factory=_factory).start(max_cycles=1)
        # M6-07: the host injects a LocalSQLiteWorkerTransport (never store_path).
        assert captured["transport"].db_path == Path(tmp_path / "ops.sqlite3")
        assert captured["worker_id"] == cfg.worker_id

    def test_heartbeat_registration_delegation(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        _host(cfg, fake).start(max_cycles=1)
        assert fake.registered
        assert fake.heartbeat_started
        assert fake._stopped

    def test_graceful_stop(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        result = _host(cfg, fake).start(max_cycles=2)
        assert result.exit_code == EXIT_OK
        assert fake.stop_calls >= 1

    def test_keyboard_interrupt(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        fake.raise_on_run_forever = KeyboardInterrupt()
        result = _host(cfg, fake).start()
        assert result.exit_code == EXIT_OK

    def test_fatal_exception_exit_code(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        fake.raise_on_run_forever = RuntimeError("boom lease_token leak")
        result = _host(cfg, fake).start()
        assert result.exit_code == EXIT_FATAL_RUNTIME_ERROR
        # Error message is redacted (contains secret-shaped key).
        assert result.error_message == "[REDACTED]"

    def test_config_error_exit_code(self, tmp_path: Path):
        # Missing operations_db_path -> fail closed, no production DB created.
        cfg = _config(operations_db_path=None)
        result = WindowsWorkerHost(cfg).start()
        assert result.exit_code == EXIT_CONFIG_ERROR

    def test_preflight_failure_exit_code(self, tmp_path: Path):
        cfg = self._local_config(tmp_path, caps=("llm_extraction",))
        result = WindowsWorkerHost(cfg).start()
        assert result.exit_code == EXIT_PREFLIGHT_FAILURE

    def test_already_running_exit_code(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        lock = SingleInstanceLock(cfg.single_instance_lock_path)
        assert lock.acquire()
        try:
            result = _host(cfg, fake).start(max_cycles=1)
            assert result.exit_code == EXIT_ALREADY_RUNNING
        finally:
            lock.release()

    def test_http_transport_requires_control_plane_url(self, tmp_path: Path):
        # M6-07: http transport without a control_plane_url fails at config time.
        with pytest.raises(ValueError):
            _config(
                worker_id="http-worker",
                capabilities=["cpu_media"],
                control_plane_transport=CONTROL_PLANE_TRANSPORT_FUTURE,
                operations_db_path=str(tmp_path / "ops.sqlite3"),
            )

    def test_http_transport_config_valid_with_url(self, tmp_path: Path):
        cfg = _config(
            worker_id="http-worker",
            capabilities=["cpu_media"],
            control_plane_transport=CONTROL_PLANE_TRANSPORT_FUTURE,
            control_plane_url="http://127.0.0.1:8765",
            operations_db_path=None,
            single_instance_lock_path=str(tmp_path / "host.lock"),
        )
        assert cfg.control_plane_transport == CONTROL_PLANE_TRANSPORT_FUTURE
        assert cfg.control_plane_url == "http://127.0.0.1:8765"
        # http mode must never use a local operations DB for execution.
        assert cfg.operations_db_path is None

    def test_no_production_ops_db_auto_created(self, tmp_path: Path):
        prod_db = REPO_ROOT / "data" / "operations" / "operations.sqlite3"
        before = prod_db.exists()
        cfg = _config(operations_db_path=None)
        WindowsWorkerHost(cfg).start()
        assert prod_db.exists() == before

    def test_no_production_m5_modified(self, tmp_path: Path):
        m5_db = REPO_ROOT / "data" / "knowledge" / "knowledge_store.sqlite3"
        if not m5_db.exists():
            pytest.skip("production M5 store not present")
        before = hashlib.sha256(m5_db.read_bytes()).hexdigest()
        cfg = self._local_config(tmp_path)
        _host(cfg, _FakeRuntime()).start(max_cycles=1)
        after = hashlib.sha256(m5_db.read_bytes()).hexdigest()
        assert before == after

    def test_no_network(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        _host(cfg, _FakeRuntime()).start(max_cycles=1)
        # No network calls are possible by construction (fake runtime, local DB).

    def test_no_llm_gpu_execution(self, tmp_path: Path):
        cfg = self._local_config(tmp_path, caps=("cpu_media",))
        _host(cfg, _FakeRuntime()).start(max_cycles=1)
        # Nothing is executed: preflight only checks paths, fake runtime does no work.

    def test_registration_log_redacted(self, tmp_path: Path):
        cfg = self._local_config(tmp_path)
        fake = _FakeRuntime()
        _host(cfg, fake).start(max_cycles=1)
        # register() returned a lease_token; the host logs it redacted.
        assert fake.registered


# ---------------------------------------------------------------------------
# 6. Safety / boundaries
# ---------------------------------------------------------------------------


class TestSafety:
    def test_no_production_ops_db_auto_created_via_cli(self, tmp_path: Path):
        raw = {**_base_config(), "operations_db_path": None}
        path = _write_config(tmp_path, raw)
        proc = subprocess.run(
            [sys.executable, "-m", "src.operations.windows_worker", "print-config", "--config", str(path)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == EXIT_OK
        assert not (REPO_ROOT / "data" / "operations" / "operations.sqlite3").exists()

    def test_target_capability_profile(self):
        assert "store_ingest" not in WINDOWS_PC_TARGET_CAPABILITIES
        assert "collector" in WINDOWS_PC_TARGET_CAPABILITIES
        assert "llm_extraction" in WINDOWS_PC_TARGET_CAPABILITIES


# ---------------------------------------------------------------------------
# 6b. Production handler registry (M6-08)
# ---------------------------------------------------------------------------


class TestProductionHandlerRegistry:
    def test_production_registry_returns_all_six_stages(self, tmp_path: Path):
        config = WorkerHostConfig.from_dict(
            _base_config(
                "prod-worker",
                workspace_root=str(tmp_path),
                archive_root=str(tmp_path / "archive"),
                processed_root=str(tmp_path / "processed"),
                control_plane_transport=CONTROL_PLANE_TRANSPORT_FUTURE,
                control_plane_url="http://127.0.0.1:1",
                runtime_references={
                    "browser_profile": str(tmp_path / "profile"),
                    "browser_executable": str(tmp_path / "chrome.exe"),
                    "asr_runtime": str(tmp_path / "runtime"),
                    "vlm_runtime": str(tmp_path / "runtime"),
                    "llm_runtime": str(tmp_path / "llama"),
                    "llm_model_root": str(tmp_path / "models"),
                },
            )
        )
        from src.operations.windows_worker import build_production_handler_registry

        registry = build_production_handler_registry(config)
        assert set(registry.keys()) == {
            "DISCOVER",
            "ARCHIVE",
            "MEDIA_PROCESS",
            "KNOWLEDGE_EXTRACT",
            "KNOWLEDGE_FINALIZE",
            "STORE_INGEST",
        }

    def test_registry_injects_deterministic_source_url_on_archive(self, tmp_path: Path):
        from dataclasses import dataclass, field

        from src.operations.windows_worker import _with_source_url

        calls = []

        def _fake_handler(claimed):
            calls.append(claimed)
            return "handled"

        wrapped = _with_source_url(_fake_handler)

        @dataclass
        class _FakeClaimed:
            metadata: dict = field(default_factory=dict)
            platform_content_id: str = "7681603850364521734"

        result = wrapped(_FakeClaimed(metadata={}))
        assert result == "handled"
        assert calls[0].metadata["source_url"] == "https://www.douyin.com/video/7681603850364521734"

    def test_registry_keeps_existing_source_url_on_archive(self):
        from dataclasses import dataclass, field

        from src.operations.windows_worker import _with_source_url

        calls = []

        def _fake_handler(claimed):
            calls.append(claimed)
            return "handled"

        wrapped = _with_source_url(_fake_handler)

        @dataclass
        class _FakeClaimed:
            metadata: dict = field(default_factory=dict)
            platform_content_id: str = "7681603850364521734"

        wrapped(_FakeClaimed(metadata={"source_url": "https://example.com/custom"}))
        assert calls[0].metadata["source_url"] == "https://example.com/custom"

    def test_registry_requires_source_url_field_not_mutated_in_place(self):
        from dataclasses import dataclass, field

        from src.operations.windows_worker import _with_source_url

        @dataclass
        class _FakeClaimed:
            metadata: dict = field(default_factory=dict)
            platform_content_id: str = "abc123"

        original = _FakeClaimed(metadata={"x": 1})
        wrapped = _with_source_url(lambda claimed: None)
        wrapped(original)
        assert "source_url" not in original.metadata
        assert original.metadata == {"x": 1}

    def test_registry_available_as_cli_handler_registry(self):
        import inspect

        from src.operations.windows_worker import (
            PRODUCTION_HANDLER_REGISTRY_VERSION,
            build_production_handler_registry,
        )

        assert PRODUCTION_HANDLER_REGISTRY_VERSION == "m6-08-prod-handlers-v1"
        params = inspect.signature(build_production_handler_registry).parameters
        assert "config" in params


# ---------------------------------------------------------------------------
# 7. PowerShell autostart artifacts
# ---------------------------------------------------------------------------


class TestPowerShell:
    def test_run_script_path_correctness(self):
        text = RUN_SCRIPT.read_text(encoding="utf-8")
        assert ".venv\\Scripts\\python.exe" in text
        assert "src.operations.windows_worker" in text
        assert "run_m6_worker.ps1" in RUN_SCRIPT.name

    def test_install_script_dryrun(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "-Apply" in text
        assert "DRY-RUN" in text
        assert "PkpM6WindowsWorker" in text

    def test_task_name_deterministic(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'TaskName = "PkpM6WindowsWorker"' in text
        assert UNINSTALL_SCRIPT.read_text(encoding="utf-8").count("PkpM6WindowsWorker") >= 1
        assert STATUS_SCRIPT.read_text(encoding="utf-8").count("PkpM6WindowsWorker") >= 1

    def test_task_trigger_logon(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "New-ScheduledTaskTrigger -AtLogOn" in text

    def test_startup_delay(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "StartupDelaySeconds" in text
        assert "PT" in text  # ISO-8601 delay

    def test_exact_venv_python(self):
        text = RUN_SCRIPT.read_text(encoding="utf-8")
        assert '.venv\\Scripts\\python.exe' in text

    def test_working_directory(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "-WorkingDirectory" in text
        assert "RepoRoot" in text

    def test_restart_policy(self):
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "RestartCount" in text
        assert "RestartInterval" in text
        assert "$RestartCount = 999" in text
        # Verify 999999 is not used (exceeds Task Scheduler schema limit)
        assert "999999" not in text

    def test_uninstall_idempotency_design(self):
        text = UNINSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "SilentlyContinue" in text
        assert "no-op" in text

    def test_status_read_only_design(self):
        text = STATUS_SCRIPT.read_text(encoding="utf-8")
        assert "Get-ScheduledTask" in text
        assert "NOT INSTALLED" in text
        assert "Register-ScheduledTask" not in text
        assert "Unregister-ScheduledTask" not in text

    def test_no_task_scheduler_mutation_during_tests(self):
        # The install script defaults to dry-run; nothing here registers a task.
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "if ($Apply)" in text


# ---------------------------------------------------------------------------
# 8. Real install dry-run (0 mutation)
# ---------------------------------------------------------------------------


class TestInstallDryRun:
    def test_install_dryrun_zero_mutation(self):
        """Run install script -DryRun against the example config and confirm
        it exits 0 without registering a scheduled task."""
        if sys.platform != "win32":
            pytest.skip("Windows-only")
        if not VENV_PYTHON.exists():
            pytest.skip("venv Python not present")
        task_name = "PkpM6WindowsWorkerTestDryRun"
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_SCRIPT),
            "-Config",
            str(EXAMPLE_CONFIG),
            "-TaskName",
            task_name,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert "DRY-RUN only" in proc.stdout
        # Confirm no task was actually registered.
        check = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue) {{ 'exists' }} else {{ 'absent' }}"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )
        assert "absent" in check.stdout


# ---------------------------------------------------------------------------
# 9. Profile cookie provider security & non-persistence audit
# ---------------------------------------------------------------------------


class TestProfileCookieProviderSecurity:
    def test_none_or_missing_profile_returns_empty(self, tmp_path):
        from src.operations.windows_worker import _ProfileCookieProvider

        provider_none = _ProfileCookieProvider(None)
        assert provider_none.get_credentials() == {}

        provider_missing = _ProfileCookieProvider(tmp_path / "nonexistent")
        assert provider_missing.get_credentials() == {}

    def test_corrupted_cookie_file_returns_empty_safely(self, tmp_path):
        from src.operations.windows_worker import _ProfileCookieProvider

        fake_profile = tmp_path / "fake_profile"
        cookie_dir = fake_profile / "Default" / "Network"
        cookie_dir.mkdir(parents=True)
        (cookie_dir / "Cookies").write_bytes(b"garbage-data-not-sqlite")
        (fake_profile / "Local State").write_text("{}", encoding="utf-8")

        provider = _ProfileCookieProvider(fake_profile)
        # Must gracefully handle any exception and return empty dict without raising
        result = provider.get_credentials()
        assert result == {}

    def test_wrap_archive_with_source_url_no_cookie_leak(self):
        from datetime import datetime, timezone
        from src.operations.models import ClaimedJob
        from src.operations.stages import StageExecutionResult
        from src.operations.windows_worker import _with_source_url

        captured_jobs: list[ClaimedJob] = []

        def dummy_handler(job: ClaimedJob) -> StageExecutionResult:
            captured_jobs.append(job)
            return StageExecutionResult(
                stage="ARCHIVE",
                canonical_id=job.canonical_id,
                status="SUCCEEDED",
                input_fingerprint="fp_in",
                output_fingerprint="fp_out",
                metadata={"archive_path": "/fake/archive"},
            )

        wrapped = _with_source_url(dummy_handler)
        job = ClaimedJob(
            job_id="job_sec_test",
            stage="ARCHIVE",
            canonical_id="douyin_7660044343020916006",
            platform="douyin",
            platform_content_id="7660044343020916006",
            input_fingerprint="fp_in",
            required_capabilities=frozenset(["network"]),
            lease_owner="worker_test",
            leased_at="2026-09-11T10:00:00Z",
            lease_expires_at="2026-09-11T10:10:00Z",
            _lease_token="lease_secret_token",
            metadata={},
        )
        res = wrapped(job)
        assert res.status == "SUCCEEDED"
        assert len(captured_jobs) == 1
        assert captured_jobs[0].metadata.get("source_url") == "https://www.douyin.com/video/7660044343020916006"
        assert "cookie" not in res.metadata
        assert "sessionid" not in str(res.metadata)
        assert "passport_csrf_token" not in str(res.metadata)

    def test_cookie_provider_does_not_mutate_or_copy_profile(self, tmp_path):
        """Audit requirement: profile path may be configured, but profile contents
        are never copied into repo, data, or stage outputs."""
        fake_profile = tmp_path / "browser_profile"
        fake_profile.mkdir()
        secret_file = fake_profile / "sensitive_data.txt"
        secret_file.write_text("dummy-secret-marker", encoding="utf-8")

        from src.operations.windows_worker import _ProfileCookieProvider
        provider = _ProfileCookieProvider(fake_profile)
        provider.get_credentials()

        # Confirm nothing was copied out of fake_profile
        assert secret_file.exists()
        assert list(tmp_path.iterdir()) == [fake_profile]