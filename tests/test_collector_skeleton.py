"""Unit and regression tests for Collector Subsystem skeleton (DY-C01)."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.collector.base import BaseCollector, CollectorMode, CollectorRunResult, CollectorStatus
from src.collector.cli import build_collector_parser, main as cli_main
from src.collector.config import CollectorConfig
from src.collector.douyin import DouyinCollector, DouyinCollectorConfig
from src.collector.errors import CollectorErrorCode, ConfigError, LockError
from src.collector.interfaces import (
    StubAuthStateDetector,
    StubBrowserRuntime,
    StubCanonicalTransformer,
    StubDownloadQueueProducer,
    StubMetadataRepository,
    StubRawArchiver,
    StubSourceClient,
)
from src.collector.lock import ServiceLock, is_pid_alive
from src.collector.logging import configure_collector_logging, redact_secrets
from src.collector.models import DownloadTask, RawArchiveBatch, Watermark
from src.collector.service import CollectorService


class FakeCollector(BaseCollector):
    """Fake platform collector for testing generic abstractions without Douyin specifics."""

    def __init__(self, config: CollectorConfig, auth_ok: bool = True) -> None:
        super().__init__(config)
        self.auth_ok = auth_ok
        self.initialized = False
        self.shutdown_called = False

    @property
    def platform(self) -> str:
        return "bilibili"

    def initialize(self, run_id: str) -> None:
        self.initialized = True

    def preflight_auth_check(self, run_id: str) -> bool:
        return self.auth_ok

    def probe(self, run_id: str) -> CollectorRunResult:
        return CollectorRunResult(
            run_id=run_id,
            platform=self.platform,
            mode=CollectorMode.PROBE,
            status=CollectorStatus.SUCCESS,
            metrics={"healthy": True},
        )

    def sync(self, run_id: str) -> CollectorRunResult:
        return CollectorRunResult(
            run_id=run_id,
            platform=self.platform,
            mode=CollectorMode.SYNC,
            status=CollectorStatus.SUCCESS,
            metrics={"items_synced": 5},
        )

    def backfill(self, run_id: str, limit: int | None = None) -> CollectorRunResult:
        return CollectorRunResult(
            run_id=run_id,
            platform=self.platform,
            mode=CollectorMode.BACKFILL,
            status=CollectorStatus.SUCCESS,
            metrics={"items_backfilled": limit or 10},
        )

    def shutdown(self, run_id: str) -> None:
        self.shutdown_called = True


class ExplodingCollector(BaseCollector):
    """Collector that raises an unexpected exception to test error boundaries."""

    @property
    def platform(self) -> str:
        return "exploding"

    def probe(self, run_id: str) -> CollectorRunResult:
        raise RuntimeError("Unexpected boom in probe")

    def sync(self, run_id: str) -> CollectorRunResult:
        raise KeyError("Missing critical internal mapping")

    def backfill(self, run_id: str, limit: int | None = None) -> CollectorRunResult:
        raise ValueError("Invalid limit")


# =====================================================================
# 1. Config Tests
# =====================================================================

def test_config_defaults(tmp_path: Path):
    config = CollectorConfig(runtime_root=tmp_path / "runtime")
    assert config.platform == "douyin"
    assert config.page_size == 10
    assert config.headless is True
    assert config.lock_timeout_sec == 3600
    assert config.lock_path == tmp_path / "runtime" / "douyin_collector.lock"


def test_config_from_dict_and_load(tmp_path: Path):
    cfg_data = {
        "platform": "douyin",
        "runtime_root": "./rt",
        "raw_archive_root": "./raw",
        "page_size": 20,
        "headless": False,
        "extra": {"aid": 6383},
    }
    cfg_file = tmp_path / "collector.json"
    cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

    config = DouyinCollectorConfig.load(cfg_file)
    assert config.platform == "douyin"
    assert config.page_size == 20
    assert config.headless is False
    assert config.aid == 6383
    assert config.runtime_root == (tmp_path / "rt").resolve()


def test_config_rejects_sensitive_secrets(tmp_path: Path):
    dirty_data = {
        "platform": "douyin",
        "sessionid": "secret123456",
    }
    cfg_file = tmp_path / "dirty.json"
    cfg_file.write_text(json.dumps(dirty_data), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        CollectorConfig.load(cfg_file)
    assert "Sensitive credential key" in str(exc_info.value)


# =====================================================================
# 2. CLI Help & Command Tests
# =====================================================================

def test_cli_help(capsys):
    parser = build_collector_parser()
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Unified Collection Ingestion CLI" in out
    assert "douyin" in out


def test_cli_probe_json_execution(tmp_path: Path, capsys):
    ret = cli_main(["douyin", "probe", "--json"])
    assert ret == 0
    captured = capsys.readouterr().out
    # Parse the JSON output line
    for line in captured.strip().split("\n"):
        if line.startswith("{"):
            data = json.loads(captured[captured.find("{"):])
            assert data["platform"] == "douyin"
            assert data["mode"] == "probe"
            assert data["status"] == "PARTIAL"
            assert "missing_dependencies" in data["metrics"]
            break


def test_cli_sync_not_implemented_returns_code_1(capsys):
    ret = cli_main(["douyin", "sync", "--json"])
    assert ret == 1
    captured = capsys.readouterr().out
    data = json.loads(captured[captured.find("{"):])
    assert data["status"] == "NOT_IMPLEMENTED"
    assert data["error"]["code"] == "DEPENDENCY_NOT_READY"


def test_cli_backfill_not_implemented_returns_code_1(capsys):
    ret = cli_main(["douyin", "backfill", "--json", "--limit", "10"])
    assert ret == 1
    captured = capsys.readouterr().out
    data = json.loads(captured[captured.find("{"):])
    assert data["status"] == "NOT_IMPLEMENTED"
    assert data["error"]["code"] == "DEPENDENCY_NOT_READY"


# =====================================================================
# 3. Lock & Concurrency Tests
# =====================================================================

def test_service_lock_acquisition_and_release(tmp_path: Path):
    lock_file = tmp_path / "test.lock"
    lock = ServiceLock(lock_file, platform="douyin")

    assert not lock_file.exists()
    lock.acquire("run_test1")
    assert lock_file.exists()
    assert lock.acquired is True

    # Check payload
    content = json.loads(lock_file.read_text(encoding="utf-8"))
    assert content["pid"] == os.getpid()
    assert content["run_id"] == "run_test1"

    # Second acquisition by another instance must fail
    second_lock = ServiceLock(lock_file, platform="douyin")
    with pytest.raises(LockError) as exc:
        second_lock.acquire("run_test2")
    assert "locked by running process" in str(exc.value)

    lock.release()
    assert not lock_file.exists()
    assert lock.acquired is False


def test_service_lock_stale_detection(tmp_path: Path):
    lock_file = tmp_path / "stale.lock"
    # Write a lock file with an absurdly high PID that definitely doesn't exist
    stale_payload = {
        "pid": 9999999,
        "started_at": "2026-01-01T00:00:00Z",
        "platform": "douyin",
        "run_id": "run_stale",
    }
    lock_file.write_text(json.dumps(stale_payload), encoding="utf-8")

    # New lock instance should detect dead PID, break stale lock, and acquire
    lock = ServiceLock(lock_file, platform="douyin")
    lock.acquire("run_recovered")
    assert lock.acquired is True

    new_content = json.loads(lock_file.read_text(encoding="utf-8"))
    assert new_content["pid"] == os.getpid()
    assert new_content["run_id"] == "run_recovered"

    lock.release()


# =====================================================================
# 4. Run ID Uniqueness Tests
# =====================================================================

def test_run_id_uniqueness(tmp_path: Path):
    config = CollectorConfig(runtime_root=tmp_path / "rt")
    collector = FakeCollector(config)
    service = CollectorService(collector, config)

    res1 = service.execute(CollectorMode.PROBE)
    res2 = service.execute(CollectorMode.PROBE)

    assert res1.run_id != res2.run_id
    assert res1.run_id.startswith("run_")
    assert res2.run_id.startswith("run_")


# =====================================================================
# 5. Service Lifecycle & Extensibility Tests
# =====================================================================

def test_fake_collector_full_lifecycle(tmp_path: Path):
    config = CollectorConfig(runtime_root=tmp_path / "rt")
    collector = FakeCollector(config)
    service = CollectorService(collector, config)

    # Sync
    res = service.execute(CollectorMode.SYNC)
    assert res.status == CollectorStatus.SUCCESS
    assert res.platform == "bilibili"
    assert res.metrics["items_synced"] == 5
    assert collector.initialized is True
    assert collector.shutdown_called is True

    # Backfill
    res_bf = service.execute(CollectorMode.BACKFILL, limit=50)
    assert res_bf.status == CollectorStatus.SUCCESS
    assert res_bf.metrics["items_backfilled"] == 50


def test_service_preflight_auth_failure(tmp_path: Path):
    config = CollectorConfig(runtime_root=tmp_path / "rt")
    collector = FakeCollector(config, auth_ok=False)
    service = CollectorService(collector, config)

    res = service.execute(CollectorMode.SYNC)
    assert res.status == CollectorStatus.FAILED
    assert res.error["code"] == CollectorErrorCode.AUTH_NOT_READY.value
    assert "Preflight authentication check failed" in res.error["message"]


def test_service_exception_boundary(tmp_path: Path):
    config = CollectorConfig(runtime_root=tmp_path / "rt")
    collector = ExplodingCollector(config)
    service = CollectorService(collector, config)

    # Probe explosion
    res_probe = service.execute(CollectorMode.PROBE)
    assert res_probe.status == CollectorStatus.FAILED
    assert res_probe.error["code"] == CollectorErrorCode.UNKNOWN.value
    assert "Unexpected boom" in res_probe.error["message"]

    # Sync explosion
    res_sync = service.execute(CollectorMode.SYNC)
    assert res_sync.status == CollectorStatus.FAILED
    assert res_sync.error["code"] == CollectorErrorCode.UNKNOWN.value
    assert "Missing critical" in res_sync.error["message"]


# =====================================================================
# 6. Douyin Collector & Stubs Integration Tests
# =====================================================================

def test_douyin_collector_with_stubs(tmp_path: Path):
    config = DouyinCollectorConfig(runtime_root=tmp_path / "rt")
    collector = DouyinCollector(
        config=config,
        browser_runtime=StubBrowserRuntime(),
        auth_detector=StubAuthStateDetector("LOGIN_OK"),
        source_client=StubSourceClient(),
        raw_archiver=StubRawArchiver(tmp_path / "raw"),
        transformer=StubCanonicalTransformer(),
        repository=StubMetadataRepository(),
        queue_producer=StubDownloadQueueProducer(),
    )
    service = CollectorService(collector, config)

    res_probe = service.execute(CollectorMode.PROBE)
    assert res_probe.status == CollectorStatus.SUCCESS
    assert res_probe.metrics["browser_runtime_ready"] is True
    assert len(res_probe.metrics["missing_dependencies"]) == 0

    res_sync = service.execute(CollectorMode.SYNC)
    assert res_sync.status == CollectorStatus.SUCCESS


def test_douyin_collector_initialize_with_profile_path_no_runtime(tmp_path: Path):
    """Regression: initialize() must not crash when profile_path is set but no
    browser_runtime is injected (production branch previously raised
    NameError: name 'Path' is not defined in collector.py)."""
    config = DouyinCollectorConfig(
        runtime_root=tmp_path / "rt",
        profile_path=str(tmp_path / "chrome-profile"),
    )
    collector = DouyinCollector(config=config)
    collector.initialize("run_regression_profile_path")
    assert collector.browser_runtime is None


def test_douyin_collector_initialize_wires_runtime_when_profile_exists(tmp_path: Path):
    """initialize() wires DouyinBrowserRuntimeProvider when profile_path exists
    and no runtime is injected, without spawning a real browser."""
    profile = tmp_path / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    config = DouyinCollectorConfig(
        runtime_root=tmp_path / "rt",
        profile_path=str(profile),
    )
    collector = DouyinCollector(config=config)
    with patch(
        "src.collector.douyin.browser_runtime.DouyinBrowserRuntimeProvider"
    ) as fake_runtime_cls:
        fake_runtime_cls.return_value.is_running.return_value = False
        fake_runtime_cls.return_value.launch.return_value = None
        collector.initialize("run_regression_profile_launch")
    assert collector.browser_runtime is not None
    fake_runtime_cls.return_value.launch.assert_called_once()


# =====================================================================
# 7. Secret Logging Regression Tests
# =====================================================================

def test_redact_secrets_pattern():
    raw_text = (
        "Request failed for url https://douyin.com/detail?msToken=ABCDEFGHIJKL1234567890&a_bogus=1234567890 "
        "with Cookie: sessionid=deadbeef123456; sid_guard=secret98765; passport_csrf_token=pass123; Bearer tokenXYZ"
    )
    redacted = redact_secrets(raw_text)
    assert "deadbeef123456" not in redacted
    assert "secret98765" not in redacted
    assert "ABCDEFGHIJKL1234567890" not in redacted
    assert "tokenXYZ" not in redacted
    assert "sessionid=[REDACTED]" in redacted
    assert "sid_guard=[REDACTED]" in redacted
    assert "msToken=[REDACTED]" in redacted
    assert "Bearer [REDACTED]" in redacted


def test_logger_redaction_handler(tmp_path: Path):
    logger = configure_collector_logging("test_run", log_dir=tmp_path)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    from src.collector.logging import RedactingFormatter
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger.addHandler(handler)

    logger.info("Connection failed: sessionid=my_secret_token_12345")
    output = stream.getvalue()
    assert "my_secret_token_12345" not in output
    assert "sessionid=[REDACTED]" in output
