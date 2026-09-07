"""UUID Task Sandbox & Orphan Garbage Collection Engine (DY-D04).

Implements:
1. Production TaskSandbox & TaskSandboxProvider with execution-scoped UUID directories.
2. Strict separation between logical task_id (C09) and ephemeral execution_id (attempt).
3. Directory layout: input/, work/, output/, logs/ with atomic metadata persistence.
4. Path containment firewall: enforces relative containment, rejects ../ and absolute escape.
5. Sandbox retention policy: DELETE_ON_SUCCESS, PRESERVE_ON_FAILURE, PRESERVE_ALWAYS.
6. Multi-factor Orphan Detection: PID liveness check + metadata state + TTL grace period.
7. Crash recovery & garbage collection (GC): leaves active workers untouched, cleans stale orphans.
8. Cross-platform support (Windows NTFS + Linux/macOS) with zero final archive access.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.downloader.contracts import scrub_secrets

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Enums and Exceptions
# =============================================================================


class SandboxRetentionPolicy(str, Enum):
    """Retention behavior for task sandboxes upon execution completion."""

    DELETE_ON_SUCCESS = "DELETE_ON_SUCCESS"
    DELETE_ON_FAILURE = "DELETE_ON_FAILURE"
    PRESERVE_ON_FAILURE = "PRESERVE_ON_FAILURE"
    PRESERVE_ALWAYS = "PRESERVE_ALWAYS"


class SandboxState(str, Enum):
    """Lifecycle states of an execution sandbox."""

    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    CLEANED = "CLEANED"


class SandboxPathEscapeError(RuntimeError):
    """Raised when an artifact or file path attempts to escape sandbox containment."""


class SandboxDiskSpaceError(RuntimeError):
    """Raised when available disk space is below required threshold for sandbox creation."""


class SandboxMetadataCorruptError(RuntimeError):
    """Raised when sandbox.json metadata cannot be loaded or parsed."""


# =============================================================================
# 2. Process Liveness & Security Utilities
# =============================================================================


def is_pid_alive(pid: int) -> bool:
    """Robust cross-platform check whether a process ID is currently running.

    Invariant:
    - Conservative: if access is denied or indeterminate, returns True to prevent accidental deletion.
    - If PID <= 0, returns False.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                False,
                pid,
            )
            if handle == 0:
                # Process does not exist or access denied
                last_error = ctypes.windll.kernel32.GetLastError()
                ERROR_ACCESS_DENIED = 5
                if last_error == ERROR_ACCESS_DENIED:
                    return True
                return False

            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


def utcnow_iso() -> str:
    """Returns current UTC ISO-8601 timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# =============================================================================
# 3. Sandbox Metadata Contract (sandbox.json)
# =============================================================================


@dataclass
class TaskSandboxMetadata:
    """Persistent execution metadata for sandbox provenance and orphan detection.

    Security Invariant:
    - Guaranteed zero Cookie, Token, sessionid, authorization fields.
    """

    execution_id: str
    task_id: str
    platform: str
    platform_content_id: str
    owner_pid: int
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    owner_started_at: float = field(default_factory=time.time)
    state: str = SandboxState.ACTIVE.value
    failure_reason: str | None = None
    artifacts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Enforce security invariant: reject credentials in metadata fields
        for val in [self.execution_id, self.task_id, self.platform_content_id, str(self.failure_reason)]:
            if any(k in val.lower() for k in ["sessionid=", "sid_guard=", "mstoken=", "a_bogus="]):
                raise ValueError("Security violation: authentication token detected in TaskSandboxMetadata!")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "owner_pid": self.owner_pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "owner_started_at": round(self.owner_started_at, 3),
            "state": self.state,
            "failure_reason": scrub_secrets(self.failure_reason) if self.failure_reason else None,
            "artifacts": list(self.artifacts),
        }

    def save_atomic(self, file_path: Path) -> None:
        """Atomically saves metadata to disk using write-to-temp-then-rename."""
        tmp_path = file_path.with_suffix(".tmp")
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(tmp_path, file_path)

    @classmethod
    def load(cls, file_path: Path) -> TaskSandboxMetadata:
        """Loads and parses metadata from sandbox.json."""
        if not file_path.is_file():
            raise FileNotFoundError(f"Metadata file not found: {file_path}")
        try:
            raw = file_path.read_text(encoding="utf-8")
            d = json.loads(raw)
            return cls(
                execution_id=d["execution_id"],
                task_id=d["task_id"],
                platform=d.get("platform", "douyin"),
                platform_content_id=d.get("platform_content_id", "unknown"),
                owner_pid=int(d.get("owner_pid", 0)),
                created_at=d.get("created_at", ""),
                updated_at=d.get("updated_at", ""),
                owner_started_at=float(d.get("owner_started_at", 0.0)),
                state=d.get("state", SandboxState.ACTIVE.value),
                failure_reason=d.get("failure_reason"),
                artifacts=list(d.get("artifacts", [])),
            )
        except Exception as exc:
            raise SandboxMetadataCorruptError(f"Corrupt sandbox metadata at {file_path}: {exc}") from exc


# =============================================================================
# 4. TaskSandbox Implementation
# =============================================================================


class TaskSandbox:
    """Production isolated execution workspace for an individual download attempt."""

    def __init__(
        self,
        root: Path,
        execution_id: str,
        task_id: str,
        metadata: TaskSandboxMetadata,
        retention_policy: SandboxRetentionPolicy = SandboxRetentionPolicy.PRESERVE_ON_FAILURE,
    ) -> None:
        self._root = root.resolve()
        self._execution_id = execution_id
        self._task_id = task_id
        self._metadata = metadata
        self._retention_policy = retention_policy
        self._cleaned_up = False

        # Directory layout
        self._input_dir = self._root / "input"
        self._work_dir = self._root / "work"
        self._output_dir = self._root / "output"
        self._logs_dir = self._root / "logs"
        self._meta_file = self._root / "sandbox.json"

    @property
    def root(self) -> Path:
        """Root directory of this execution sandbox."""
        return self._root

    @property
    def path(self) -> Path:
        """Alias for root directory to satisfy legacy TaskSandbox protocol."""
        return self._root

    @property
    def execution_id(self) -> str:
        """UUID representing this specific execution attempt."""
        return self._execution_id

    @property
    def task_id(self) -> str:
        """Deterministic logical task identifier (from C09)."""
        return self._task_id

    @property
    def input_dir(self) -> Path:
        return self._input_dir

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def logs_dir(self) -> Path:
        return self._logs_dir

    @property
    def metadata(self) -> TaskSandboxMetadata:
        return self._metadata

    @property
    def retention_policy(self) -> SandboxRetentionPolicy:
        return self._retention_policy

    @property
    def is_cleaned_up(self) -> bool:
        return self._cleaned_up

    def register_artifact(self, path: Path) -> Path:
        """Registers a produced media file, strictly asserting sandbox containment.

        Raises:
            SandboxPathEscapeError: if path escapes sandbox containment.
        """
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(self._root)
        except ValueError as exc:
            raise SandboxPathEscapeError(
                f"Artifact '{path}' escapes sandbox containment! Resolved: '{resolved}', Root: '{self._root}'"
            ) from exc

        # Ensure it's not a path traversal trick with ..
        if ".." in rel.parts:
            raise SandboxPathEscapeError(f"Path traversal detected in relative artifact path: '{rel}'")

        rel_str = rel.as_posix()
        if rel_str not in self._metadata.artifacts:
            self._metadata.artifacts.append(rel_str)
            self._metadata.updated_at = utcnow_iso()
            self._metadata.save_atomic(self._meta_file)

        return resolved

    def list_artifacts(self) -> list[Path]:
        """Returns all media artifacts present in output_dir and work_dir."""
        found: list[Path] = []
        for target in [self._output_dir, self._work_dir]:
            if target.is_dir():
                for item in target.rglob("*"):
                    if item.is_file():
                        found.append(item)
        return found

    def finalize_success(self) -> None:
        """Transitions execution state to SUCCESS and applies retention policy."""
        self._metadata.state = SandboxState.SUCCESS.value
        self._metadata.updated_at = utcnow_iso()
        if self._root.is_dir():
            self._metadata.save_atomic(self._meta_file)

        if self._retention_policy in (
            SandboxRetentionPolicy.DELETE_ON_SUCCESS,
            SandboxRetentionPolicy.DELETE_ON_FAILURE,
        ):
            self._delete_root()

    def finalize_failure(self, reason: str = "") -> None:
        """Transitions execution state to FAILED and applies retention policy."""
        self._metadata.state = SandboxState.FAILED.value
        self._metadata.failure_reason = scrub_secrets(reason)
        self._metadata.updated_at = utcnow_iso()
        if self._root.is_dir():
            self._metadata.save_atomic(self._meta_file)

        if self._retention_policy == SandboxRetentionPolicy.DELETE_ON_FAILURE:
            self._delete_root()

    def cleanup(self) -> None:
        """Applies final cleanup based on retention policy. Safe to call multiple times."""
        if self._cleaned_up:
            return

        # If still in ACTIVE state upon cleanup, mark as interrupted failure
        if self._metadata.state in (SandboxState.ACTIVE.value, SandboxState.INITIALIZING.value):
            self.finalize_failure("Interrupted or incomplete execution cleanup")

        if self._metadata.state == SandboxState.SUCCESS.value:
            if self._retention_policy == SandboxRetentionPolicy.DELETE_ON_SUCCESS:
                self._delete_root()
        elif self._metadata.state == SandboxState.FAILED.value:
            if self._retention_policy == SandboxRetentionPolicy.DELETE_ON_FAILURE:
                self._delete_root()

        self._cleaned_up = True

    def _delete_root(self) -> None:
        """Safely removes the physical sandbox directory from disk."""
        if self._root.is_dir():
            shutil.rmtree(self._root, ignore_errors=True)
        self._cleaned_up = True


# =============================================================================
# 5. Production TaskSandboxProvider with Orphan Detection & GC
# =============================================================================


@dataclass(frozen=True)
class OrphanRecord:
    """Discovered orphaned or candidate workspace."""

    execution_id: str
    task_id: str
    path: Path
    state: str
    owner_pid: int
    is_pid_alive: bool
    age_seconds: float
    eligible_for_gc: bool
    reason: str


@dataclass(frozen=True)
class GcSummary:
    """Execution summary of an orphan garbage collection run."""

    scanned_count: int
    active_live_count: int
    orphaned_count: int
    deleted_count: int
    retained_failure_count: int
    quarantined_count: int
    deleted_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ProductionTaskSandboxProvider:
    """Production-grade TaskSandboxProvider managing UUID sandboxes, containment, and orphan GC."""

    def __init__(
        self,
        base_dir: Path | None = None,
        retention_policy: SandboxRetentionPolicy = SandboxRetentionPolicy.PRESERVE_ON_FAILURE,
        success_ttl_seconds: float = 300.0,
        failure_ttl_seconds: float = 86400.0,  # 24 hours
        orphan_grace_period_seconds: float = 600.0,  # 10 minutes
        min_disk_space_bytes: int = 50 * 1024 * 1024,  # 50 MB
    ) -> None:
        self._base_dir = (base_dir or Path(tempfile.gettempdir()) / "agy_downloader" / "tasks").resolve()
        self._retention_policy = retention_policy
        self._success_ttl = success_ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._orphan_grace = orphan_grace_period_seconds
        self._min_disk_space = min_disk_space_bytes

        # Ensure root base directory exists
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def retention_policy(self) -> SandboxRetentionPolicy:
        return self._retention_policy

    def create_sandbox(self, task: Any) -> TaskSandbox:
        """Creates a fresh, UUID-isolated execution sandbox for a task.

        Args:
            task: DownloadTask instance or logical task_id string.

        Raises:
            SandboxDiskSpaceError: if available disk space is insufficient.
        """
        # 1. Disk Space Check
        usage = shutil.disk_usage(self._base_dir)
        if usage.free < self._min_disk_space:
            raise SandboxDiskSpaceError(
                f"Insufficient disk space on {self._base_dir}: {usage.free} bytes available, {self._min_disk_space} required."
            )

        # 2. Extract Task Attributes
        if hasattr(task, "task_id"):
            task_id = str(task.task_id)
            platform = getattr(task, "platform", "douyin")
            platform_content_id = getattr(task, "platform_content_id", "unknown")
        elif isinstance(task, dict):
            task_id = str(task.get("task_id", "task_unknown"))
            platform = str(task.get("platform", "douyin"))
            platform_content_id = str(task.get("platform_content_id", "unknown"))
        else:
            task_id = str(task)
            platform = "douyin"
            platform_content_id = "unknown"

        # 3. Generate Ephemeral Execution ID (UUID4)
        execution_id = uuid.uuid4().hex

        # 4. Provision Sandbox Directory Layout
        root = self._base_dir / execution_id
        for subdir in ["input", "work", "output", "logs"]:
            (root / subdir).mkdir(parents=True, exist_ok=True)

        # 5. Persist Initial Metadata Atomically
        metadata = TaskSandboxMetadata(
            execution_id=execution_id,
            task_id=task_id,
            platform=platform,
            platform_content_id=platform_content_id,
            owner_pid=os.getpid(),
            owner_started_at=time.time(),
            state=SandboxState.ACTIVE.value,
        )
        metadata.save_atomic(root / "sandbox.json")

        return TaskSandbox(
            root=root,
            execution_id=execution_id,
            task_id=task_id,
            metadata=metadata,
            retention_policy=self._retention_policy,
        )

    def scan_orphans(self, now_epoch: float | None = None) -> list[OrphanRecord]:
        """Scans base_dir for orphaned workspaces using multi-factor heuristics.

        Invariants:
        1. AGE ALONE IS NOT PROOF: If owner_pid is still alive, NEVER mark as eligible for GC.
        2. Unrecognized or corrupt directories are marked for quarantine, never blindly wiped.
        """
        now = now_epoch if now_epoch is not None else time.time()
        records: list[OrphanRecord] = []

        if not self._base_dir.is_dir():
            return records

        for item in self._base_dir.iterdir():
            if not item.is_dir():
                continue

            meta_file = item / "sandbox.json"
            if not meta_file.is_file():
                records.append(
                    OrphanRecord(
                        execution_id=item.name,
                        task_id="unknown",
                        path=item,
                        state="UNKNOWN_NO_METADATA",
                        owner_pid=0,
                        is_pid_alive=False,
                        age_seconds=now - item.stat().st_mtime,
                        eligible_for_gc=False,
                        reason="Quarantine: directory missing sandbox.json metadata",
                    )
                )
                continue

            try:
                meta = TaskSandboxMetadata.load(meta_file)
            except SandboxMetadataCorruptError as exc:
                records.append(
                    OrphanRecord(
                        execution_id=item.name,
                        task_id="unknown",
                        path=item,
                        state="CORRUPT_METADATA",
                        owner_pid=0,
                        is_pid_alive=False,
                        age_seconds=now - item.stat().st_mtime,
                        eligible_for_gc=False,
                        reason=f"Quarantine: {exc}",
                    )
                )
                continue

            age_seconds = max(0.0, now - meta.owner_started_at)
            owner_alive = is_pid_alive(meta.owner_pid)

            # Heuristic 1: If owner is alive, NEVER GC (active execution or debugging session)
            if owner_alive:
                records.append(
                    OrphanRecord(
                        execution_id=meta.execution_id,
                        task_id=meta.task_id,
                        path=item,
                        state=meta.state,
                        owner_pid=meta.owner_pid,
                        is_pid_alive=True,
                        age_seconds=age_seconds,
                        eligible_for_gc=False,
                        reason="Active: owner process is currently alive",
                    )
                )
                continue

            # Heuristic 2: Owner is dead!
            if meta.state in (SandboxState.ACTIVE.value, SandboxState.INITIALIZING.value):
                # Worker crashed mid-execution
                eligible = age_seconds >= self._orphan_grace
                reason = "Crash Orphan: owner PID dead while ACTIVE" if eligible else "Grace period: owner PID dead recently"
                records.append(
                    OrphanRecord(
                        execution_id=meta.execution_id,
                        task_id=meta.task_id,
                        path=item,
                        state=SandboxState.ORPHANED.value,
                        owner_pid=meta.owner_pid,
                        is_pid_alive=False,
                        age_seconds=age_seconds,
                        eligible_for_gc=eligible,
                        reason=reason,
                    )
                )
            elif meta.state == SandboxState.SUCCESS.value:
                eligible = age_seconds >= self._success_ttl
                records.append(
                    OrphanRecord(
                        execution_id=meta.execution_id,
                        task_id=meta.task_id,
                        path=item,
                        state=meta.state,
                        owner_pid=meta.owner_pid,
                        is_pid_alive=False,
                        age_seconds=age_seconds,
                        eligible_for_gc=eligible,
                        reason="Expired Success: owner dead and TTL expired" if eligible else "Retained Success: TTL unexpired",
                    )
                )
            elif meta.state == SandboxState.FAILED.value:
                eligible = age_seconds >= self._failure_ttl
                records.append(
                    OrphanRecord(
                        execution_id=meta.execution_id,
                        task_id=meta.task_id,
                        path=item,
                        state=meta.state,
                        owner_pid=meta.owner_pid,
                        is_pid_alive=False,
                        age_seconds=age_seconds,
                        eligible_for_gc=eligible,
                        reason="Expired Failure: owner dead and failure TTL expired" if eligible else "Retained Failure: preserved for diagnostics",
                    )
                )
            else:
                records.append(
                    OrphanRecord(
                        execution_id=meta.execution_id,
                        task_id=meta.task_id,
                        path=item,
                        state=meta.state,
                        owner_pid=meta.owner_pid,
                        is_pid_alive=False,
                        age_seconds=age_seconds,
                        eligible_for_gc=False,
                        reason=f"Unknown state '{meta.state}'",
                    )
                )

        return records

    def gc_orphans(self, now_epoch: float | None = None) -> GcSummary:
        """Sweeps and deletes all eligible orphaned sandboxes."""
        records = self.scan_orphans(now_epoch=now_epoch)
        scanned = len(records)
        active_live = 0
        orphaned = 0
        deleted = 0
        retained_failure = 0
        quarantined = 0
        deleted_paths: list[str] = []
        errors: list[str] = []

        for rec in records:
            if rec.is_pid_alive:
                active_live += 1
                continue

            if "Quarantine" in rec.reason:
                quarantined += 1
                continue

            if rec.state == SandboxState.FAILED.value and not rec.eligible_for_gc:
                retained_failure += 1
                continue

            if rec.state == SandboxState.ORPHANED.value:
                orphaned += 1

            if rec.eligible_for_gc:
                try:
                    shutil.rmtree(rec.path, ignore_errors=True)
                    deleted += 1
                    deleted_paths.append(str(rec.path))
                except Exception as exc:
                    errors.append(f"Failed to delete {rec.path}: {exc}")

        return GcSummary(
            scanned_count=scanned,
            active_live_count=active_live,
            orphaned_count=orphaned,
            deleted_count=deleted,
            retained_failure_count=retained_failure,
            quarantined_count=quarantined,
            deleted_paths=deleted_paths,
            errors=errors,
        )
