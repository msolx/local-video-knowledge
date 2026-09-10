"""M6-01 Operations domain models: asset/job state machines, deterministic identity.

Freezes the M6 contract for the Operations control plane (docs/M6_OPERATIONS_ARCHITECTURE.md
§5-§10, docs/M6_DECISIONS.md D1-D6, D10, D11, D13):

  - Asset lifecycle  : DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE
  - Job lifecycle    : QUEUED → LEASED → RUNNING → SUCCEEDED
                                                        ├── FAILED_RETRYABLE
                                                        └── FAILED_TERMINAL
                       plus the additive terminal state CANCELLED (M6-01 correction:
                       admin `cancel job` must be distinguishable from terminal failure).
  - Asset identity   : frozen canonical (platform, platform_content_id).
  - Job identity     : deterministic, no time/attempt/worker/lease components.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

# ----------------------------------------------------------------------
# Frozen version / path constants
# ----------------------------------------------------------------------

OPERATIONS_SCHEMA_VERSION = "operations-store-v1"
OPERATIONS_POLICY_VERSION = "operations-policy-v1"
OPERATIONS_USER_VERSION = 1
DEFAULT_OPERATIONS_PATH = "data/operations/operations.sqlite3"

# Frozen M6-00 capability vocabulary (Decision 8 / architecture §12). Exact-name
# matching only; empty requirement = generic control-plane job.
VALID_CAPABILITIES: frozenset[str] = frozenset(
    {
        "collector",
        "downloader",
        "cpu_media",
        "gpu_asr",
        "gpu_vlm",
        "llm_extraction",
        "store_ingest",
    }
)

# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class AssetLifecycleState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ARCHIVED = "ARCHIVED"
    EVIDENCE_READY = "EVIDENCE_READY"
    KNOWLEDGE_READY = "KNOWLEDGE_READY"
    SEARCHABLE = "SEARCHABLE"


class JobState(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELLED = "CANCELLED"


class JobStage(str, Enum):
    DISCOVER = "DISCOVER"
    ARCHIVE = "ARCHIVE"
    MEDIA_PROCESS = "MEDIA_PROCESS"
    EVIDENCE_READY = "EVIDENCE_READY"
    KNOWLEDGE_EXTRACT = "KNOWLEDGE_EXTRACT"
    KNOWLEDGE_FINALIZE = "KNOWLEDGE_FINALIZE"
    STORE_INGEST = "STORE_INGEST"


class PipelineRunStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TriggerType(str, Enum):
    DISCOVERY = "discovery"
    MANUAL = "manual"
    RECOVERY = "recovery"


# ----------------------------------------------------------------------
# Legal transition tables (single source of truth for the state engine)
# ----------------------------------------------------------------------

_ASSET_TRANSITIONS: dict[str, frozenset[str]] = {
    AssetLifecycleState.DISCOVERED.value: frozenset({AssetLifecycleState.ARCHIVED.value}),
    AssetLifecycleState.ARCHIVED.value: frozenset({AssetLifecycleState.EVIDENCE_READY.value}),
    AssetLifecycleState.EVIDENCE_READY.value: frozenset({AssetLifecycleState.KNOWLEDGE_READY.value}),
    AssetLifecycleState.KNOWLEDGE_READY.value: frozenset({AssetLifecycleState.SEARCHABLE.value}),
    AssetLifecycleState.SEARCHABLE.value: frozenset(),
}

_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    JobState.QUEUED.value: frozenset({JobState.LEASED.value, JobState.CANCELLED.value}),
    # LEASED → QUEUED is an explicit lease-expiry recovery transition.
    JobState.LEASED.value: frozenset({JobState.RUNNING.value, JobState.QUEUED.value}),
    JobState.RUNNING.value: frozenset(
        {
            JobState.SUCCEEDED.value,
            JobState.FAILED_RETRYABLE.value,
            JobState.FAILED_TERMINAL.value,
        }
    ),
    JobState.FAILED_RETRYABLE.value: frozenset(
        {
            JobState.QUEUED.value,
            JobState.CANCELLED.value,
            JobState.FAILED_TERMINAL.value,
        }
    ),
    JobState.SUCCEEDED.value: frozenset(),
    JobState.FAILED_TERMINAL.value: frozenset(),
    JobState.CANCELLED.value: frozenset(),
}

TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    {
        JobState.SUCCEEDED.value,
        JobState.FAILED_TERMINAL.value,
        JobState.CANCELLED.value,
    }
)

# States that carry an active worker lease.
LEASE_HELD_JOB_STATES: frozenset[str] = frozenset(
    {JobState.LEASED.value, JobState.RUNNING.value}
)

VALID_TRIGGER_TYPES: frozenset[str] = frozenset(
    {trigger.value for trigger in TriggerType}
)

_INPUT_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_PREFIX = "job_"
_JOB_ID_PREFIX_RE = re.compile(r"^job_[0-9a-f]{16}$")


def is_terminal_job_state(state: str) -> bool:
    return state in TERMINAL_JOB_STATES


def is_legal_asset_transition(from_state: str, to_state: str) -> bool:
    return to_state in _ASSET_TRANSITIONS.get(from_state, frozenset())


def is_legal_job_transition(from_state: str, to_state: str) -> bool:
    return to_state in _JOB_TRANSITIONS.get(from_state, frozenset())


# ----------------------------------------------------------------------
# Deterministic normalization (identity components only)
# ----------------------------------------------------------------------


def normalize_identity_component(value: str) -> str:
    """NFKC + strip + collapse whitespace. Used for identity components only."""
    normalized = unicodedata.normalize("NFKC", str(value))
    return " ".join(normalized.split())


def normalize_input_fingerprint(value: str) -> str:
    """Validate a stage input fingerprint (deterministic artifact digest).

    Only lowercase 64-hex is accepted so callers can never substitute a
    timestamp or ad-hoc string for a real upstream fingerprint.
    """
    if not isinstance(value, str) or not _INPUT_FINGERPRINT_RE.match(value):
        raise ValueError(
            "input_fingerprint must be a lowercase 64-char sha256 hex digest"
        )
    return value


def normalize_job_stage(value: str) -> str:
    stage = normalize_identity_component(value).upper()
    if stage not in {s.value for s in JobStage}:
        raise ValueError(f"unknown job stage: {value!r}")
    return stage


def normalize_capability(value: str) -> str:
    cap = normalize_identity_component(value)
    if cap not in VALID_CAPABILITIES:
        raise ValueError(f"unknown capability: {value!r}")
    return cap


def normalize_capabilities(values) -> list[str]:
    """Normalize + validate an ordered capability list (deduped, order preserved)."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cap = normalize_capability(value)
        if cap not in seen:
            seen.add(cap)
            result.append(cap)
    return result


# ----------------------------------------------------------------------
# Deterministic job identity
# ----------------------------------------------------------------------


def compute_job_id(
    platform: str,
    platform_content_id: str,
    stage: str,
    input_fingerprint: str,
    policy_version: str,
) -> str:
    """Deterministic orchestration job identity.

    job_id = "job_" + sha256(
        f"{platform}|{platform_content_id}|{stage}|{input_fingerprint}|{policy_version}"
    )[:16]

    Deliberately excludes: current time, attempt number, worker id, lease id,
    pipeline_run_id. Repeated scheduler ticks therefore collapse to one job.
    """
    platform = normalize_identity_component(platform)
    content_id = normalize_identity_component(platform_content_id)
    stage = normalize_job_stage(stage)
    fingerprint = normalize_input_fingerprint(input_fingerprint)
    policy_version = normalize_identity_component(policy_version)
    raw = f"{platform}|{content_id}|{stage}|{fingerprint}|{policy_version}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{_JOB_ID_PREFIX}{digest}"


def is_valid_job_id(value: str) -> bool:
    return bool(_JOB_ID_PREFIX_RE.match(value))


# ----------------------------------------------------------------------
# Timestamps / backoff (UTC ISO-8601; injectable for tests)
# ----------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def compute_backoff_seconds(
    attempt_number: int, base_seconds: int = 60, cap_seconds: int = 3600
) -> int:
    """Exponential backoff: min(base * 2**(attempt-1), cap). Computed in Python,
    never in a DB trigger."""
    if attempt_number < 1:
        attempt_number = 1
    return min(base_seconds * (2 ** (attempt_number - 1)), cap_seconds)


def compute_next_retry_at(now_iso: str, attempt_number: int, *, base_seconds: int = 60, cap_seconds: int = 3600) -> str:
    return format_iso(parse_iso(now_iso) + timedelta(seconds=compute_backoff_seconds(attempt_number, base_seconds, cap_seconds)))


def new_lease_token() -> str:
    """Cryptographically random fencing token for a job claim (``lease_<hex>``).

    A fresh token is generated on every successful claim; it participates in
    every execution-ownership mutation and is never part of logical job identity.
    """
    import secrets

    return "lease_" + secrets.token_hex(16)


# ----------------------------------------------------------------------
# Result models
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class EnqueueResult:
    """Result of enqueue_job.

    outcome is one of:
      - enqueued            : a new logical job was created (QUEUED).
      - exists_active       : same identity already QUEUED/LEASED/RUNNING.
      - existing_success    : same identity already SUCCEEDED → SKIP (cache hit).
      - exists_retryable    : same identity FAILED_RETRYABLE → retry/requeue handles it.
      - exists_terminal     : same identity FAILED_TERMINAL → not silently resurrected.
      - exists_cancelled    : same identity CANCELLED → not silently resurrected.
    """

    job_id: str
    state: str
    outcome: str
    created: bool
    message: str


@dataclass(frozen=True)
class OperationsValidationResult:
    valid: bool
    schema_version: str
    policy_version: str
    counts: dict[str, int]
    checks: dict[str, Any]
    violations: list[str]


@dataclass(frozen=True)
class ClaimedJob:
    """Result of an atomic capability-aware claim (M6-02).

    Contains the job identity plus the execution-ownership lease for the current
    claim. ``lease_token`` is the fencing token for this execution generation —
    it is excluded from ``repr()`` and from the public/JSON serialization so it
    is never accidentally logged.
    """

    job_id: str
    stage: str
    canonical_id: str
    platform: str
    platform_content_id: str
    input_fingerprint: str
    required_capabilities: frozenset[str]
    lease_owner: str
    leased_at: str
    lease_expires_at: str
    _lease_token: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def lease_token(self) -> str:
        return self._lease_token

    def to_public_dict(self) -> dict[str, Any]:
        """JSON/log-safe serialization — never includes the lease token."""
        return {
            "job_id": self.job_id,
            "stage": self.stage,
            "canonical_id": self.canonical_id,
            "platform": self.platform,
            "platform_content_id": self.platform_content_id,
            "input_fingerprint": self.input_fingerprint,
            "required_capabilities": sorted(self.required_capabilities),
            "lease_owner": self.lease_owner,
            "leased_at": self.leased_at,
            "lease_expires_at": self.lease_expires_at,
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        # Explicit repr that hides the fencing token.
        return (
            f"ClaimedJob(job_id={self.job_id!r}, stage={self.stage!r}, "
            f"canonical_id={self.canonical_id!r}, lease_expires_at={self.lease_expires_at!r})"
        )