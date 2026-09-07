"""Persistent Browser Runtime Provider for Douyin Collection Ingestion.

Implements BrowserRuntimeProvider Protocol (C02 slot) via a dedicated Node.js sidecar
managing puppeteer-core and a persistent Chromium profile (userDataDir).
Includes:
- ProfileRuntimeLock (profile.lock) with PID staleness check
- Subprocess lifecycle management (spawn, health, info, evaluate, navigate, shutdown)
- Thread-safe NDJSON IPC over stdio
- Headful / Headless execution support
- No Credential Transfer Through Collector IPC/argv guarantee
"""

from __future__ import annotations

import atexit
import ctypes
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import utcnow_iso
from ..errors import BrowserRuntimeError, DependencyNotReadyError, LockError
from ..interfaces import BrowserRuntimeProvider
from .config import DouyinCollectorConfig

logger = logging.getLogger("collector.douyin.browser")

DEFAULT_CHROME_WINDOWS = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_PROFILE_PATH = Path(r"G:\antigravity-cli\dy\runtime\chrome-profile")
SIDECAR_SCRIPT_PATH = Path(__file__).parent / "browser" / "runtime_server.js"


def _is_pid_alive(pid: int | None) -> bool:
    """Check if an OS process with the given PID is currently active."""
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _terminate_pid(pid: int | None, timeout_sec: float = 3.0) -> None:
    """Safely terminate a specific process ID without collateral damage."""
    if not pid or not _is_pid_alive(pid):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_sec,
                check=False,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(0.2)
            if _is_pid_alive(pid):
                os.kill(pid, 9)  # SIGKILL
    except Exception as exc:
        logger.debug(f"Failed to terminate PID {pid}: {exc}")


# ---------------------------------------------------------------------------
# Profile Runtime Lock
# ---------------------------------------------------------------------------

class ProfileRuntimeLock:
    """Exclusive filesystem lock for a dedicated Chromium profile directory.
    
    Prevents multiple simultaneous browser instances from accessing the same
    userDataDir and causing SQLite/SingletonLock corruption.
    """

    def __init__(self, profile_path: Path, timeout_sec: int = 3600) -> None:
        self.profile_path = profile_path.resolve()
        self.lock_path = self.profile_path / "profile.lock"
        self.timeout_sec = timeout_sec
        self._acquired = False
        self.sidecar_pid: int | None = None
        self.browser_pid: int | None = None

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> None:
        """Atomically acquire the profile lock with stale detection."""
        # Hardening 1: Prohibit auto-creating empty profile
        if not self.profile_path.exists():
            raise DependencyNotReadyError(
                f"Cannot acquire profile lock: profile directory '{self.profile_path}' does not exist. "
                "Automatic creation of an empty profile directory is prohibited.",
                details={"profile_path": str(self.profile_path), "code": "PROFILE_NOT_INITIALIZED"},
            )

        my_pid = os.getpid()

        meta = {
            "pid": my_pid,
            "sidecar_pid": self.sidecar_pid,
            "browser_pid": self.browser_pid,
            "profile_path": str(self.profile_path),
            "acquired_at": utcnow_iso(),
            "hostname": platform.node(),
        }

        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            self._acquired = True
            logger.debug(f"Acquired profile lock at {self.lock_path} (PID={my_pid})")
            return
        except FileExistsError:
            pass

        # Inspect existing lock
        existing_meta = self._read_lock()
        existing_pid = existing_meta.get("pid")
        existing_sidecar = existing_meta.get("sidecar_pid")
        existing_browser = existing_meta.get("browser_pid")

        # Check process liveness
        owning_alive = (
            _is_pid_alive(existing_pid)
            or _is_pid_alive(existing_sidecar)
            or _is_pid_alive(existing_browser)
        )

        # Hardening 2: Lock age > timeout can NEVER reclaim a live owner!
        if owning_alive:
            acquired_str = existing_meta.get("acquired_at", "")
            try:
                dt = datetime.fromisoformat(acquired_str)
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                if age > self.timeout_sec:
                    logger.warning(
                        f"Profile lock at {self.lock_path} age ({age:.1f}s) exceeds timeout ({self.timeout_sec}s), "
                        f"but owning process is still ALIVE (PID={existing_pid}). Lock takeover refused."
                    )
            except Exception:
                pass

            raise LockError(
                f"Chromium profile directory '{self.profile_path}' is already locked by active process "
                f"(PID {existing_pid}, browser PID {existing_browser}). Only one browser instance may access this profile.",
                details={"lock_path": str(self.lock_path), "existing_lock": existing_meta},
            )

        # Here, owning_alive is False (all recorded owners are DEAD)
        logger.warning(
            f"Detected stale profile lock at {self.lock_path}: owning PIDs "
            f"(python={existing_pid}, sidecar={existing_sidecar}, browser={existing_browser}) are dead. Reclaiming."
        )

        # Reclaim stale lock
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError as exc:
            raise LockError(f"Failed to remove stale profile lock: {exc}") from exc

        # Retry atomic acquisition once
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            self._acquired = True
            logger.info(f"Successfully reclaimed profile lock at {self.lock_path} (PID={my_pid})")
        except FileExistsError as exc:
            raise LockError(f"Contention while acquiring profile lock at {self.lock_path}") from exc

    def update_pids(self, sidecar_pid: int | None, browser_pid: int | None) -> None:
        """Update lock metadata with active child PIDs."""
        self.sidecar_pid = sidecar_pid
        self.browser_pid = browser_pid
        if not self._acquired or not self.lock_path.exists():
            return
        try:
            meta = self._read_lock()
            meta["sidecar_pid"] = sidecar_pid
            meta["browser_pid"] = browser_pid
            with open(self.lock_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to update profile lock PIDs: {exc}")

    def release(self) -> None:
        """Release profile lock."""
        if not self._acquired:
            return
        try:
            if self.lock_path.exists():
                meta = self._read_lock()
                if meta.get("pid") == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
                    logger.debug(f"Released profile lock at {self.lock_path}")
        except Exception as exc:
            logger.warning(f"Error releasing profile lock at {self.lock_path}: {exc}")
        finally:
            self._acquired = False

    def _read_lock(self) -> dict[str, Any]:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Browser Session (Runtime Handle)
# ---------------------------------------------------------------------------

class BrowserSession:
    """Active session wrapper providing allowlisted RPC operations to clients."""

    def __init__(self, provider: DouyinBrowserRuntimeProvider) -> None:
        self._provider = provider

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Execute an allowlisted RPC method on the sidecar."""
        return self._provider.send_command(method, params or {}, timeout=timeout)

    def navigate(self, url: str, timeout: float = 30.0, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        """Navigate active page to URL."""
        return self.request(
            "page.navigate",
            {"url": url, "timeout_ms": int(timeout * 1000), "wait_until": wait_until},
            timeout=timeout + 5.0,
        )

    def get_content(self) -> dict[str, str]:
        """Get page title and url."""
        res = self.request("page.info")
        return {"title": res.get("title", ""), "url": res.get("url", "")}

    def screenshot(self, path: str | Path | None = None, encoding: str = "base64") -> dict[str, Any]:
        """Capture page screenshot."""
        params: dict[str, Any] = {"encoding": encoding}
        if path:
            params["path"] = str(path)
        return self.request("page.screenshot", params)

    def _evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        """Internal / debug-only evaluation of JS expression in active page context."""
        res = self._provider.send_command("_debug.evaluate", {"expression": expression}, timeout=timeout)
        return res.get("result")

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        """Debug-only evaluation. Production features must use allowlisted request()."""
        return self._evaluate(expression, timeout=timeout)

    def is_alive(self) -> bool:
        return self._provider.is_running()

    def capture_credential_snapshot(self, timeout: float = 30.0) -> dict[str, Any]:
        """Allowlisted credential snapshot acquisition (D02)."""
        return self.request("douyin.credentials.snapshot", {}, timeout=timeout)


# ---------------------------------------------------------------------------
# Douyin Browser Runtime Provider (C02 Slot)
# ---------------------------------------------------------------------------

class DouyinBrowserRuntimeProvider:
    """Manages dedicated Chromium persistent browser session via Node.js sidecar.
    
    Implements BrowserRuntimeProvider Protocol.
    """

    def __init__(
        self,
        profile_path: Path | str | None = None,
        headless: bool = False,
        executable_path: Path | str | None = None,
        lock_timeout_sec: int = 3600,
        sidecar_script: Path | str | None = None,
        node_executable: str = "node",
    ) -> None:
        self.profile_path = Path(profile_path or DEFAULT_PROFILE_PATH).resolve()
        self.headless = headless
        self.executable_path = Path(executable_path or DEFAULT_CHROME_WINDOWS).resolve()
        self.lock_timeout_sec = lock_timeout_sec
        self.sidecar_script = Path(sidecar_script or SIDECAR_SCRIPT_PATH).resolve()
        self.node_executable = node_executable

        self.lock = ProfileRuntimeLock(self.profile_path, timeout_sec=lock_timeout_sec)
        self._proc: subprocess.Popen[str] | None = None
        self._sidecar_pid: int | None = None
        self._browser_pid: int | None = None
        self._is_running = False

        self._pending_requests: dict[str, tuple[threading.Event, list[Any]]] = {}
        self._lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        atexit.register(self._atexit_cleanup)

    @classmethod
    def from_config(cls, config: DouyinCollectorConfig) -> DouyinBrowserRuntimeProvider:
        """Factory method to construct provider from DouyinCollectorConfig."""
        profile = config.profile_path or DEFAULT_PROFILE_PATH
        return cls(
            profile_path=profile,
            headless=config.headless,
            lock_timeout_sec=config.lock_timeout_sec,
        )

    # -----------------------------------------------------------------------
    # BrowserRuntimeProvider Protocol methods
    # -----------------------------------------------------------------------

    def is_running(self) -> bool:
        """Check if both the sidecar process and Chromium are active."""
        if not self._is_running or not self._proc:
            return False
        if self._proc.poll() is not None:
            self._is_running = False
            return False
        return True

    def launch(self) -> None:
        """Launch Node sidecar and dedicated persistent browser instance."""
        if self.is_running():
            logger.info("Browser runtime is already running.")
            return

        # Hardening 1: Prohibit automatic creation of empty profile
        if not self.profile_path.exists():
            raise DependencyNotReadyError(
                f"Chromium profile directory '{self.profile_path}' does not exist. "
                "Automatic creation of an empty profile directory is prohibited. A valid persistent profile must be bootstrapped.",
                details={"profile_path": str(self.profile_path), "code": "PROFILE_NOT_INITIALIZED"},
            )

        logger.info(
            f"Launching DouyinBrowserRuntime: profile={self.profile_path}, "
            f"headless={self.headless}, executable={self.executable_path}"
        )

        # 1. Acquire Profile Lock
        self.lock.acquire()

        try:
            # 2. Spawn Sidecar Process
            if not self.sidecar_script.exists():
                raise BrowserRuntimeError(
                    f"Sidecar script not found at {self.sidecar_script}",
                    details={"sidecar_path": str(self.sidecar_script)},
                )

            # Resolve NODE_PATH candidates
            env = os.environ.copy()
            extra_node_paths = [
                r"G:\opencode_project\opencode-user\AppData\Local\Temp\opencode\dy\node_modules",
                r"G:\antigravity-cli\node_modules",
                str(Path(__file__).parents[3] / "node_modules"),
            ]
            current_np = env.get("NODE_PATH", "")
            all_np = [p for p in extra_node_paths if Path(p).exists()]
            if current_np:
                all_np.append(current_np)
            env["NODE_PATH"] = os.pathsep.join(all_np)

            creationflags = 0
            if sys.platform == "win32" and self.headless:
                creationflags = subprocess.CREATE_NO_WINDOW

            self._proc = subprocess.Popen(
                [self.node_executable, str(self.sidecar_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
            self._sidecar_pid = self._proc.pid
            self._is_running = True

            # 3. Start I/O listener threads
            self._stdout_thread = threading.Thread(
                target=self._read_stdout_loop, daemon=True, name="SidecarStdoutReader"
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop, daemon=True, name="SidecarStderrReader"
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

            # 4. Issue launch command to sidecar
            launch_params = {
                "profile_path": str(self.profile_path),
                "headless": self.headless,
                "executable_path": str(self.executable_path),
            }
            res = self.send_command("runtime.launch", launch_params, timeout=30.0)

            self._browser_pid = res.get("browser_pid")
            logger.info(
                f"Browser runtime launched successfully (sidecar PID={self._sidecar_pid}, "
                f"browser PID={self._browser_pid})"
            )

            # 5. Update lock metadata with running child PIDs
            self.lock.update_pids(self._sidecar_pid, self._browser_pid)

        except Exception as exc:
            logger.error(f"Failed to launch browser runtime: {exc}")
            self.close()
            raise

    def close(self) -> None:
        """Gracefully close browser, sidecar process, and release lock."""
        logger.info("Closing DouyinBrowserRuntime...")
        self._is_running = False

        # Attempt graceful close command via IPC
        if self._proc and self._proc.poll() is None:
            try:
                self.send_command("runtime.close", timeout=5.0)
            except Exception as e:
                logger.debug(f"IPC close command encountered error (continuing teardown): {e}")

            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.warning(f"Sidecar PID {self._sidecar_pid} did not exit within timeout. Terminating.")
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()

        # Specifically ensure dedicated browser PID is cleaned up if lingering
        if self._browser_pid and _is_pid_alive(self._browser_pid):
            logger.warning(f"Dedicated browser PID {self._browser_pid} still active after sidecar exit. Terminating.")
            _terminate_pid(self._browser_pid)

        # Release profile lock
        self.lock.release()

        self._proc = None
        self._sidecar_pid = None
        self._browser_pid = None
        logger.info("DouyinBrowserRuntime closed successfully.")

    # -----------------------------------------------------------------------
    # Extended Management & IPC API
    # -----------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Perform live health inspection of the runtime."""
        if not self.is_running():
            return {
                "healthy": False,
                "connected": False,
                "running": False,
                "error": "Browser runtime is not running",
            }
        try:
            res = self.send_command("runtime.health", timeout=10.0)
            res["running"] = True
            return res
        except Exception as exc:
            return {
                "healthy": False,
                "connected": False,
                "running": True,
                "error": str(exc),
            }

    def info(self) -> dict[str, Any]:
        """Get runtime environment and process metadata."""
        if not self.is_running():
            return {
                "running": False,
                "profile_path": str(self.profile_path),
                "headless": self.headless,
            }
        res = self.send_command("runtime.info", timeout=5.0)
        res["running"] = True
        return res

    def restart(self) -> None:
        """Restart browser runtime with clean teardown and re-launch."""
        logger.info("Restarting browser runtime...")
        self.close()
        time.sleep(0.5)
        self.launch()

    def get_session(self) -> BrowserSession:
        """Get an active BrowserSession handle for page interaction."""
        if not self.is_running():
            raise BrowserRuntimeError("Cannot obtain session: browser runtime is not running.")
        return BrowserSession(self)

    def capture_credential_snapshot(self, timeout: float = 30.0) -> dict[str, Any]:
        """Allowlisted credential snapshot acquisition (D02)."""
        return self.send_command("douyin.credentials.snapshot", {}, timeout=timeout)

    def send_command(self, action: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Send an allowlisted command to sidecar and synchronously wait for JSON response."""
        if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
            raise BrowserRuntimeError(f"Cannot send command '{action}': sidecar process is dead.")

        req_id = f"req_{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        result_holder: list[Any] = [None, None]

        with self._lock:
            self._pending_requests[req_id] = (event, result_holder)

        msg = {"id": req_id, "method": action, "params": params or {}}
        try:
            payload = json.dumps(msg) + "\n"
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except OSError as exc:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            raise BrowserRuntimeError(f"Failed to write command '{action}' to sidecar stdin: {exc}") from exc

        # Wait for response
        signaled = event.wait(timeout=timeout)
        with self._lock:
            self._pending_requests.pop(req_id, None)

        if not signaled:
            raise BrowserRuntimeError(
                f"Command '{action}' (ID: {req_id}) timed out after {timeout:.1f}s."
            )

        success, data = result_holder[0], result_holder[1]
        if not success:
            err_dict = data if isinstance(data, dict) else {"message": str(data)}
            raise BrowserRuntimeError(
                f"Browser sidecar error on action '{action}': {err_dict.get('message', 'Unknown error')}",
                details=err_dict,
            )

        return data or {}

    # -----------------------------------------------------------------------
    # Background I/O threads
    # -----------------------------------------------------------------------

    def _read_stdout_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                resp = json.loads(line_str)
                req_id = resp.get("id")
                with self._lock:
                    pending = self._pending_requests.get(req_id)
                if pending:
                    event, holder = pending
                    holder[0] = resp.get("success", False)
                    holder[1] = resp.get("data") if resp.get("success") else resp.get("error")
                    event.set()
                else:
                    logger.debug(f"Unmatched sidecar response: {line_str}")
            except json.JSONDecodeError:
                logger.warning(f"Corrupted sidecar stdout line: {line_str}")

        # EOF reached
        self._is_running = False
        with self._lock:
            for event, holder in self._pending_requests.values():
                holder[0] = False
                holder[1] = {"code": "EOF", "message": "Sidecar stdout closed unexpectedly"}
                event.set()
            self._pending_requests.clear()

    def _read_stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            msg = line.strip()
            if msg:
                logger.debug(f"[SidecarStderr] {msg}")

    def _atexit_cleanup(self) -> None:
        """Emergency process cleanup on Python termination."""
        if self._is_running or (self._proc and self._proc.poll() is None):
            self.close()

    # Context manager support
    def __enter__(self) -> DouyinBrowserRuntimeProvider:
        self.launch()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
