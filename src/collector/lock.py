"""Service-level single-instance lock with stale process detection."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LockError

logger = logging.getLogger("collector.lock")


def is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is currently active on the host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class ServiceLock:
    """Acquires an exclusive lock file to prevent concurrent collector sync instances."""

    def __init__(
        self,
        lock_path: Path,
        platform: str = "douyin",
        timeout_sec: int = 3600,
    ) -> None:
        self.lock_path = lock_path
        self.platform = platform
        self.timeout_sec = timeout_sec
        self.acquired = False
        self.run_id: str | None = None

    def acquire(self, run_id: str) -> None:
        self.run_id = run_id
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self.lock_path.exists():
            stale = False
            lock_info: dict[str, Any] = {}
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    lock_info = json.load(f)
                locked_pid = lock_info.get("pid", 0)
                locked_time_str = lock_info.get("started_at", "")

                # Check process liveness
                if not is_pid_alive(locked_pid):
                    logger.warning(
                        f"Found stale lock file for dead PID {locked_pid}. Breaking lock."
                    )
                    stale = True
                elif locked_time_str:
                    # Check timeout expiry
                    locked_time = datetime.fromisoformat(locked_time_str).timestamp()
                    if time.time() - locked_time > self.timeout_sec:
                        logger.warning(
                            f"Lock for PID {locked_pid} exceeded timeout ({self.timeout_sec}s). Breaking lock."
                        )
                        stale = True
            except Exception as e:
                logger.warning(f"Malformed lock file found ({e}). Breaking lock.")
                stale = True

            if not stale:
                raise LockError(
                    f"Collector service is locked by running process (PID {lock_info.get('pid')}, "
                    f"run_id: {lock_info.get('run_id')})",
                    details=lock_info,
                )

            # Break stale lock
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception as e:
                raise LockError(f"Failed to remove stale lock file: {e}")

        # Write lock file
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "platform": self.platform,
            "run_id": self.run_id,
        }
        try:
            with open(self.lock_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self.acquired = True
        except Exception as e:
            raise LockError(f"Failed to create lock file {self.lock_path}: {e}")

    def release(self) -> None:
        if self.acquired and self.lock_path.exists():
            try:
                # Only unlink if this process owns the lock
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                if info.get("pid") == os.getpid() and info.get("run_id") == self.run_id:
                    self.lock_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Error releasing lock file: {e}")
            finally:
                self.acquired = False

    def __enter__(self) -> ServiceLock:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
