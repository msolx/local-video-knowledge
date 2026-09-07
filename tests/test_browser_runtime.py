"""Tests for Douyin Persistent Browser Runtime Provider (DY-C02).

Validates:
- Protocol compliance (BrowserRuntimeProvider)
- ProfileRuntimeLock atomic acquisition, conflict detection, and stale PID reclamation
- Hardening 1: Prohibit automatic creation of empty profile (DependencyNotReadyError)
- Hardening 2: Live owner with expired lock age strictly refuses reclaim
- Hardening 3: Allowlisted RPC methods and rejection of non-allowlisted methods
- Process lifecycle (launch, health, info, restart, graceful shutdown)
- In-page evaluation and navigation via BrowserSession request()
- No credential transfer through IPC/argv
- 3x restart stability & crash recovery
- DouyinCollector slot integration
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

from src.collector.base import CollectorStatus
from src.collector.douyin import (
    BrowserSession,
    DouyinBrowserRuntimeProvider,
    DouyinCollector,
    DouyinCollectorConfig,
    ProfileRuntimeLock,
)
from src.collector.errors import BrowserRuntimeError, DependencyNotReadyError, LockError
from src.collector.interfaces import BrowserRuntimeProvider


@pytest.fixture
def temp_profile_dir() -> Generator[Path, None, None]:
    """Provide an isolated existing temporary directory for profile testing."""
    td = Path(tempfile.mkdtemp(prefix="test_c02_profile_"))
    try:
        yield td
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# Unit Tests: Protocol & Lock Mechanics
# ---------------------------------------------------------------------------

def test_protocol_compliance() -> None:
    """Verify DouyinBrowserRuntimeProvider satisfies BrowserRuntimeProvider protocol."""
    provider = DouyinBrowserRuntimeProvider(headless=True)
    assert isinstance(provider, BrowserRuntimeProvider)
    assert hasattr(provider, "is_running")
    assert hasattr(provider, "launch")
    assert hasattr(provider, "close")
    assert hasattr(provider, "health")
    assert hasattr(provider, "info")
    assert hasattr(provider, "restart")
    assert hasattr(provider, "get_session")


def test_missing_profile_returns_dependency_not_ready(tmp_path: Path) -> None:
    """Hardening 1: Verify missing profile directory raises DependencyNotReadyError."""
    missing_dir = tmp_path / "non_existent_profile_dir"
    assert not missing_dir.exists()

    provider = DouyinBrowserRuntimeProvider(profile_path=missing_dir, headless=True)
    with pytest.raises(DependencyNotReadyError) as exc_info:
        provider.launch()

    assert "does not exist" in str(exc_info.value)
    assert not missing_dir.exists(), "Must NOT automatically create missing profile directory!"


def test_profile_runtime_lock_acquire_and_release(temp_profile_dir: Path) -> None:
    """Verify normal lock acquisition and release."""
    lock = ProfileRuntimeLock(temp_profile_dir)
    assert not lock.is_acquired

    lock.acquire()
    assert lock.is_acquired
    assert (temp_profile_dir / "profile.lock").exists()

    with open(temp_profile_dir / "profile.lock", "r", encoding="utf-8") as f:
        meta = json.load(f)
        assert meta["pid"] == os.getpid()
        assert meta["profile_path"] == str(temp_profile_dir.resolve())
        assert "acquired_at" in meta

    lock.release()
    assert not lock.is_acquired
    assert not (temp_profile_dir / "profile.lock").exists()


def test_profile_runtime_lock_conflict(temp_profile_dir: Path) -> None:
    """Verify that acquiring an already locked profile by an active process raises LockError."""
    lock1 = ProfileRuntimeLock(temp_profile_dir)
    lock1.acquire()

    lock2 = ProfileRuntimeLock(temp_profile_dir)
    with pytest.raises(LockError) as exc_info:
        lock2.acquire()

    assert "already locked" in str(exc_info.value)
    lock1.release()


def test_profile_lock_live_owner_with_expired_age_refuses_reclaim(temp_profile_dir: Path) -> None:
    """Hardening 2: Live owner + lock age > timeout MUST NOT RECLAIM."""
    lock_file = temp_profile_dir / "profile.lock"
    # Live owner (current process), but acquired 10,000 seconds ago
    live_expired_meta = {
        "pid": os.getpid(),
        "sidecar_pid": None,
        "browser_pid": None,
        "profile_path": str(temp_profile_dir.resolve()),
        "acquired_at": "2020-01-01T00:00:00+00:00",
        "hostname": "test-host",
    }
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(live_expired_meta, f)

    competitor_lock = ProfileRuntimeLock(temp_profile_dir, timeout_sec=60)
    with pytest.raises(LockError) as exc_info:
        competitor_lock.acquire()

    assert "already locked by active process" in str(exc_info.value)
    # Ensure lock was NOT overwritten
    with open(lock_file, "r", encoding="utf-8") as f:
        current_meta = json.load(f)
        assert current_meta["acquired_at"] == "2020-01-01T00:00:00+00:00"


def test_profile_runtime_lock_stale_pid_reclamation(temp_profile_dir: Path) -> None:
    """Hardening 2: Dead owner + stale lock -> RECLAIM PASS."""
    lock_file = temp_profile_dir / "profile.lock"
    fake_meta = {
        "pid": 999999,  # Non-existent PID
        "sidecar_pid": None,
        "browser_pid": None,
        "profile_path": str(temp_profile_dir.resolve()),
        "acquired_at": "2026-09-01T00:00:00+00:00",
        "hostname": "fake-host",
    }
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(fake_meta, f)

    new_lock = ProfileRuntimeLock(temp_profile_dir)
    new_lock.acquire()

    assert new_lock.is_acquired
    with open(lock_file, "r", encoding="utf-8") as f:
        reclaimed_meta = json.load(f)
        assert reclaimed_meta["pid"] == os.getpid()

    new_lock.release()


def test_profile_runtime_lock_update_pids(temp_profile_dir: Path) -> None:
    """Verify that update_pids updates metadata on disk."""
    lock = ProfileRuntimeLock(temp_profile_dir)
    lock.acquire()
    lock.update_pids(sidecar_pid=1111, browser_pid=2222)

    with open(temp_profile_dir / "profile.lock", "r", encoding="utf-8") as f:
        meta = json.load(f)
        assert meta["sidecar_pid"] == 1111
        assert meta["browser_pid"] == 2222

    lock.release()


def test_runtime_provider_from_config(temp_profile_dir: Path) -> None:
    """Verify provider factory method creates instance from DouyinCollectorConfig."""
    cfg = DouyinCollectorConfig(profile_path=temp_profile_dir, headless=True)
    provider = DouyinBrowserRuntimeProvider.from_config(cfg)
    assert provider.profile_path == temp_profile_dir.resolve()
    assert provider.headless is True


def test_zero_credential_transfer_verification() -> None:
    """Verify that no credentials or cookies exist in default configs or sidecar scripts."""
    sidecar_path = Path(__file__).parents[1] / "src" / "collector" / "douyin" / "browser" / "runtime_server.js"
    assert sidecar_path.exists()
    content = sidecar_path.read_text(encoding="utf-8")

    assert "sessionid_ss=" not in content
    assert "passport_csrf_token=" not in content
    assert "msToken=" not in content


# ---------------------------------------------------------------------------
# Integration Tests: Subprocess Lifecycle & Browser Operations
# ---------------------------------------------------------------------------

def test_browser_runtime_lifecycle_headless(temp_profile_dir: Path) -> None:
    """Full lifecycle test in headless mode."""
    provider = DouyinBrowserRuntimeProvider(profile_path=temp_profile_dir, headless=True)
    assert not provider.is_running()

    try:
        provider.launch()
        assert provider.is_running()

        health = provider.health()
        assert health.get("healthy") is True
        assert health.get("connected") is True
        assert health.get("running") is True
        assert health.get("browser_pid") is not None
        assert health.get("node_pid") is not None

        info = provider.info()
        assert info.get("running") is True
        assert info.get("headless") is True
        assert "Chrome" in str(info.get("version"))

        assert (temp_profile_dir / "profile.lock").exists()

    finally:
        provider.close()
        assert not provider.is_running()
        assert not (temp_profile_dir / "profile.lock").exists()


def test_browser_session_allowlisted_request_and_navigation(temp_profile_dir: Path) -> None:
    """Hardening 3: Test BrowserSession request() and allowlisted navigation."""
    with DouyinBrowserRuntimeProvider(profile_path=temp_profile_dir, headless=True) as provider:
        session = provider.get_session()
        assert isinstance(session, BrowserSession)
        assert session.is_alive()

        # 1. Allowlisted ping RPC
        ping_res = session.request("runtime.ping")
        assert ping_res["status"] == "pong"
        assert "timestamp" in ping_res

        # 2. Navigation to a data HTML document
        data_url = "data:text/html,<html><head><title>DY C02 Test Page</title></head><body><h1>Runtime Active</h1></body></html>"
        nav_res = session.navigate(data_url)
        assert nav_res["title"] == "DY C02 Test Page"
        assert "data:text/html" in nav_res["url"]

        content = session.get_content()
        assert content["title"] == "DY C02 Test Page"

        # 3. Internal evaluate check
        res = session._evaluate("10 + 20")
        assert res == 30


def test_browser_runtime_rejects_arbitrary_method(temp_profile_dir: Path) -> None:
    """Hardening 3: Verify that sending non-allowlisted command raises PROTOCOL_METHOD_NOT_ALLOWED."""
    with DouyinBrowserRuntimeProvider(profile_path=temp_profile_dir, headless=True) as provider:
        with pytest.raises(BrowserRuntimeError) as exc_info:
            provider.send_command("unauthorized_arbitrary_code_injection")
        assert "not permitted by sidecar allowlist" in str(exc_info.value)


def test_browser_runtime_3x_restart_cycle(temp_profile_dir: Path) -> None:
    """Verify 3 consecutive restart cycles without deadlocks or lingering locks."""
    provider = DouyinBrowserRuntimeProvider(profile_path=temp_profile_dir, headless=True)

    for cycle in range(3):
        provider.launch()
        assert provider.is_running(), f"Cycle {cycle+1} launch failed"
        health = provider.health()
        assert health.get("healthy") is True, f"Cycle {cycle+1} health check failed"

        provider.close()
        assert not provider.is_running(), f"Cycle {cycle+1} close failed"
        assert not (temp_profile_dir / "profile.lock").exists(), f"Cycle {cycle+1} lock not released"


def test_sidecar_crash_detection_and_recovery(temp_profile_dir: Path) -> None:
    """Verify behavior when sidecar process crashes unexpectedly."""
    provider = DouyinBrowserRuntimeProvider(profile_path=temp_profile_dir, headless=True)
    provider.launch()
    assert provider.is_running()

    # Force kill sidecar process
    assert provider._proc is not None
    provider._proc.kill()
    time.sleep(0.3)

    assert not provider.is_running()

    provider.close()
    provider.launch()
    assert provider.is_running()
    assert provider.health().get("healthy") is True

    provider.close()


def test_douyin_collector_slot_resolution_with_runtime(temp_profile_dir: Path) -> None:
    """Verify DouyinCollector slot architecture integrates C02 runtime."""
    cfg = DouyinCollectorConfig(profile_path=temp_profile_dir, headless=True)
    provider = DouyinBrowserRuntimeProvider.from_config(cfg)
    collector = DouyinCollector(config=cfg, browser_runtime=provider)

    probe_result = collector.probe(run_id="test_run_slot")
    assert probe_result.status == CollectorStatus.PARTIAL
    assert probe_result.metrics.get("browser_runtime_ready") is True
    missing = probe_result.metrics.get("missing_dependencies", [])
    assert not any("C02" in item for item in missing)
    assert any("C03" in item for item in missing)
