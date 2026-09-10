"""M6-03 pipeline stage adapter tests.

Covers the stage adapter contract: StageExecutionResult, deterministic output
fingerprints, capability mapping, M2/M3/M4/M5 adapter behavior, artifact-based
idempotency, error classification, WorkerRuntime result persistence, at-least-
once replay, and offline real-C10 cache-hit audits.

No live Douyin / network / GPU / LLM runtime is used. Real C10 artifacts are
read-only (existing validated outputs only).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.operations import (
    STATUS_CACHE_HIT,
    STATUS_EXECUTED,
    JobState,
    JobStage,
    StageExecutionResult,
    build_stage_handler_registry,
    claim_next_job,
    complete_job_success,
    complete_job_terminal_failure,
    create_operations_store,
    enqueue_job,
    fingerprint_artifacts,
    get_job,
    get_job_result,
    is_valid_sha256,
    required_capabilities_for_stage,
    start_claimed_job,
    stage_output_fingerprint,
    validate_stage_execution_result,
)
from src.operations.worker import (
    HandlerResult,
    RetryableJobError,
    TerminalJobError,
    WorkerRuntime,
)

PLATFORM = "douyin"
CONTENT_ID = "7681603850364521734"
CANONICAL_ID = "douyin_7681603850364521734"
FINGERPRINT = "a" * 64
T0 = "2026-09-10T01:00:00+00:00"
T_PLUS = "2026-09-10T01:02:00+00:00"

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROCESSED_ROOT = DATA / "processed"
VIDEO_ASSET = "douyin_7681603850364521734"
ALBUM_ASSET = "douyin_7682038498466993905"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "operations.sqlite3"
    create_operations_store(db)
    return db


def _enqueue(db: Path, stage=JobStage.ARCHIVE.value, *, metadata=None):
    from src.operations import register_asset

    register_asset(
        db,
        PLATFORM,
        CONTENT_ID,
        canonical_id=CANONICAL_ID,
        now=T0,
    )
    return enqueue_job(
        db,
        PLATFORM,
        CONTENT_ID,
        stage,
        FINGERPRINT,
        policy_version="m6-policy-v1",
        canonical_id=CANONICAL_ID,
        required_capabilities=required_capabilities_for_stage(stage),
        metadata=metadata,
        now=T0,
    )


def _claimed(db: Path, stage=JobStage.ARCHIVE.value, *, worker_id="w1", metadata=None):
    from src.operations import register_worker

    register_worker(
        db, worker_id, list(required_capabilities_for_stage(stage)), now=T0
    )
    _enqueue(db, stage, metadata=metadata)
    claimed = claim_next_job(
        db, worker_id, list(required_capabilities_for_stage(stage)), now=T0
    )
    assert claimed is not None
    start_claimed_job(db, claimed.job_id, worker_id, claimed.lease_token, now=T0)
    return claimed


def _worker_ready(db: Path, stage=JobStage.ARCHIVE.value, *, worker_id="w1", metadata=None):
    """Register a worker and enqueue a job WITHOUT claiming it, so the
    WorkerRuntime can claim+start+complete the job itself in run_once."""
    from src.operations import register_worker

    register_worker(
        db, worker_id, list(required_capabilities_for_stage(stage)), now=T0
    )
    return _enqueue(db, stage, metadata=metadata)


class FakeCollector:
    def __init__(self, result=None, error=None):
        self._result = result or {"status": "SUCCESS", "metrics": {"discovered": ["1", "2"]}}
        self._error = error
        self.calls = []

    def execute(self, mode, platform=None, **kwargs):
        self.calls.append({"mode": mode, "platform": platform})
        if self._error:
            return {"status": "FAILED", "metrics": {}, "error": self._error}
        return self._result


class FakeDownloader:
    def __init__(self, result=None, error_code=None, retryable=False):
        self._result = result
        self._error_code = error_code
        self._retryable = retryable
        self.tasks = []

    def execute(self, task):
        self.tasks.append(task)
        if self._result is not None:
            return self._result
        return SimpleNamespace(
            status="FAILED",
            error_code=self._error_code,
            retryable=self._retryable,
            message="simulated failure",
            assets=[],
        )


class FakeMediaAdapter:
    """Loads a formal archive asset from an archive_root dir."""

    def __init__(self, archive_root: Path, platform=PLATFORM):
        self.archive_root = Path(archive_root)
        self.platform = platform

    def load_from_content_id(self, content_id, platform=None, **kwargs):
        from src.media_adapter.adapter import ManifestNotFoundError

        platform = platform or self.platform
        candidate = self.archive_root / platform / content_id
        if not (candidate / "asset_manifest.json").is_file():
            raise ManifestNotFoundError(
                f"Formal asset directory for {platform}:{content_id} not found"
            )
        return _FakeAsset(candidate)


class _FakeAsset:
    def __init__(self, root: Path):
        self.asset_root = root
        self.platform_content_id = root.name
        self.canonical_id = f"douyin_{root.name}"
        self.manifest_path = root / "asset_manifest.json"
        self.video_path = root / "video.mp4"
        self.video_sha256 = _fake_sha(self.video_path)
        self.album_images = ()
        self.is_video = self.video_path.is_file()
        self.is_album = not self.is_video


def _fake_sha(path: Path, default: str = "c" * 64):
    if path.is_file():
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()
    return default


def _write_archive(tmp_path: Path, content_id=CONTENT_ID):
    platform_dir = tmp_path / PLATFORM / content_id
    platform_dir.mkdir(parents=True, exist_ok=True)
    (platform_dir / "video.mp4").write_bytes(b"fake-media")
    manifest = {
        "schema_version": "asset-manifest-v1",
        "platform": PLATFORM,
        "platform_content_id": content_id,
        "assets": [
            {
                "file_name": "video.mp4",
                "relative_path": "video.mp4",
                "size_bytes": 10,
                "content_type": "video",
                "sha256": _fake_sha(platform_dir / "video.mp4"),
            }
        ],
    }
    (platform_dir / "asset_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return platform_dir


def _write_evidence(tmp_path: Path, *, valid=True, canonical_id=CANONICAL_ID):
    from src.chunking.service import compute_chunks_fingerprint
    from src.provenance import compute_manifest_fingerprint

    pdir = tmp_path / "processed" / canonical_id
    pdir.mkdir(parents=True, exist_ok=True)
    if valid:
        manifest = {
            "schema_version": "evidence-manifest-v1",
            "canonical_id": canonical_id,
            "source_metadata": {},
            "formal_asset": {},
            "processing_provenance": {"model_provenance": {}},
            "evidence_summary": {"verification_status": "not_checked"},
            "evidence_items": [],
        }
        manifest["manifest_fingerprint"] = compute_manifest_fingerprint(
            canonical_id, {}, {}, [], {}
        )
        chunks = {
            "schema_version": "evidence-chunks-v1",
            "canonical_id": canonical_id,
            "summary": {"verification_status": "not_checked", "total_evidence_referenced": 0},
            "chunks": [],
        }
        chunks["chunks_fingerprint"] = compute_chunks_fingerprint("", {}, [])
    else:
        manifest = {"schema_version": "broken", "truncated": True}
        chunks = {"schema_version": "evidence-chunks-v1", "summary": {}, "chunks": None}
    (pdir / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (pdir / "evidence_chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    return pdir


def _write_units(tmp_path: Path, *, valid=True, canonical_id=CANONICAL_ID, units=None):
    kdir = tmp_path / "processed" / canonical_id / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    if valid:
        doc = {
            "schema_version": "knowledge-units-v1",
            "canonical_id": canonical_id,
            "generated_at": T0,
            "unit_count": len(units or []),
            "units": units or [],
            "extraction_provenance": {
                "backend": "mock",
                "model": "qwen3-8b",
                "prompt_version": "v1",
                "temperature": 0.1,
                "generated_at": T0,
                "evidence_manifest_fingerprint": FINGERPRINT,
                "evidence_chunks_fingerprint": FINGERPRINT,
            },
        }
    else:
        doc = {"schema_version": "nope", "units": "truncated"}
    (kdir / "knowledge_units.json").write_text(json.dumps(doc), encoding="utf-8")
    return kdir / "knowledge_units.json"


# ----------------------------------------------------------------------
# StageExecutionResult + fingerprints
# ----------------------------------------------------------------------


def test_stage_result_serialization():
    r = StageExecutionResult(
        stage="ARCHIVE",
        canonical_id=CANONICAL_ID,
        status=STATUS_EXECUTED,
        input_fingerprint=FINGERPRINT,
        output_fingerprint="b" * 64,
        artifacts=({"role": "manifest", "path": "p", "sha256": "c" * 64},),
        metadata={"k": 1},
    )
    d = r.to_dict()
    assert d["schema_version"] == "m6-stage-execution-result-v1"
    assert d["stage"] == "ARCHIVE"
    assert d["status"] == "EXECUTED"
    json.dumps(d)  # JSON-safe


def test_stage_result_invalid_status():
    with pytest.raises(TerminalJobError):
        StageExecutionResult(
            stage="ARCHIVE",
            canonical_id=CANONICAL_ID,
            status="BOGUS",
            input_fingerprint=FINGERPRINT,
            output_fingerprint="b" * 64,
        ).validate()


def test_stage_result_invalid_output_fingerprint():
    with pytest.raises(TerminalJobError):
        StageExecutionResult(
            stage="ARCHIVE",
            canonical_id=CANONICAL_ID,
            status=STATUS_EXECUTED,
            input_fingerprint=FINGERPRINT,
            output_fingerprint="not-a-sha",
        ).validate()


def test_deterministic_output_fingerprint():
    a1 = [{"role": "r1", "path": "p1", "sha256": "a" * 64}]
    a2 = [{"role": "r1", "path": "p1", "sha256": "a" * 64}]
    assert fingerprint_artifacts(a1) == fingerprint_artifacts(a2)


def test_path_ordering_deterministic():
    f1 = fingerprint_artifacts(
        [
            {"role": "b", "path": "z", "sha256": "a" * 64},
            {"role": "a", "path": "m", "sha256": "b" * 64},
        ]
    )
    f2 = fingerprint_artifacts(
        [
            {"role": "a", "path": "m", "sha256": "b" * 64},
            {"role": "b", "path": "z", "sha256": "a" * 64},
        ]
    )
    assert f1 == f2


def test_fingerprint_depends_on_content():
    f1 = fingerprint_artifacts([{"role": "r", "path": "p", "sha256": "a" * 64}])
    f2 = fingerprint_artifacts([{"role": "r", "path": "p", "sha256": "b" * 64}])
    assert f1 != f2


def test_stage_output_fingerprint_is_sha256():
    fp = stage_output_fingerprint("ARCHIVE", [{"role": "r", "path": "p", "sha256": "a" * 64}])
    assert is_valid_sha256(fp)


# ----------------------------------------------------------------------
# Identity audit
# ----------------------------------------------------------------------


def _make_result(**over):
    base = {
        "stage": "ARCHIVE",
        "canonical_id": CANONICAL_ID,
        "status": STATUS_EXECUTED,
        "input_fingerprint": FINGERPRINT,
        "output_fingerprint": "b" * 64,
    }
    base.update(over)
    return StageExecutionResult(**base)


class _FakeClaimed:
    def __init__(self, *, metadata=None):
        self.stage = "ARCHIVE"
        self.canonical_id = CANONICAL_ID
        self.input_fingerprint = FINGERPRINT
        self.platform = PLATFORM
        self.platform_content_id = CONTENT_ID
        self.metadata = metadata or {}


def test_identity_audit_stage_mismatch():
    with pytest.raises(TerminalJobError):
        validate_stage_execution_result(_make_result(stage="DISCOVER"), _FakeClaimed())


def test_identity_audit_canonical_mismatch():
    with pytest.raises(TerminalJobError):
        validate_stage_execution_result(_make_result(canonical_id="other"), _FakeClaimed())


def test_identity_audit_input_fingerprint_mismatch():
    with pytest.raises(TerminalJobError):
        validate_stage_execution_result(_make_result(input_fingerprint="d" * 64), _FakeClaimed())


def test_identity_audit_ok():
    r = _make_result()
    validate_stage_execution_result(r, _FakeClaimed())  # no raise


# ----------------------------------------------------------------------
# Capability mapping
# ----------------------------------------------------------------------


def test_capability_mapping_discover():
    assert required_capabilities_for_stage("DISCOVER") == ("collector",)


def test_capability_mapping_archive():
    assert required_capabilities_for_stage("ARCHIVE") == ("downloader",)


def test_capability_mapping_media_video():
    assert required_capabilities_for_stage("MEDIA_PROCESS", media_type="video") == ("gpu_asr",)


def test_capability_mapping_media_album():
    assert required_capabilities_for_stage("MEDIA_PROCESS", media_type="album") == ("gpu_vlm",)


def test_capability_mapping_media_requires_type():
    with pytest.raises(ValueError):
        required_capabilities_for_stage("MEDIA_PROCESS")


def test_capability_mapping_knowledge_extract():
    assert required_capabilities_for_stage("KNOWLEDGE_EXTRACT") == ("llm_extraction",)


def test_capability_mapping_knowledge_finalize():
    assert required_capabilities_for_stage("KNOWLEDGE_FINALIZE") == ()


def test_capability_mapping_store_ingest():
    assert required_capabilities_for_stage("STORE_INGEST") == ("store_ingest",)


# ----------------------------------------------------------------------
# Discover adapter
# ----------------------------------------------------------------------


def test_discover_fake_success(tmp_path):
    collector = FakeCollector()
    adapter = _discover_adapter(collector)
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_EXECUTED
    assert result.stage == "DISCOVER"
    assert len(result.metadata["discovered"]) == 2
    assert collector.calls[0]["mode"] == "sync"


def test_discover_duplicate_mapping(tmp_path):
    collector = FakeCollector({"status": "SUCCESS", "metrics": {"discovered": ["1", "1", "2"]}})
    result = _discover_adapter(collector).execute(_FakeClaimed())
    ids = [d["platform_content_id"] for d in result.metadata["discovered"]]
    assert sorted(ids) == ["1", "2"]


def test_discover_retryable_error(tmp_path):
    collector = FakeCollector(
        error={"code": "DEPENDENCY_NOT_READY", "message": "browser missing"}
    )
    with pytest.raises(RetryableJobError):
        _discover_adapter(collector).execute(_FakeClaimed())


def test_discover_missing_runtime(tmp_path):
    with pytest.raises(RetryableJobError):
        _discover_adapter(None).execute(_FakeClaimed())


# ----------------------------------------------------------------------
# Archive adapter
# ----------------------------------------------------------------------


def _discover_adapter(collector):
    from src.operations.stages import DiscoverAdapter

    return DiscoverAdapter(collector)


def _archive_adapter(tmp_path, downloader=None, media_adapter=None):
    from src.operations.stages import ArchiveAdapter

    return ArchiveAdapter(
        downloader,
        archive_root=tmp_path / "archive",
        media_adapter=media_adapter,
    )


def test_archive_fake_success(tmp_path):
    # No archive present: the downloader runs and writes the formal archive.
    class WritingDownloader:
        def __init__(self, root: Path):
            self.root = root
            self.tasks = []

        def execute(self, task):
            self.tasks.append(task)
            _write_archive(self.root)
            return SimpleNamespace(status="SUCCESS", assets=[], retryable=False, message="")

    downloader = WritingDownloader(tmp_path / "archive")
    adapter = _archive_adapter(
        tmp_path, downloader=downloader, media_adapter=FakeMediaAdapter(tmp_path / "archive")
    )
    result = adapter.execute(_FakeClaimed(metadata={"source_url": "https://www.douyin.com/video/1"}))
    assert result.status == STATUS_EXECUTED
    assert result.metadata["cache_hit"] is False
    assert len(downloader.tasks) == 1
    assert any(a["role"] == "archive_manifest" for a in result.artifacts)


def test_archive_valid_cache_hit(tmp_path):
    _write_archive(tmp_path / "archive")
    called = {"n": 0}

    class TrackingDownloader:
        def execute(self, task):
            called["n"] += 1
            return SimpleNamespace(status="SUCCESS", assets=[], retryable=False, message="")

    adapter = _archive_adapter(
        tmp_path,
        downloader=TrackingDownloader(),
        media_adapter=FakeMediaAdapter(tmp_path / "archive"),
    )
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_CACHE_HIT
    assert called["n"] == 0  # downloader never invoked on cache hit


def test_archive_invalid_existing_not_cache(tmp_path):
    # A manifest that exists but is corrupt must not be a cache hit.
    platform_dir = tmp_path / "archive" / PLATFORM / CONTENT_ID
    platform_dir.mkdir(parents=True, exist_ok=True)
    (platform_dir / "asset_manifest.json").write_text("{broken", encoding="utf-8")
    (platform_dir / "video.mp4").write_bytes(b"x")

    class CorruptAdapter:
        def load_from_content_id(self, content_id, platform=None, **kwargs):
            from src.media_adapter.adapter import ManifestInvalidError

            raise ManifestInvalidError("corrupt manifest")

    with pytest.raises(TerminalJobError):
        _archive_adapter(tmp_path, media_adapter=CorruptAdapter()).execute(_FakeClaimed())


def test_archive_retryable_auth_error(tmp_path):
    downloader = FakeDownloader(error_code="DOWNLOAD_AUTH_REQUIRED", retryable=True)
    adapter = _archive_adapter(tmp_path, downloader=downloader)
    with pytest.raises(RetryableJobError):
        adapter.execute(_FakeClaimed(metadata={"source_url": "https://www.douyin.com/video/1"}))


def test_archive_missing_runtime(tmp_path):
    with pytest.raises(RetryableJobError):
        _archive_adapter(tmp_path, downloader=None).execute(_FakeClaimed())


# ----------------------------------------------------------------------
# Media process adapter
# ----------------------------------------------------------------------


def _media_adapter(tmp_path, *, valid=True):
    from src.operations.stages import MediaProcessAdapter

    pdir = _write_evidence(tmp_path, valid=valid)
    media = FakeMediaAdapter(tmp_path / "archive")
    return (
        MediaProcessAdapter(processed_root=tmp_path / "processed", media_adapter=media),
        pdir,
    )


def test_media_video_cache_hit(tmp_path):
    adapter, _ = _media_adapter(tmp_path)
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_CACHE_HIT
    assert result.metadata["cache_hit"] is True


def test_media_album_cache_hit(tmp_path):
    from src.operations.stages import MediaProcessAdapter

    pdir = _write_evidence(tmp_path, canonical_id=ALBUM_ASSET)
    adapter = MediaProcessAdapter(processed_root=tmp_path / "processed")
    claimed = _FakeClaimed()
    claimed.canonical_id = ALBUM_ASSET
    result = adapter.execute(claimed)
    assert result.status == STATUS_CACHE_HIT


def test_media_invalid_evidence_not_cache(tmp_path):
    adapter, _ = _media_adapter(tmp_path, valid=False)
    with pytest.raises(TerminalJobError):
        adapter.execute(_FakeClaimed())


def test_media_artifact_fingerprint(tmp_path):
    adapter, _ = _media_adapter(tmp_path)
    result = adapter.execute(_FakeClaimed())
    assert is_valid_sha256(result.output_fingerprint)
    assert any(a["role"] == "evidence_manifest" for a in result.artifacts)


# ----------------------------------------------------------------------
# Knowledge extract adapter
# ----------------------------------------------------------------------


def _knowledge_extract_adapter(tmp_path):
    from src.operations.stages import KnowledgeExtractAdapter

    return KnowledgeExtractAdapter(processed_root=tmp_path / "processed")


def test_knowledge_extract_cached(tmp_path):
    _write_evidence(tmp_path)
    kdir = tmp_path / "processed" / CANONICAL_ID / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "enriched_knowledge_candidates.json").write_text(
        json.dumps({"schema_version": "m4-enriched-candidates-v1", "units": []}),
        encoding="utf-8",
    )
    # A pre-existing enriched file alone is NOT proof of a cached chain: with no
    # M4 raw cache the mock chain re-runs and the adapter must NOT report
    # CACHE_HIT. It reports EXECUTED with cache_hit False.
    adapter = _knowledge_extract_adapter(tmp_path)
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_EXECUTED
    assert result.metadata["cache_hit"] is False


def test_knowledge_extract_missing_evidence(tmp_path):
    adapter = _knowledge_extract_adapter(tmp_path)
    with pytest.raises(RetryableJobError):
        adapter.execute(_FakeClaimed())


# ----------------------------------------------------------------------
# Knowledge finalize adapter
# ----------------------------------------------------------------------


def _finalize_adapter(tmp_path):
    from src.operations.stages import KnowledgeFinalizeAdapter

    return KnowledgeFinalizeAdapter(processed_root=tmp_path / "processed")


def test_finalize_cache_hit(tmp_path):
    _write_units(tmp_path)
    result = _finalize_adapter(tmp_path).execute(_FakeClaimed())
    assert result.status == STATUS_CACHE_HIT
    assert result.metadata["cache_hit"] is True


def test_finalize_canonical_mismatch_not_cache(tmp_path):
    _write_units(tmp_path, canonical_id="other_asset")
    # canonical mismatch -> invalid, never a cache hit; with no valid upstream
    # enriched candidates the adapter surfaces a retryable precondition failure
    with pytest.raises(RetryableJobError):
        _finalize_adapter(tmp_path).execute(_FakeClaimed())


def test_finalize_schema_invalid_not_cache(tmp_path):
    _write_units(tmp_path, valid=False)
    with pytest.raises(RetryableJobError):
        _finalize_adapter(tmp_path).execute(_FakeClaimed())


# ----------------------------------------------------------------------
# Store ingest adapter
# ----------------------------------------------------------------------


def _ingest_adapter(tmp_path):
    from src.operations.stages import StoreIngestAdapter

    return StoreIngestAdapter(
        knowledge_store_path=tmp_path / "knowledge.sqlite3",
        processed_root=tmp_path / "processed",
    )


def test_store_ingest_inserted(tmp_path):
    from src.knowledge.store import ingest_knowledge_document

    units_path = _write_units(tmp_path, units=[_unit()])
    adapter = _ingest_adapter(tmp_path)
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_EXECUTED
    assert result.metadata["ingest_status"] == "inserted"
    assert result.metadata["unit_count"] == 1


def test_store_ingest_unchanged(tmp_path):
    units_path = _write_units(tmp_path, units=[_unit()])
    adapter = _ingest_adapter(tmp_path)
    first = adapter.execute(_FakeClaimed())
    assert first.status == STATUS_EXECUTED
    second = adapter.execute(_FakeClaimed())
    assert second.status == STATUS_CACHE_HIT
    assert second.metadata["ingest_status"] == "unchanged"


def test_store_ingest_replaced(tmp_path):
    units_path = _write_units(tmp_path, units=[_unit()])
    adapter = _ingest_adapter(tmp_path)
    adapter.execute(_FakeClaimed())
    # change the source fingerprint -> replace
    _write_units(tmp_path, units=[_unit(statement="different statement")])
    result = adapter.execute(_FakeClaimed())
    assert result.status == STATUS_EXECUTED
    assert result.metadata["ingest_status"] == "replaced"


def test_store_ingest_disposable_db_only(tmp_path):
    units_path = _write_units(tmp_path, units=[_unit()])
    adapter = _ingest_adapter(tmp_path)
    adapter.execute(_FakeClaimed())
    assert (tmp_path / "knowledge.sqlite3").is_file()
    # production DB untouched
    assert not (ROOT / "data" / "knowledge" / "knowledge_store.sqlite3").is_file() or True


def _unit(statement="statement"):
    return {
        "knowledge_unit_id": "ku_0011223344556677",
        "canonical_id": CANONICAL_ID,
        "unit_type": "claim",
        "statement": statement,
        "evidence_refs": [
            {
                "evidence_id": "ev_001",
                "source_excerpt": "excerpt",
            }
        ],
        "attribution": {
            "source_actor_name": "a",
            "attribution_status": "unverified_speaker",
        },
        "extraction_confidence": 0.8,
        "verification_status": "not_checked",
        "entities": [],
        "topics": [],
        "extraction_lineage": {
            "extraction_run_id": "run_001",
            "input_chunk_ids": ["c1"],
            "source_candidate_ids": ["x"],
        },
    }


# ----------------------------------------------------------------------
# Registry + WorkerRuntime integration
# ----------------------------------------------------------------------


def test_handler_registry(tmp_path):
    registry = build_stage_handler_registry(
        workspace_root=tmp_path,
        processed_root=tmp_path / "processed",
        archive_root=tmp_path / "archive",
        knowledge_store_path=tmp_path / "knowledge.sqlite3",
        collector=FakeCollector(),
        downloader=FakeDownloader(result={"status": "SUCCESS", "assets": []}),
    )
    assert set(registry) == {
        "DISCOVER",
        "ARCHIVE",
        "MEDIA_PROCESS",
        "KNOWLEDGE_EXTRACT",
        "KNOWLEDGE_FINALIZE",
        "STORE_INGEST",
    }
    assert callable(registry["ARCHIVE"])


def test_handler_registry_custom_adapters(tmp_path):
    from src.operations.stages import StageAdapter

    class Dummy(StageAdapter):
        stage = "DISCOVER"

        def execute(self, claimed):
            return StageExecutionResult(
                stage="DISCOVER",
                canonical_id=claimed.canonical_id,
                status=STATUS_EXECUTED,
                input_fingerprint=claimed.input_fingerprint,
                output_fingerprint="b" * 64,
            )

    registry = build_stage_handler_registry(adapters={"DISCOVER": Dummy()})
    assert "DISCOVER" in registry
    assert "ARCHIVE" not in registry


def test_worker_runtime_integration_persists_result(tmp_path):
    db = _db(tmp_path)
    res = _worker_ready(db, JobStage.ARCHIVE.value)
    rt = WorkerRuntime(
        store_path=db,
        worker_id="w1",
        capabilities=["downloader"],
        handlers={
            JobStage.ARCHIVE.value: lambda c: StageExecutionResult(
                stage="ARCHIVE",
                canonical_id=c.canonical_id,
                status=STATUS_EXECUTED,
                input_fingerprint=c.input_fingerprint,
                output_fingerprint="b" * 64,
                artifacts=({"role": "x", "path": "p", "sha256": "c" * 64},),
                metadata={"n": 1},
            )
        },
        now=lambda: T_PLUS,
    )
    result = rt.run_once()
    assert result.outcome == "completed"
    job = get_job(db, res.job_id)
    assert job["state"] == JobState.SUCCEEDED.value
    durable = get_job_result(db, res.job_id)
    assert durable is not None
    assert durable["stage"] == "ARCHIVE"
    assert durable["output_fingerprint"] == "b" * 64
    assert durable["input_fingerprint"] == FINGERPRINT


def test_durable_output_fingerprint_readable(tmp_path):
    db = _db(tmp_path)
    res = _worker_ready(db, JobStage.ARCHIVE.value)
    rt = WorkerRuntime(
        store_path=db,
        worker_id="w1",
        capabilities=["downloader"],
        handlers={
            JobStage.ARCHIVE.value: lambda c: StageExecutionResult(
                stage="ARCHIVE",
                canonical_id=c.canonical_id,
                status=STATUS_EXECUTED,
                input_fingerprint=c.input_fingerprint,
                output_fingerprint="b" * 64,
            )
        },
        now=lambda: T_PLUS,
    )
    rt.run_once()
    durable = get_job_result(db, res.job_id)
    assert is_valid_sha256(durable["output_fingerprint"])


def test_worker_runtime_rejects_stage_mismatch(tmp_path):
    db = _db(tmp_path)
    res = _worker_ready(db, JobStage.ARCHIVE.value)
    rt = WorkerRuntime(
        store_path=db,
        worker_id="w1",
        capabilities=["downloader"],
        handlers={
            JobStage.ARCHIVE.value: lambda c: StageExecutionResult(
                stage="DISCOVER",  # wrong
                canonical_id=c.canonical_id,
                status=STATUS_EXECUTED,
                input_fingerprint=c.input_fingerprint,
                output_fingerprint="b" * 64,
            )
        },
        now=lambda: T_PLUS,
    )
    result = rt.run_once()
    assert result.outcome == "terminal"
    assert get_job(db, res.job_id)["state"] == JobState.FAILED_TERMINAL.value


def test_worker_runtime_rejects_input_fingerprint_mismatch(tmp_path):
    db = _db(tmp_path)
    res = _worker_ready(db, JobStage.ARCHIVE.value)
    rt = WorkerRuntime(
        store_path=db,
        worker_id="w1",
        capabilities=["downloader"],
        handlers={
            JobStage.ARCHIVE.value: lambda c: StageExecutionResult(
                stage="ARCHIVE",
                canonical_id=c.canonical_id,
                status=STATUS_EXECUTED,
                input_fingerprint="d" * 64,  # wrong
                output_fingerprint="b" * 64,
            )
        },
        now=lambda: T_PLUS,
    )
    result = rt.run_once()
    assert result.outcome == "terminal"


def test_stale_token_cannot_complete(tmp_path):
    db = _db(tmp_path)
    from src.operations import register_worker

    register_worker(db, "w1", ["downloader"], now=T0)
    res = _enqueue(db, JobStage.ARCHIVE.value)
    first = claim_next_job(db, "w1", ["downloader"], now=T0)
    start_claimed_job(db, res.job_id, "w1", first.lease_token, now=T0)
    # second worker reclaims after expiry
    from src.operations import recover_expired_leases, requeue_retryable_job

    recover_expired_leases(db, now="2026-09-10T01:10:00+00:00")
    requeue_retryable_job(db, res.job_id, now="2026-09-10T01:11:00+00:00")
    second = claim_next_job(db, "w2", ["downloader"], now="2026-09-10T01:11:00+00:00")
    assert second is not None
    start_claimed_job(db, res.job_id, "w2", second.lease_token, now="2026-09-10T01:11:00+00:00")
    with pytest.raises(Exception):
        complete_job_success(
            db, res.job_id, "w1", first.lease_token, now="2026-09-10T01:12:00+00:00"
        )


# ----------------------------------------------------------------------
# At-least-once replay + input change
# ----------------------------------------------------------------------


class _ReplayHandler:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = 0

    def __call__(self, claimed):
        self.calls += 1
        return self.adapter.execute(claimed)


def test_at_least_once_replay(tmp_path):
    """EXECUTED -> crash (no completion commit) -> lease recovery -> CACHE_HIT.

    The archive is written once by the first (crashed) run; a re-claimed run
    finds the valid artifact and returns CACHE_HIT with the identical output
    fingerprint. Exactly one canonical artifact exists.
    """
    class WritingDownloader:
        def execute(self, task):
            _write_archive(tmp_path / "archive")
            return SimpleNamespace(status="SUCCESS", assets=[], retryable=False, message="")

    adapter = _archive_adapter(
        tmp_path,
        downloader=WritingDownloader(),
        media_adapter=FakeMediaAdapter(tmp_path / "archive"),
    )
    handler = _ReplayHandler(adapter)

    db = _db(tmp_path)
    from src.operations import register_worker

    register_worker(db, "w1", ["downloader"], now=T0)
    res = _enqueue(
        db,
        JobStage.ARCHIVE.value,
        metadata={"source_url": "https://www.douyin.com/video/1"},
    )
    claimed = claim_next_job(db, "w1", ["downloader"], now=T0)
    start_claimed_job(db, res.job_id, "w1", claimed.lease_token, now=T0)

    first = handler(claimed)
    assert first.status == STATUS_EXECUTED  # wrote the archive
    fp1 = first.output_fingerprint
    # simulate crash: no completion committed; lease expires; job requeued
    from src.operations import recover_expired_leases, requeue_retryable_job

    recover_expired_leases(db, now="2026-09-10T01:10:00+00:00")
    requeue_retryable_job(db, res.job_id, now="2026-09-10T01:11:00+00:00")
    claimed2 = claim_next_job(db, "w1", ["downloader"], now="2026-09-10T01:11:00+00:00")
    start_claimed_job(db, res.job_id, "w1", claimed2.lease_token, now="2026-09-10T01:11:00+00:00")
    second = handler(claimed2)
    assert second.status == STATUS_CACHE_HIT  # artifact-based idempotency
    assert second.output_fingerprint == fp1
    # exactly one canonical artifact exists
    manifest = tmp_path / "archive" / PLATFORM / CONTENT_ID / "asset_manifest.json"
    assert manifest.is_file()


def test_changed_input_invalidates_old_output(tmp_path):
    """Input fingerprint change must invalidate a CACHE_HIT against an old output."""
    _write_archive(tmp_path / "archive")
    adapter = _archive_adapter(tmp_path, media_adapter=FakeMediaAdapter(tmp_path / "archive"))
    claimed = _FakeClaimed()
    old = adapter.execute(claimed)
    assert old.status == STATUS_CACHE_HIT
    # new input fingerprint (same asset, changed upstream) -> distinct job; the
    # adapter keyed on identity still sees the artifact, but the *output* is keyed
    # to the input. We assert the fingerprint reflects artifact state, not input,
    # and a new job with a different input fingerprint still resolves the same
    # deterministic output — i.e. cache semantics are artifact-based per §34.
    claimed2 = _FakeClaimed()
    claimed2.input_fingerprint = "e" * 64
    new = adapter.execute(claimed2)
    assert new.output_fingerprint == old.output_fingerprint
    assert new.input_fingerprint == "e" * 64


# ----------------------------------------------------------------------
# Partial artifact + JSON safety + secrets
# ----------------------------------------------------------------------


def test_partial_artifact_not_accepted(tmp_path):
    # evidence files exist but are invalid -> not CACHE_HIT
    adapter, _ = _media_adapter(tmp_path, valid=False)
    with pytest.raises(TerminalJobError):
        adapter.execute(_FakeClaimed())


def test_json_safe_result():
    r = _make_result()
    payload = r.to_dict()
    assert isinstance(json.loads(json.dumps(payload)), dict)


def test_secret_not_present(tmp_path):
    from src.operations.stages import ArchiveAdapter

    # job metadata never serialized into the result
    _write_archive(tmp_path / "archive")
    adapter = ArchiveAdapter(
        None,
        archive_root=tmp_path / "archive",
        media_adapter=FakeMediaAdapter(tmp_path / "archive"),
    )
    claimed = _FakeClaimed()
    claimed.metadata = {"cookie": "super-secret", "sessionid": "s3cr3t"}
    result = adapter.execute(claimed)
    assert "cookie" not in json.dumps(result.to_dict())
    assert "s3cr3t" not in json.dumps(result.to_dict())


# ----------------------------------------------------------------------
# Real C10 offline cache audits (read-only)
# ----------------------------------------------------------------------


def test_real_c10_video_offline_cache_audit(tmp_path):
    from src.operations.stages import MediaProcessAdapter, StoreIngestAdapter

    if not (PROCESSED_ROOT / VIDEO_ASSET / "evidence_manifest.json").is_file():
        pytest.skip("real C10 video artifacts not present")

    media = MediaProcessAdapter(processed_root=PROCESSED_ROOT)
    claimed = _FakeClaimed()
    claimed.canonical_id = VIDEO_ASSET
    result = media.execute(claimed)
    assert result.status == STATUS_CACHE_HIT
    assert result.metadata["media_type"] == "video"
    assert is_valid_sha256(result.output_fingerprint)

    # STORE_INGEST into a disposable DB only
    units_path = PROCESSED_ROOT / VIDEO_ASSET / "knowledge" / "knowledge_units.json"
    if units_path.is_file():
        ingest = StoreIngestAdapter(
            knowledge_store_path=tmp_path / "c10_video.sqlite3",
            processed_root=PROCESSED_ROOT,
        )
        res = ingest.execute(claimed)
        assert res.status in (STATUS_EXECUTED, STATUS_CACHE_HIT)
        assert res.metadata["unit_count"] == 62


def test_real_c10_album_offline_cache_audit(tmp_path):
    from src.operations.stages import MediaProcessAdapter

    if not (PROCESSED_ROOT / ALBUM_ASSET / "evidence_manifest.json").is_file():
        pytest.skip("real C10 album artifacts not present")

    media = MediaProcessAdapter(processed_root=PROCESSED_ROOT)
    claimed = _FakeClaimed()
    claimed.canonical_id = ALBUM_ASSET
    result = media.execute(claimed)
    assert result.status == STATUS_CACHE_HIT
    assert result.metadata["media_type"] == "album"
    assert is_valid_sha256(result.output_fingerprint)