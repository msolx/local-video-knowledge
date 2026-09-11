"""M6-03 pipeline stage adapters — connect WorkerRuntime to the sealed M2-M5
stage entry points.

Contract
--------
An adapter is a thin wrapper. It does NOT reimplement pipeline logic. It only:

  - parses typed job/claim context (canonical_id, platform_content_id, stage,
    input_fingerprint, job metadata/config reference)
  - resolves logical artifact paths from configurable roots
  - calls the sealed stage API
  - preflights upstream state
  - decides artifact-based idempotency / cache hits
  - validates output artifacts
  - computes a deterministic output fingerprint
  - returns a JSON-safe typed ``StageExecutionResult``

Error classification uses the M6-02 worker contract:

  - ``RetryableJobError``  : transient runtime/precondition failures
                             (browser temporarily unavailable, auth runtime
                             unavailable, file still transferring, LLM endpoint
                             unavailable, DB temporarily locked, upstream stage
                             artifact not yet landed)
  - ``TerminalJobError``   : permanent failures (schema-invalid canonical
                             artifact, unsupported media, corrupt finalized
                             source, identity mismatch)

The M6-02 WorkerRuntime is the only orchestrator; it knows nothing about M2-M5.
Adapters are injected into it via ``build_stage_handler_registry``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .models import JobStage, normalize_identity_component
from .worker import RetryableJobError, TerminalJobError

__all__ = [
    "STAGES_POLICY_VERSION",
    "STAGE_EXECUTION_RESULT_SCHEMA_VERSION",
    "STATUS_EXECUTED",
    "STATUS_CACHE_HIT",
    "ArtifactDescriptor",
    "StageExecutionResult",
    "is_valid_sha256",
    "fingerprint_artifacts",
    "stage_output_fingerprint",
    "required_capabilities_for_stage",
    "StageAdapter",
    "DiscoverAdapter",
    "ArchiveAdapter",
    "MediaProcessAdapter",
    "KnowledgeExtractAdapter",
    "KnowledgeFinalizeAdapter",
    "StoreIngestAdapter",
    "build_stage_handler_registry",
    "validate_stage_execution_result",
]

# ----------------------------------------------------------------------
# Frozen policy / schema constants
# ----------------------------------------------------------------------

STAGES_POLICY_VERSION = "m6-stages-policy-v1"
STAGE_EXECUTION_RESULT_SCHEMA_VERSION = "m6-stage-execution-result-v1"

STATUS_EXECUTED = "EXECUTED"
STATUS_CACHE_HIT = "CACHE_HIT"
_VALID_STATUSES = frozenset({STATUS_EXECUTED, STATUS_CACHE_HIT})

# M6-00 Decision 8 capability vocabulary (exact-name matching).
_STAGE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    JobStage.DISCOVER.value: ("collector",),
    JobStage.ARCHIVE.value: ("downloader",),
    JobStage.MEDIA_PROCESS.value: (),  # media-type dependent; see function below
    JobStage.KNOWLEDGE_EXTRACT.value: ("llm_extraction",),
    JobStage.KNOWLEDGE_FINALIZE.value: (),  # deterministic render, no LLM needed
    JobStage.STORE_INGEST.value: ("store_ingest",),
}

# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def is_valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class ArtifactDescriptor:
    """One canonical output artifact of a stage execution.

    ``path`` is the logical relative path (e.g. ``processed/.../knowledge_units.json``).
    ``resolved_path`` is execution-local diagnostic only and is never part of the
    output fingerprint.
    """

    role: str
    path: str
    sha256: str
    size_bytes: Optional[int] = None
    resolved_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "path": self.path, "sha256": self.sha256}
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        if self.resolved_path is not None:
            data["resolved_path"] = self.resolved_path
        return data

    def fingerprint_dict(self) -> dict[str, Any]:
        """Deterministic fingerprint payload (role + path + sha256 only)."""
        return {"role": self.role, "path": self.path, "sha256": self.sha256}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    policy_version: str = STAGES_POLICY_VERSION,
) -> str:
    """Deterministic fingerprint over canonical ordered artifact descriptors.

    Sorting rule: by (role, path) ascending. Only role/path/sha256 participate;
    mtime/size/resolved paths are excluded so NAS path changes never alter the
    canonical output identity.
    """
    ordered = sorted(
        artifacts,
        key=lambda d: (str(d.get("role", "")), str(d.get("path", ""))),
    )
    payload = json.dumps(
        {"policy_version": policy_version, "artifacts": ordered},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def stage_output_fingerprint(
    stage: str,
    artifacts: list[dict[str, Any]],
    *,
    policy_version: str = STAGES_POLICY_VERSION,
) -> str:
    """Compute an output fingerprint for a stage from its canonical artifacts.

    If a stage has a frozen M4/M5 artifact fingerprint (enriched candidates
    fingerprint, finalization fingerprint, M5 source artifact fingerprint) the
    adapter reuses that value directly; this helper is for artifact-composed
    stages (ARCHIVE, MEDIA_PROCESS).
    """
    return fingerprint_artifacts(artifacts, policy_version=policy_version)


# ----------------------------------------------------------------------
# StageExecutionResult
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StageExecutionResult:
    """Typed JSON-safe result of one stage adapter execution.

    ``output_fingerprint`` is deterministic (never time/worker/attempt/job
    dependent) and becomes the ``input_fingerprint`` of the downstream stage.
    """

    stage: str
    canonical_id: str
    status: str
    input_fingerprint: str
    output_fingerprint: str
    artifacts: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = STAGE_EXECUTION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.artifacts is not None and not isinstance(self.artifacts, tuple):
            object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if self.metadata is not None and not isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "canonical_id": self.canonical_id,
            "status": self.status,
            "input_fingerprint": self.input_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "artifacts": [dict(a) for a in self.artifacts],
            "metadata": dict(self.metadata),
        }

    def validate(self) -> None:
        """Internal invariant checks. Raises TerminalJobError on violation."""
        if self.stage not in _STAGE_CAPABILITIES:
            raise TerminalJobError(f"unknown stage {self.stage!r} in StageExecutionResult")
        if self.status not in _VALID_STATUSES:
            raise TerminalJobError(f"invalid status {self.status!r}")
        if not self.canonical_id:
            raise TerminalJobError("StageExecutionResult.canonical_id is empty")
        if not is_valid_sha256(self.input_fingerprint):
            raise TerminalJobError("input_fingerprint is not a valid SHA-256")
        if not is_valid_sha256(self.output_fingerprint):
            raise TerminalJobError("output_fingerprint is not a valid SHA-256")
        json.dumps(self.to_dict())  # JSON-safe check


def validate_stage_execution_result(result: "StageExecutionResult", claimed: Any) -> None:
    """Identity audit at the WorkerRuntime integration point (M6-03 §28).

    Before a job can be committed SUCCEEDED the stage result must match the
    claimed job identity exactly; otherwise the result must NOT be committed.
    """
    result.validate()
    if result.stage != claimed.stage:
        raise TerminalJobError(
            f"stage result stage {result.stage!r} != job stage {claimed.stage!r}"
        )
    if result.canonical_id != claimed.canonical_id:
        raise TerminalJobError(
            f"stage result canonical_id {result.canonical_id!r} != "
            f"job canonical_id {claimed.canonical_id!r}"
        )
    if result.input_fingerprint != claimed.input_fingerprint:
        raise TerminalJobError(
            f"stage result input_fingerprint {result.input_fingerprint!r} != "
            f"job input_fingerprint {claimed.input_fingerprint!r}"
        )
    json.dumps(result.to_dict())


# ----------------------------------------------------------------------
# Capability mapping (M6-00 Decision 8 / M6-03 §23)
# ----------------------------------------------------------------------


def required_capabilities_for_stage(stage: Any, media_type: Optional[str] = None) -> tuple[str, ...]:
    """Deterministic stage -> capability mapping for the M6-04 scheduler.

    ``media_type`` is required for MEDIA_PROCESS: ``video`` -> gpu_asr,
    ``album`` -> gpu_vlm (the two halves of M3 run on different workers).
    """
    stage_name = normalize_identity_component(stage).upper()
    if stage_name == JobStage.MEDIA_PROCESS.value:
        if media_type == "video":
            return ("gpu_asr",)
        if media_type == "album":
            return ("gpu_vlm",)
        raise ValueError("media_type is required for MEDIA_PROCESS capability mapping")
    if stage_name not in _STAGE_CAPABILITIES:
        raise ValueError(f"unknown stage {stage!r}")
    return _STAGE_CAPABILITIES[stage_name]


# ----------------------------------------------------------------------
# Adapter base
# ----------------------------------------------------------------------


class StageAdapter:
    """Base class for M6-03 pipeline stage adapters.

    Subclasses expose ``stage`` and ``execute(claimed) -> StageExecutionResult``.
    ``to_handler`` produces a callable accepted by WorkerRuntime.
    """

    stage: str = ""

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return required_capabilities_for_stage(self.stage)

    def to_handler(self) -> Callable[[Any], StageExecutionResult]:
        return self.execute

    def execute(self, claimed: Any) -> StageExecutionResult:
        raise NotImplementedError


# ----------------------------------------------------------------------
# DiscoverAdapter (M2 collector / M6-00 DISCOVER)
# ----------------------------------------------------------------------


class DiscoverAdapter(StageAdapter):
    """Execute the M2 collector abstraction for a discovery run.

    This adapter only invokes the existing collector service abstraction; it
    does NOT reimplement list/collection parsing or cursor/watermark logic.
    Discovery is batch-oriented: one run may discover many canonical assets, so
    the result records the discovered identities in ``metadata``. The scheduler
    (M6-04) turns each discovered identity into downstream ARCHIVE jobs; no
    polling loop lives here.

    Error classification:
      - DEPENDENCY_NOT_READY / AUTH_NOT_READY / RUNTIME_ERROR / LOCKED -> Retryable
      - CONFIG_ERROR / NOT_IMPLEMENTED / UNKNOWN               -> Terminal
    """

    stage = JobStage.DISCOVER.value

    def __init__(
        self,
        collector=None,
        *,
        platform: str = "douyin",
        mode: str = "sync",
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.collector = collector
        self.platform = platform
        self.mode = mode
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ("collector",)

    def execute(self, claimed: Any) -> StageExecutionResult:
        if self.collector is None:
            raise RetryableJobError("collector runtime not available")
        run = self.collector.execute(self.mode, platform=self.platform)
        data = run.to_dict() if hasattr(run, "to_dict") else dict(run)
        status = data.get("status")
        error = data.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        if status and str(status).endswith("NOT_IMPLEMENTED"):
            raise RetryableJobError(f"collector dependencies not ready: {error}")
        if code in (
            "DEPENDENCY_NOT_READY",
            "AUTH_NOT_READY",
            "RUNTIME_ERROR",
            "LOCKED",
        ):
            raise RetryableJobError(
                f"collector transient failure ({code}): {error.get('message', '')}"
            )
        if code in ("CONFIG_ERROR", "NOT_IMPLEMENTED", "UNKNOWN"):
            raise TerminalJobError(
                f"collector permanent failure ({code}): {error.get('message', '')}"
            )
        metrics = data.get("metrics", {}) or {}
        discovered = metrics.get("discovered", []) or []
        seen = set()
        identities = []
        for item in discovered:
            if item in seen:
                continue
            seen.add(item)
            identities.append(
                {
                    "platform": self.platform,
                    "platform_content_id": item,
                }
            )
        out_fp = fingerprint_artifacts(
            [
                {
                    "role": "discovered_identity",
                    "path": f"{self.platform}/{item}",
                    "sha256": _sha256_bytes(str(item).encode("utf-8")),
                }
                for item in sorted(set(discovered))
            ],
            policy_version=self.policy_version,
        )
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=out_fp,
            artifacts=(),
            metadata={
                "platform": self.platform,
                "discovered": identities,
                "metrics": {k: v for k, v in metrics.items() if not isinstance(v, dict)},
            },
        )


# ----------------------------------------------------------------------
# ArchiveAdapter (M2 downloader / M6-00 ARCHIVE)
# ----------------------------------------------------------------------


class ArchiveAdapter(StageAdapter):
    """Execute the M2 downloader abstraction for one canonical asset.

    Precondition : asset identity + source detail are available (from claimed
                   identity / job metadata).
    Cache rule   : a valid formal archive (asset_manifest.json schema-valid +
                   media present + SHA-256 match) already exists -> CACHE_HIT.
    Postcondition: formal archive artifacts exist and the manifest/hash are
                   valid (CanonicalMediaAssetAdapter.load_from_dir passes).
    """

    stage = JobStage.ARCHIVE.value

    def __init__(
        self,
        downloader=None,
        *,
        archive_root: Optional[Path] = None,
        media_adapter=None,
        platform: str = "douyin",
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.downloader = downloader
        self.archive_root = Path(archive_root) if archive_root else None
        self.media_adapter = media_adapter
        self.platform = platform
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ("downloader",)

    def _load_asset(self, claimed: Any):
        """Return the formal asset or None when the archive is absent.

        Raises TerminalJobError when a manifest exists but is schema-invalid
        (a corrupt formal archive must not be silently overwritten)."""
        if self.media_adapter is None:
            return None
        try:
            return self.media_adapter.load_from_content_id(
                claimed.platform_content_id, platform=self.platform
            )
        except Exception as exc:
            from ..media_adapter.adapter import (
                ManifestInvalidError,
                ManifestNotFoundError,
            )

            if isinstance(exc, ManifestNotFoundError):
                return None
            if isinstance(exc, ManifestInvalidError):
                raise TerminalJobError(
                    f"corrupt formal archive for {claimed.platform_content_id}: {exc}"
                ) from exc
            # Unknown loader failure: assume the archive is simply not there yet
            # (upstream transfer may still be in progress).
            return None

    def _archive_artifacts(self, asset: Any) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        manifest = getattr(asset, "manifest_path", None)
        if manifest is not None:
            artifacts.append(
                {
                    "role": "archive_manifest",
                    "path": f"archive/{self.platform}/{asset.platform_content_id}/asset_manifest.json",
                    "sha256": _sha256_file(Path(manifest)),
                }
            )
        video = getattr(asset, "video_path", None)
        if video is not None:
            sha = getattr(asset, "video_sha256", None) or _sha256_file(Path(video))
            artifacts.append(
                {
                    "role": "media_video",
                    "path": f"archive/{self.platform}/{asset.platform_content_id}/{Path(video).name}",
                    "sha256": sha,
                }
            )
        for img in getattr(asset, "album_images", ()) or ():
            path = getattr(img, "path", None)
            sha = getattr(img, "sha256", None)
            if path is None:
                continue
            artifacts.append(
                {
                    "role": "media_image",
                    "path": f"archive/{self.platform}/{asset.platform_content_id}/{Path(path).name}",
                    "sha256": sha or _sha256_file(Path(path)),
                }
            )
        return artifacts

    def _media_type(self, asset: Any) -> Optional[str]:
        """Resolve a durable media_type ('video'/'album') from the formal asset.

        Minimal M6-04 gap-fill: the scheduler needs media_type to route the
        MEDIA_PROCESS capability (video -> gpu_asr, album -> gpu_vlm). We never
        infer it from canonical_id.
        """
        if getattr(asset, "is_video", False):
            return "video"
        if getattr(asset, "is_album", False):
            return "album"
        ctype = getattr(asset, "content_type", None)
        ctype_str = str(getattr(ctype, "value", ctype)).lower()
        if "video" in ctype_str:
            return "video"
        if "album" in ctype_str or "image" in ctype_str:
            return "album"
        return None

    def execute(self, claimed: Any) -> StageExecutionResult:
        asset = self._load_asset(claimed)
        if asset is not None:
            return StageExecutionResult(
                stage=self.stage,
                canonical_id=claimed.canonical_id,
                status=STATUS_CACHE_HIT,
                input_fingerprint=claimed.input_fingerprint,
                output_fingerprint=stage_output_fingerprint(
                    self.stage, self._archive_artifacts(asset), policy_version=self.policy_version
                ),
                artifacts=tuple(self._archive_artifacts(asset)),
                metadata={
                    "cache_hit": True,
                    "archive": True,
                    "media_type": self._media_type(asset),
                },
            )
        if self.downloader is None:
            raise RetryableJobError("downloader runtime not available")
        task = self._build_task(claimed)
        result = self.downloader.execute(task)
        if getattr(result, "status", None) != "SUCCESS":
            if getattr(result, "retryable", False):
                raise RetryableJobError(
                    getattr(result, "message", "") or "downloader retryable failure"
                )
            raise TerminalJobError(
                getattr(result, "message", "") or "downloader terminal failure"
            )
        asset = self._load_asset(claimed)
        if asset is None:
            raise TerminalJobError(
                "downloader reported success but no formal archive appeared"
            )
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=stage_output_fingerprint(
                self.stage, self._archive_artifacts(asset), policy_version=self.policy_version
            ),
            artifacts=tuple(self._archive_artifacts(asset)),
            metadata={
                "cache_hit": False,
                "archive": True,
                "media_type": self._media_type(asset),
            },
        )

    def _build_task(self, claimed: Any):
        meta = dict(getattr(claimed, "metadata", {}) or {})
        source_url = meta.get("source_url")
        scope_id = meta.get("scope_id", "default")
        content_type = meta.get("content_type", "video")
        if not source_url:
            raise RetryableJobError("source_url not available in job metadata")
        from ..collector.download_models import (
            DownloadTask,
            compute_download_task_id,
        )

        task_id = compute_download_task_id(
            claimed.platform,
            scope_id,
            claimed.platform_content_id,
            content_type,
        )
        return DownloadTask.from_dict(
            {
                "task_id": task_id,
                "platform": claimed.platform,
                "scope_id": scope_id,
                "platform_content_id": claimed.platform_content_id,
                "content_type": content_type,
                "source_url": source_url,
            }
        )


# ----------------------------------------------------------------------
# MediaProcessAdapter (M3 evidence build / M6-00 MEDIA_PROCESS)
# ----------------------------------------------------------------------


def _sync_processed_artifacts(src_dir: Path, dst_dir: Path) -> None:
    """Atomically copy processed artifacts from src_dir to dst_dir.

    Ensures:
    - Atomicity via temporary files (.tmp.* replaced on completion)
    - Destination SHA-256 matches source
    - Existing identical files are idempotent (no re-copy)
    - Lock files and temporary files are excluded
    - Non-destructive: source files are read-only and never modified/deleted
    """
    import shutil

    src_path = Path(src_dir)
    dst_path = Path(dst_dir)
    if not src_path.is_dir():
        return
    if src_path.resolve() == dst_path.resolve():
        return

    dst_path.mkdir(parents=True, exist_ok=True)
    for item in src_path.iterdir():
        if item.name.endswith(".lock") or item.name.startswith(".tmp."):
            continue
        dest = dst_path / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        elif item.is_file():
            # Check idempotency
            if dest.is_file() and dest.stat().st_size == item.stat().st_size:
                if _sha256_file(dest) == _sha256_file(item):
                    continue
            tmp_dest = dst_path / f".tmp.{item.name}"
            shutil.copy2(item, tmp_dest)
            tmp_dest.replace(dest)
            # Verify destination hash matches source
            src_hash = _sha256_file(item)
            dst_hash = _sha256_file(dest)
            if src_hash != dst_hash:
                dest.unlink(missing_ok=True)
                raise IOError(
                    f"Artifact sync hash mismatch for {item.name}: {src_hash} != {dst_hash}"
                )


class MediaProcessAdapter(StageAdapter):
    """Execute the M3 evidence build for a canonical asset (video or album).

    Cache rule: evidence_manifest.json + evidence_chunks.json exist and are
    schema-valid (verify_evidence_manifest + verify_evidence_chunks) -> CACHE_HIT.

    Routing: video -> process_canonical_asset, album -> process_canonical_album.
    Capability is media-type dependent (gpu_asr / gpu_vlm); the scheduler picks
    the requirement via ``required_capabilities_for_stage`` using media_type.
    """

    stage = JobStage.MEDIA_PROCESS.value

    def __init__(
        self,
        *,
        config=None,
        processed_root: Optional[Path] = None,
        media_adapter=None,
        force: bool = False,
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.config = config
        self.processed_root = Path(processed_root) if processed_root else None
        self.media_adapter = media_adapter
        self.force = force
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ("gpu_asr", "gpu_vlm")

    def _processed_dir(self, claimed: Any) -> Path:
        root = self.processed_root or Path("data/processed")
        return root / claimed.canonical_id

    def _evidence_files(self, processed_dir: Path) -> tuple[Path, Path]:
        return (
            processed_dir / "evidence_manifest.json",
            processed_dir / "evidence_chunks.json",
        )

    def _evidence_artifacts(self, processed_dir: Path) -> list[dict[str, Any]]:
        manifest_path, chunks_path = self._evidence_files(processed_dir)
        artifacts = []
        if manifest_path.is_file():
            artifacts.append(
                {
                    "role": "evidence_manifest",
                    "path": f"processed/{processed_dir.name}/evidence_manifest.json",
                    "sha256": _sha256_file(manifest_path),
                }
            )
        if chunks_path.is_file():
            artifacts.append(
                {
                    "role": "evidence_chunks",
                    "path": f"processed/{processed_dir.name}/evidence_chunks.json",
                    "sha256": _sha256_file(chunks_path),
                }
            )
        return artifacts

    def _evidence_valid(self, processed_dir: Path) -> bool:
        from ..chunking.service import verify_evidence_chunks
        from ..provenance import verify_evidence_manifest

        manifest_path, chunks_path = self._evidence_files(processed_dir)
        if not manifest_path.is_file() or not chunks_path.is_file():
            return False
        return verify_evidence_manifest(manifest_path) and verify_evidence_chunks(chunks_path)

    def execute(self, claimed: Any) -> StageExecutionResult:
        from ..media_adapter.adapter import ManifestNotFoundError

        processed_dir = self._processed_dir(claimed)
        if self._evidence_valid(processed_dir):
            return StageExecutionResult(
                stage=self.stage,
                canonical_id=claimed.canonical_id,
                status=STATUS_CACHE_HIT,
                input_fingerprint=claimed.input_fingerprint,
                output_fingerprint=stage_output_fingerprint(
                    self.stage,
                    self._evidence_artifacts(processed_dir),
                    policy_version=self.policy_version,
                ),
                artifacts=tuple(self._evidence_artifacts(processed_dir)),
                metadata={"cache_hit": True, "media_type": self._media_type(claimed)},
            )
        manifest_path, chunks_path = self._evidence_files(processed_dir)
        if manifest_path.is_file() or chunks_path.is_file():
            # A partial or corrupt evidence artifact must never be treated as a
            # cache hit and must not be silently overwritten.
            raise TerminalJobError(
                f"existing evidence artifacts for {claimed.canonical_id} are "
                "invalid (partial/corrupt); refusing to cache-hit or overwrite"
            )
        if self.config is None:
            raise RetryableJobError("media processing runtime not available")
        if self.media_adapter is None:
            raise RetryableJobError("archive adapter not available")
        try:
            asset = self.media_adapter.load_from_content_id(
                claimed.platform_content_id, platform=claimed.platform
            )
        except ManifestNotFoundError as exc:
            raise RetryableJobError(
                f"formal archive for {claimed.platform_content_id} not present"
            ) from exc
        content_type = getattr(asset, "content_type", "")
        from ..pipeline import process_canonical_album, process_canonical_asset

        if getattr(asset, "is_video", False):
            result = process_canonical_asset(
                self.config, asset, force=self.force, stop_after="chunk"
            )
        elif getattr(asset, "is_album", False):
            result = process_canonical_album(
                self.config, asset, force=self.force, stop_after="chunk"
            )
        else:
            raise TerminalJobError(
                f"unsupported media content_type {content_type!r} for "
                f"{claimed.platform_content_id}"
            )
        if result is not None:
            res_path = Path(result)
            if res_path.resolve() != processed_dir.resolve() and res_path.is_dir():
                _sync_processed_artifacts(res_path, processed_dir)
        if not self._evidence_valid(processed_dir):
            raise TerminalJobError(
                "M3 processor reported success but evidence artifacts are not valid"
            )
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=stage_output_fingerprint(
                self.stage,
                self._evidence_artifacts(processed_dir),
                policy_version=self.policy_version,
            ),
            artifacts=tuple(self._evidence_artifacts(processed_dir)),
            metadata={"cache_hit": False, "media_type": self._media_type(claimed)},
        )

    def _media_type(self, claimed: Any) -> Optional[str]:
        if self.media_adapter is not None:
            try:
                asset = self.media_adapter.load_from_content_id(
                    claimed.platform_content_id, platform=claimed.platform
                )
            except Exception:
                asset = None
            if asset is not None:
                if getattr(asset, "is_video", False):
                    return "video"
                if getattr(asset, "is_album", False):
                    return "album"
        # Fall back to the evidence manifest content_type when the archive
        # adapter is unavailable (e.g. offline cache audit on processed data).
        manifest_path = self._evidence_files(self._processed_dir(claimed))[0]
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                return None
            ct = str(data.get("content_type", ""))
            if "video" in ct:
                return "video"
            if "album" in ct or "image" in ct:
                return "album"
        return None


# ----------------------------------------------------------------------
# KnowledgeExtractAdapter (M4 extract+merge+enrich / KNOWLEDGE_EXTRACT)
# ----------------------------------------------------------------------


class KnowledgeExtractAdapter(StageAdapter):
    """Run the M4 candidate chain: extract -> merge -> enrich.

    M4 owns its own raw-extraction cache, fingerprints and run lineage; the
    adapter calls the M4 API (which returns ``cache_hit``) instead of inventing
    a second "file exists -> skip" rule. LLM calls are only made by M4 when the
    cache misses.

    Postcondition: enriched_knowledge_candidates.json (or frozen equivalent) is
    schema-valid. Output fingerprint reuses the frozen M4 enriched fingerprint
    when present, else composes from artifact hashes.
    """

    stage = JobStage.KNOWLEDGE_EXTRACT.value

    def __init__(
        self,
        *,
        processed_root: Optional[Path] = None,
        backend=None,
        extraction_config=None,
        merge_config=None,
        enrichment_config=None,
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.processed_root = Path(processed_root) if processed_root else None
        self.backend = backend
        self.extraction_config = extraction_config
        self.merge_config = merge_config
        self.enrichment_config = enrichment_config
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ("llm_extraction",)

    def _processed_dir(self, claimed: Any) -> Path:
        root = self.processed_root or Path("data/processed")
        return root / claimed.canonical_id

    def _knowledge_dir(self, claimed: Any) -> Path:
        return self._processed_dir(claimed) / "knowledge"

    def execute(self, claimed: Any) -> StageExecutionResult:
        from ..knowledge.enrichment import enrich_knowledge_candidates
        from ..knowledge.extractor import extract_knowledge_candidates
        from ..knowledge.merger import merge_knowledge_candidates

        processed_dir = self._processed_dir(claimed)
        kdir = self._knowledge_dir(claimed)
        try:
            extracted = extract_knowledge_candidates(
                processed_dir, config=self.extraction_config, backend=self.backend
            )
            merged = merge_knowledge_candidates(processed_dir, config=self.merge_config)
            enriched = enrich_knowledge_candidates(
                processed_dir, config=self.enrichment_config, backend=self.backend
            )
        except FileNotFoundError as exc:
            raise RetryableJobError(
                f"M4 upstream artifacts not ready: {exc}"
            ) from exc
        all_cached = bool(
            extracted.get("cache_hit") and merged.get("cache_hit") and enriched.get("cache_hit")
        )
        enriched_file = kdir / "enriched_knowledge_candidates.json"
        if not enriched_file.is_file():
            raise TerminalJobError("M4 enrichment produced no enriched artifact")
        frozen_fp = enriched.get("fingerprint")
        if frozen_fp and is_valid_sha256(frozen_fp):
            out_fp = frozen_fp
        else:
            out_fp = stage_output_fingerprint(
                self.stage,
                [
                    {
                        "role": "enriched_candidates",
                        "path": f"processed/{claimed.canonical_id}/knowledge/enriched_knowledge_candidates.json",
                        "sha256": _sha256_file(enriched_file),
                    }
                ],
                policy_version=self.policy_version,
            )
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_CACHE_HIT if all_cached else STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=out_fp,
            artifacts=(
                {
                    "role": "enriched_candidates",
                    "path": f"processed/{claimed.canonical_id}/knowledge/enriched_knowledge_candidates.json",
                    "sha256": _sha256_file(enriched_file),
                },
            ),
            metadata={"cache_hit": all_cached, "stage_chain": True},
        )


# ----------------------------------------------------------------------
# KnowledgeFinalizeAdapter (M4 render / KNOWLEDGE_FINALIZE)
# ----------------------------------------------------------------------


class KnowledgeFinalizeAdapter(StageAdapter):
    """Run M4 finalize_knowledge_document to produce knowledge_units.json.

    Cache rule: knowledge_units.json exists and parses via
    CanonicalKnowledgeUnitsDocument.from_dict() with canonical_id matching the
    asset -> CACHE_HIT. Otherwise run the deterministic render.

    The adapter never modifies KU fields (statement/verification_status etc.).
    """

    stage = JobStage.KNOWLEDGE_FINALIZE.value

    def __init__(
        self,
        *,
        processed_root: Optional[Path] = None,
        render_config=None,
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.processed_root = Path(processed_root) if processed_root else None
        self.render_config = render_config
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ()

    def _processed_dir(self, claimed: Any) -> Path:
        root = self.processed_root or Path("data/processed")
        return root / claimed.canonical_id

    def _units_path(self, claimed: Any) -> Path:
        return self._processed_dir(claimed) / "knowledge" / "knowledge_units.json"

    def _units_valid(self, claimed: Any) -> bool:
        from ..knowledge.models import CanonicalKnowledgeUnitsDocument

        units_path = self._units_path(claimed)
        if not units_path.is_file():
            return False
        try:
            doc = CanonicalKnowledgeUnitsDocument.from_dict(
                json.loads(units_path.read_text(encoding="utf-8"))
            )
        except Exception:
            return False
        return doc.canonical_id == claimed.canonical_id

    def execute(self, claimed: Any) -> StageExecutionResult:
        from ..knowledge.render import finalize_knowledge_document

        processed_dir = self._processed_dir(claimed)
        units_path = self._units_path(claimed)
        if self._units_valid(claimed):
            return StageExecutionResult(
                stage=self.stage,
                canonical_id=claimed.canonical_id,
                status=STATUS_CACHE_HIT,
                input_fingerprint=claimed.input_fingerprint,
                output_fingerprint=stage_output_fingerprint(
                    self.stage,
                    [
                        {
                            "role": "knowledge_units",
                            "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                            "sha256": _sha256_file(units_path),
                        }
                    ],
                    policy_version=self.policy_version,
                ),
                artifacts=(
                    {
                        "role": "knowledge_units",
                        "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                        "sha256": _sha256_file(units_path),
                    },
                ),
                metadata={"cache_hit": True, "final": True},
            )
        finalized = None
        try:
            finalized = finalize_knowledge_document(
                processed_dir, config=self.render_config
            )
        except FileNotFoundError as exc:
            raise RetryableJobError(
                "M4 upstream artifacts not ready for finalize "
                f"(enriched candidates missing): {exc}"
            ) from exc
        if not self._units_valid(claimed):
            raise TerminalJobError(
                "finalize reported success but knowledge_units.json is invalid or "
                "canonical_id mismatch"
            )
        frozen_fp = finalized.get("finalization_fingerprint")
        if not frozen_fp or not is_valid_sha256(frozen_fp):
            frozen_fp = stage_output_fingerprint(
                self.stage,
                [
                    {
                        "role": "knowledge_units",
                        "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                        "sha256": _sha256_file(units_path),
                    }
                ],
                policy_version=self.policy_version,
            )
        unit_count = finalized.get("output_unit_count")
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=frozen_fp,
            artifacts=(
                {
                    "role": "knowledge_units",
                    "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                    "sha256": _sha256_file(units_path),
                },
            ),
            metadata={
                "cache_hit": bool(finalized.get("cache_hit")),
                "output_unit_count": unit_count,
                "final": True,
            },
        )


# ----------------------------------------------------------------------
# StoreIngestAdapter (M5 ingest / STORE_INGEST)
# ----------------------------------------------------------------------


class StoreIngestAdapter(StageAdapter):
    """Ingest knowledge_units.json into the M5 knowledge store.

    Normal path is per-asset incremental ingest (inserted / replaced / unchanged
    all count as success); ``rebuild_store`` is never used as a stage. The
    knowledge store DB lives on the NAS/control-plane owner; a Windows GPU
    worker must not write it over SMB (M6-03 §22).
    """

    stage = JobStage.STORE_INGEST.value

    def __init__(
        self,
        *,
        knowledge_store_path: Optional[Path] = None,
        processed_root: Optional[Path] = None,
        policy_version: str = STAGES_POLICY_VERSION,
    ) -> None:
        self.knowledge_store_path = (
            Path(knowledge_store_path) if knowledge_store_path else None
        )
        self.processed_root = Path(processed_root) if processed_root else None
        self.policy_version = policy_version

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return ("store_ingest",)

    def _units_path(self, claimed: Any) -> Path:
        root = self.processed_root or Path("data/processed")
        return root / claimed.canonical_id / "knowledge" / "knowledge_units.json"

    def execute(self, claimed: Any) -> StageExecutionResult:
        from ..knowledge.store import (
            get_ingested_asset,
            get_unit,
            ingest_knowledge_document,
        )

        if self.knowledge_store_path is None:
            raise RetryableJobError("knowledge store path not configured")
        units_path = self._units_path(claimed)
        if not units_path.is_file():
            raise RetryableJobError(
                f"knowledge_units.json for {claimed.canonical_id} not present"
            )
        try:
            result = ingest_knowledge_document(self.knowledge_store_path, units_path)
        except Exception as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise RetryableJobError(f"knowledge store temporarily locked: {exc}") from exc
            raise TerminalJobError(f"knowledge store ingest failed: {exc}") from exc
        status = getattr(result, "status", "")
        if status not in ("inserted", "replaced", "unchanged"):
            raise TerminalJobError(f"unexpected ingest status {status!r}")
        asset = get_ingested_asset(self.knowledge_store_path, claimed.canonical_id)
        if asset is None:
            raise TerminalJobError(
                f"ingest reported {status} but asset {claimed.canonical_id} "
                "not present in store"
            )
        unit_count = getattr(result, "unit_count", None)
        if unit_count is not None and asset.get("unit_count") != unit_count:
            raise TerminalJobError(
                f"ingest unit_count {unit_count} != stored unit_count "
                f"{asset.get('unit_count')}"
            )
        source_fp = getattr(result, "source_artifact_fingerprint", "")
        if not is_valid_sha256(source_fp):
            source_fp = stage_output_fingerprint(
                self.stage,
                [
                    {
                        "role": "knowledge_units",
                        "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                        "sha256": _sha256_file(units_path),
                    }
                ],
                policy_version=self.policy_version,
            )
        return StageExecutionResult(
            stage=self.stage,
            canonical_id=claimed.canonical_id,
            status=STATUS_CACHE_HIT if status == "unchanged" else STATUS_EXECUTED,
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=source_fp,
            artifacts=(
                {
                    "role": "knowledge_units",
                    "path": f"processed/{claimed.canonical_id}/knowledge/knowledge_units.json",
                    "sha256": _sha256_file(units_path),
                },
            ),
            metadata={
                "cache_hit": status == "unchanged",
                "ingest_status": status,
                "unit_count": unit_count,
                "store": True,
            },
        )


# ----------------------------------------------------------------------
# Registry builder (M6-03 §24)
# ----------------------------------------------------------------------


def build_stage_handler_registry(
    *,
    workspace_root: Optional[Path] = None,
    processed_root: Optional[Path] = None,
    archive_root: Optional[Path] = None,
    knowledge_store_path: Optional[Path] = None,
    collector=None,
    downloader=None,
    media_adapter=None,
    app_config=None,
    llm_backend=None,
    extraction_config=None,
    merge_config=None,
    enrichment_config=None,
    render_config=None,
    force: bool = False,
    adapters: Optional[dict[str, StageAdapter]] = None,
) -> dict[str, Callable[[Any], StageExecutionResult]]:
    """Build the handler mapping handed to WorkerRuntime.

    Dependency injection keeps WorkerRuntime free of any M2/M3/M4/M5 knowledge;
    every dependency is replaceable with a fake in tests. Paths/config are
    logical roots (workspace/processed/archive) plus the M5 knowledge store
    path. When ``adapters`` is provided it wins (tests inject fakes directly).
    """
    if adapters is not None:
        return {name: adapter.to_handler() for name, adapter in adapters.items()}
    if processed_root is None and workspace_root is not None:
        processed_root = Path(workspace_root) / "data" / "processed"
    if archive_root is None and workspace_root is not None:
        archive_root = Path(workspace_root) / "data" / "raw_archive"
    adapters = {
        JobStage.DISCOVER.value: DiscoverAdapter(
            collector, platform="douyin", policy_version=STAGES_POLICY_VERSION
        ),
        JobStage.ARCHIVE.value: ArchiveAdapter(
            downloader,
            archive_root=archive_root,
            media_adapter=media_adapter,
            platform="douyin",
            policy_version=STAGES_POLICY_VERSION,
        ),
        JobStage.MEDIA_PROCESS.value: MediaProcessAdapter(
            config=app_config,
            processed_root=processed_root,
            media_adapter=media_adapter,
            force=force,
            policy_version=STAGES_POLICY_VERSION,
        ),
        JobStage.KNOWLEDGE_EXTRACT.value: KnowledgeExtractAdapter(
            processed_root=processed_root,
            backend=llm_backend,
            extraction_config=extraction_config,
            merge_config=merge_config,
            enrichment_config=enrichment_config,
            policy_version=STAGES_POLICY_VERSION,
        ),
        JobStage.KNOWLEDGE_FINALIZE.value: KnowledgeFinalizeAdapter(
            processed_root=processed_root,
            render_config=render_config,
            policy_version=STAGES_POLICY_VERSION,
        ),
        JobStage.STORE_INGEST.value: StoreIngestAdapter(
            knowledge_store_path=knowledge_store_path,
            processed_root=processed_root,
            policy_version=STAGES_POLICY_VERSION,
        ),
    }
    return {name: adapter.to_handler() for name, adapter in adapters.items()}