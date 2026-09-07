"""Downloader Service Lock (DY-D10).

Guarantees single active Worker Service per host environment to prevent concurrent
F2 executions colliding on process-global state, cwd mutations, and logger configuration.

Key Invariants:
1. Triple Ownership Identity:
   - Worker owner identity is bound to (pid, process_started_at, instance_id).
2. Safe Dead Owner Reclaim Rules:
   - Case 1: PID does not exist in OS process table -> OWNER_DEAD -> Reclaim allowed.
   - Case 2: PID exists, but current process create_time != lock.process_started_at -> PID_REUSED -> Original owner dead -> Reclaim allowed.
   - Case 3: PID exists and process create_time matches -> ORIGINAL OWNER STILL ALIVE.
             Even if heartbeat is stale, DO NOT RECLAIM. Second worker refuses to start.
3. Heartbeat Semantics:
   - Heartbeat timeout indicates possible worker unhealthiness/stalled execution, NOT process death.
   - Heartbeat alone NEVER qualifies as death proof or ownership theft justification.
4. Clean Lifecycle:
   - Lock is released upon graceful service exit or context manager termination.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ServiceLockError(Exception):
    """Raised when service lock acquisition fails because another live worker holds it."""


class OwnerLivenessStatus(str, enum.Enum):
    """Liveness and health classification of a lock-holding process."""

    OWNER_DEAD = "OWNER_DEAD"
    PID_REUSED = "PID_REUSED"
    OWNER_ALIVE_HEALTHY = "OWNER_ALIVE_HEALTHY"
    OWNER_ALIVE_STALE = "OWNER_ALIVE_STALE"

    @property
    def is_alive(self) -> bool:
        return self in (OwnerLivenessStatus.OWNER_ALIVE_HEALTHY, OwnerLivenessStatus.OWNER_ALIVE_STALE)

    @property
    def is_stale(self) -> bool:
        return self == OwnerLivenessStatus.OWNER_ALIVE_STALE

    @property
    def reclaim_allowed(self) -> bool:
        return self in (OwnerLivenessStatus.OWNER_DEAD, OwnerLivenessStatus.PID_REUSED)


def _get_process_create_time(pid: int) -> float | None:
    """Safely retrieves process creation timestamp using psutil."""
    try:
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            return proc.create_time()
    except Exception:
        pass
    return None


def check_process_liveness(pid: int, expected_started_at: float | None = None) -> tuple[bool, str]:
    """Checks PID existence and creation time.

    Returns (is_original_alive, reason_code):
    - If PID doesn't exist -> (False, "OWNER_DEAD")
    - If PID exists but create_time doesn't match -> (False, "PID_REUSED")
    - If PID exists and create_time matches -> (True, "OWNER_ALIVE")
    """
    if pid <= 0:
        return False, "OWNER_DEAD"

    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False, "OWNER_DEAD"
        if expected_started_at is not None:
            try:
                proc = psutil.Process(pid)
                # Allow 2.0s drift due to clock precision differences across calls
                if abs(proc.create_time() - expected_started_at) > 2.0:
                    return False, "PID_REUSED"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False, "OWNER_DEAD"
        return True, "OWNER_ALIVE"
    except ImportError:
        # Fallback without psutil
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True, "OWNER_ALIVE"
            return False, "OWNER_DEAD"
        else:
            try:
                os.kill(pid, 0)
                return True, "OWNER_ALIVE"
            except OSError:
                return False, "OWNER_DEAD"


def is_pid_alive(pid: int, expected_create_time: float | None = None) -> bool:
    """Verifies whether a PID is alive and optionally matches expected create time.

    Cross-platform safe: works on Windows and POSIX.
    Preserved for backwards compatibility with tests and callers.
    """
    alive, _ = check_process_liveness(pid, expected_create_time)
    return alive


@dataclass(frozen=True)
class WorkerIdentity:
    """Immutable triplet identity for a physical worker service process."""

    pid: int
    process_started_at: float
    instance_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "instance_id": self.instance_id,
        }

    def serialize(self) -> str:
        """Serializes worker identity into a compact token string for claimed_by."""
        return f"{self.pid}:{self.process_started_at:.6f}:{self.instance_id}"

    @classmethod
    def parse(cls, token: str | None) -> WorkerIdentity | None:
        if not token:
            return None
        token = str(token).strip()
        if token.startswith("{"):
            try:
                d = json.loads(token)
                pid = int(d.get("pid", 0))
                ct = float(d.get("process_started_at") or d.get("create_time", 0.0))
                inst = str(d.get("instance_id") or d.get("worker_id", ""))
                if pid > 0 and ct > 0:
                    return cls(pid=pid, process_started_at=ct, instance_id=inst)
            except Exception:
                pass
        parts = token.split(":")
        if len(parts) >= 3:
            try:
                pid = int(parts[0])
                ct = float(parts[1])
                inst = ":".join(parts[2:])
                if pid > 0 and ct > 0:
                    return cls(pid=pid, process_started_at=ct, instance_id=inst)
            except (ValueError, TypeError):
                pass
        return None

    @classmethod
    def current(cls, instance_id: str | None = None) -> WorkerIdentity:
        pid = os.getpid()
        ct = _get_process_create_time(pid) or time.time()
        inst = instance_id or f"inst_{uuid.uuid4().hex[:8]}"
        return cls(pid=pid, process_started_at=ct, instance_id=inst)


@dataclass(frozen=True)
class LockInfo:
    worker_id: str
    pid: int
    create_time: float | None
    acquired_at: float
    lock_file: str
    heartbeat_at: float | None = None
    instance_id: str | None = None


def inspect_lock(
    lock_path: Path | str,
    heartbeat_stale_threshold_sec: float = 30.0,
) -> tuple[OwnerLivenessStatus, dict[str, Any]]:
    """Inspects a lock file and returns its ownership status and raw metadata."""
    p = Path(lock_path)
    if not p.exists():
        return OwnerLivenessStatus.OWNER_DEAD, {}

    try:
        content = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Unreadable service lock file (%s): %s", p, exc)
        return OwnerLivenessStatus.OWNER_DEAD, {}

    pid = content.get("pid")
    started_at = content.get("process_started_at") or content.get("create_time")
    heartbeat_at = content.get("heartbeat_at") or content.get("acquired_at") or 0.0

    if not pid:
        return OwnerLivenessStatus.OWNER_DEAD, content

    if not is_pid_alive(pid, started_at):
        is_alive, reason = check_process_liveness(pid, started_at)
        if reason == "PID_REUSED":
            return OwnerLivenessStatus.PID_REUSED, content
        return OwnerLivenessStatus.OWNER_DEAD, content

    # Owner process IS alive!
    age = time.time() - heartbeat_at
    if age > heartbeat_stale_threshold_sec:
        return OwnerLivenessStatus.OWNER_ALIVE_STALE, content
    return OwnerLivenessStatus.OWNER_ALIVE_HEALTHY, content


class DownloaderServiceLock:
    """PID and create-time validated mutual exclusion lock for Downloader Worker Service."""

    def __init__(
        self,
        lock_path: str | Path,
        worker_id: str | None = None,
        heartbeat_stale_threshold_sec: float = 30.0,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_stale_threshold_sec = heartbeat_stale_threshold_sec
        self.identity = WorkerIdentity.current(instance_id=worker_id)
        self.worker_id = worker_id or self.identity.serialize()
        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def inspect_owner(self) -> tuple[OwnerLivenessStatus, dict[str, Any]]:
        """Inspects current lock file on disk."""
        return inspect_lock(self.lock_path, self.heartbeat_stale_threshold_sec)

    def acquire(self) -> None:
        """Acquires the service lock or raises ServiceLockError if held by a live process."""
        now = time.time()

        if self.lock_path.exists():
            status, metadata = self.inspect_owner()
            owner_pid = metadata.get("pid")
            owner_started_at = metadata.get("process_started_at") or metadata.get("create_time")
            owner_instance = metadata.get("instance_id") or metadata.get("worker_id", "unknown")

            if status == OwnerLivenessStatus.OWNER_DEAD:
                logger.warning(
                    "Reclaiming service lock (%s): previous owner PID %s (instance '%s') is DEAD. Reclaim allowed.",
                    self.lock_path,
                    owner_pid,
                    owner_instance,
                )
            elif status == OwnerLivenessStatus.PID_REUSED:
                logger.warning(
                    "Reclaiming service lock (%s): previous owner PID %s was REUSED by another process "
                    "(create_time mismatch: expected %.2f). Original owner is dead. Reclaim allowed.",
                    self.lock_path,
                    owner_pid,
                    owner_started_at or 0.0,
                )
            elif status in (OwnerLivenessStatus.OWNER_ALIVE_HEALTHY, OwnerLivenessStatus.OWNER_ALIVE_STALE):
                # Re-entrancy check: same process
                if owner_pid == self.identity.pid:
                    if abs((owner_started_at or 0.0) - self.identity.process_started_at) <= 2.0:
                        if owner_instance == self.identity.instance_id or not owner_instance or owner_instance == "unknown":
                            self._acquired = True
                            return

                if status == OwnerLivenessStatus.OWNER_ALIVE_STALE:
                    heartbeat_at = metadata.get("heartbeat_at") or metadata.get("acquired_at") or 0.0
                    stale_age = now - heartbeat_at
                    raise ServiceLockError(
                        f"Lock owner process (PID {owner_pid}, instance='{owner_instance}') is STILL ALIVE, "
                        f"but heartbeat is stale ({stale_age:.1f}s > {self.heartbeat_stale_threshold_sec}s). "
                        f"Status: OWNER_ALIVE_STALE / MANUAL_ATTENTION_REQUIRED. "
                        f"Heartbeat timeout indicates possible worker unhealthiness, not process death. "
                        f"Refusing to steal lock from a live worker process."
                    )
                else:
                    raise ServiceLockError(
                        f"Another DownloaderWorkerService is actively running and healthy "
                        f"(PID {owner_pid}, instance='{owner_instance}', lock='{self.lock_path}'). "
                        f"Refusing to start second concurrent instance."
                    )

        # Write lock payload
        lock_data = {
            "pid": self.identity.pid,
            "process_started_at": self.identity.process_started_at,
            "create_time": self.identity.process_started_at,
            "instance_id": self.identity.instance_id,
            "worker_id": self.worker_id,
            "lock_created_at": now,
            "acquired_at": now,
            "heartbeat_at": now,
        }
        self.lock_path.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
        self._acquired = True
        logger.info(
            "Acquired DownloaderServiceLock (%s) as %s (PID %d)",
            self.lock_path,
            self.identity.instance_id,
            self.identity.pid,
        )

    def heartbeat(self) -> None:
        """Updates heartbeat timestamp in lock file while holding lock."""
        if not self._acquired or not self.lock_path.exists():
            return
        try:
            content = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if content.get("pid") == self.identity.pid:
                content["heartbeat_at"] = time.time()
                self.lock_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("Heartbeat write failed: %s", exc)

    def release(self) -> None:
        """Releases lock if owned by this process."""
        if not self._acquired:
            return

        try:
            if self.lock_path.exists():
                content = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if content.get("pid") == self.identity.pid:
                    self.lock_path.unlink(missing_ok=True)
                    logger.info("Released DownloaderServiceLock (%s)", self.lock_path)
        except Exception as exc:
            logger.warning("Error releasing service lock (%s): %s", self.lock_path, exc)
        finally:
            self._acquired = False

    def __enter__(self) -> DownloaderServiceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
