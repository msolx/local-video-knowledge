"""M6-07 worker operations transport abstraction.

The M6-02 ``WorkerRuntime`` used the Operations store directly (via
``store_path``). M6-07 introduces an explicit transport boundary so the SAME
runtime can talk to a local SQLite store (NAS control-plane side) or a remote
control plane over HTTP (Windows PC side).

Design rules
------------
- ``WorkerOperationsTransport`` is the Protocol WorkerRuntime actually needs
  (register / heartbeat / claim / start / renew / complete / get).
- All state-machine rules still live in the server / store. A transport only
  *carries* requests; it never reinvents job transitions or fencing.
- ``LocalSQLiteWorkerTransport`` is a thin adapter over the frozen M6-02/M6-01
  store functions. It is what the NAS control plane's local worker and all
  existing local-mode tests use, so M6-02 semantics are preserved verbatim.
- ``RemoteUnavailableError`` signals a transient remote outage (connection
  refused / timeout / temporary 5xx). Worker loops treat it as "no job right
  now" and keep running, never crashing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .models import ClaimedJob, utc_now_iso
from .store import (
    StaleLeaseError,
    claim_next_job,
    complete_job_retryable_failure,
    complete_job_success,
    complete_job_terminal_failure,
    get_job,
    get_job_result,
    register_worker,
    renew_job_lease,
    start_claimed_job,
    worker_heartbeat,
)

__all__ = [
    "TransportError",
    "RemoteUnavailableError",
    "WorkerOperationsTransport",
    "LocalSQLiteWorkerTransport",
]


class TransportError(Exception):
    """Base error raised by a worker operations transport."""


class RemoteUnavailableError(TransportError):
    """The remote control plane is unreachable or temporarily unavailable.

    This is NOT a job-state error. Workers treat it as a transient condition:
    bounded retry/backoff, keep the host alive, rely on lease expiry recovery
    for any in-flight job.
    """


class WorkerOperationsTransport(Protocol):
    """The transport surface WorkerRuntime depends on.

    Every signature mirrors the corresponding store function (minus the
    ``db_path``, which the transport owns). Implementations may be local
    (SQLite) or remote (HTTP). Fencing semantics (lease_token) are preserved
    end-to-end: a stale token must surface as ``StaleLeaseError``.
    """

    def register_worker(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        display_name: Optional[str] = None,
        hostname: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        allowed_stages: Optional[list[str]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def heartbeat_worker(
        self,
        worker_id: str,
        *,
        capabilities: Optional[list[str]] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def claim_next_job(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        allowed_stages: Optional[list[str]] = None,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> Optional[ClaimedJob]: ...

    def start_claimed_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def complete_job_success(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def complete_job_retryable_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def complete_job_terminal_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]: ...

    def get_job_result(self, job_id: str) -> Optional[dict[str, Any]]: ...


def _allowlist_key(metadata: Optional[dict[str, Any]]) -> str:
    return "_allowed_stages"


class LocalSQLiteWorkerTransport:
    """Direct SQLite transport: delegates every call to the frozen store
    functions on the given ``db_path``.

    This preserves M6-02 behaviour exactly (same functions, same semantics).
    ``allowed_stages`` is an additive execution-placement filter persisted into
    the worker's metadata under ``_allowed_stages`` so the control plane can
    enforce stage placement even for local workers.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock or utc_now_iso

    def register_worker(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        display_name: Optional[str] = None,
        hostname: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        allowed_stages: Optional[list[str]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        meta = dict(metadata or {})
        if allowed_stages is not None:
            meta[_allowlist_key(meta)] = list(allowed_stages)
        return register_worker(
            self.db_path,
            worker_id,
            capabilities,
            display_name=display_name,
            hostname=hostname,
            metadata=meta,
            now=now or self._clock(),
        )

    def heartbeat_worker(
        self,
        worker_id: str,
        *,
        capabilities: Optional[list[str]] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        return worker_heartbeat(
            self.db_path,
            worker_id,
            capabilities=capabilities,
            status=status,
            metadata=metadata,
            now=now or self._clock(),
        )

    def claim_next_job(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        allowed_stages: Optional[list[str]] = None,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> Optional[ClaimedJob]:
        return claim_next_job(
            self.db_path,
            worker_id,
            capabilities,
            allowed_stages=allowed_stages,
            lease_duration_seconds=lease_duration_seconds,
            now=now or self._clock(),
        )

    def start_claimed_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return start_claimed_job(
            self.db_path,
            job_id,
            worker_id,
            lease_token,
            now=now or self._clock(),
            metadata=metadata,
        )

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_duration_seconds: int,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        return renew_job_lease(
            self.db_path,
            job_id,
            worker_id,
            lease_token,
            lease_duration_seconds=lease_duration_seconds,
            now=now or self._clock(),
        )

    def complete_job_success(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return complete_job_success(
            self.db_path,
            job_id,
            worker_id,
            lease_token,
            now=now or self._clock(),
            metadata=metadata,
        )

    def complete_job_retryable_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return complete_job_retryable_failure(
            self.db_path,
            job_id,
            worker_id,
            lease_token,
            error_class=error_class,
            error_message=error_message,
            now=now or self._clock(),
            metadata=metadata,
        )

    def complete_job_terminal_failure(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_class: str,
        error_message: str,
        now: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return complete_job_terminal_failure(
            self.db_path,
            job_id,
            worker_id,
            lease_token,
            error_class=error_class,
            error_message=error_message,
            now=now or self._clock(),
            metadata=metadata,
        )

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return get_job(self.db_path, job_id)

    def get_job_result(self, job_id: str) -> Optional[dict[str, Any]]:
        return get_job_result(self.db_path, job_id)

    def __repr__(self) -> str:
        return f"LocalSQLiteWorkerTransport({str(self.db_path)!r})"