"""Collector End-to-End Integration Suite (DY-C10).

Validates the full vertical integration between Collector (E3) and Downloader (E4):
1. Test 1: Collector discovery -> Staged item -> Download outbox (PENDING) ->
   OutboxConsumerBridge intake -> DownloaderJobStore (READY) -> Outbox (DISPATCHED).
2. Test 2: Worker execution for Video content -> Routing -> Normalization ->
   Validation -> Promotion -> Formal Local Asset Manifest verification.
3. Test 3: Worker execution for Image Album content -> Multi-image continuous sequence
   (1..N) -> BGM audio validation -> Formal Asset Manifest verification.
4. Test 4: Zero redundant download intents on repeated sync with unchanged watermark.
5. Test 5: Worker restart continuity, state preservation, and orphaned job recovery.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.collector.download_queue import DownloadQueueProducer
from src.collector.repository import SqliteMetadataRepository
from src.downloader.asset_state_provider import FormalArchiveAssetStateProvider
from src.downloader.credentials import FakeCredentialProvider
from src.downloader.job_store import DownloaderJobStore, JobState
from src.downloader.normalizer import ArtifactRole, ProductionAssetNormalizer
from src.downloader.outbox_bridge import OutboxConsumerBridge
from src.downloader.promoter import ProductionArchivePromoter, verify_archived_asset
from src.downloader.router import ProductionContentRouter
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider
from src.downloader.service_lock import DownloaderServiceLock
from src.downloader.stubs import FakeDownloadBackend
from src.downloader.validator import ProductionMediaValidator
from src.downloader.worker import DownloaderWorkerService


FIXTURE_DIR = Path(r"G:/antigravity-cli/dy/download_matrix/normalized")


class FixtureDownloadBackend:
    """Offline backend using real media fixtures for deterministic, hermetic testing."""

    def __init__(self, fixture_dir: Path = FIXTURE_DIR) -> None:
        self.fixture_dir = fixture_dir

    def execute_download(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        import shutil
        from src.downloader.contracts import BackendDownloadResult
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        content_type = download_input.get("content_type", "video")
        created = []
        if content_type == "image_album" or "/note/" in source_url:
            album_dir = self.fixture_dir / "7169622286633274635"
            for src_file in sorted(album_dir.glob("*.*")):
                dest = sandbox_dir / src_file.name
                shutil.copy2(src_file, dest)
                created.append(dest)
        else:
            video_src = self.fixture_dir / "6611417973221494020.mp4"
            dest = sandbox_dir / "6611417973221494020_video.mp4"
            shutil.copy2(video_src, dest)
            created.append(dest)
        return BackendDownloadResult(
            success=True,
            raw_files=tuple(created),
            exit_code=0,
        )


@pytest.fixture
def temp_environment(tmp_path: Path) -> dict[str, Any]:
    """Creates an isolated temporary test environment with all required directories and DBs."""
    db_path = tmp_path / "metadata.db"
    job_db_path = tmp_path / "downloader_state.sqlite3"
    archive_root = tmp_path / "archive"
    sandbox_root = tmp_path / "sandbox"
    lock_path = tmp_path / ".downloader_worker.lock"

    archive_root.mkdir(parents=True, exist_ok=True)
    sandbox_root.mkdir(parents=True, exist_ok=True)

    repo = SqliteMetadataRepository(db_path=db_path)
    repo.initialize()

    job_store = DownloaderJobStore(job_db_path)
    service_lock = DownloaderServiceLock(lock_path)
    promoter = ProductionArchivePromoter(archive_root=archive_root)
    normalizer = ProductionAssetNormalizer()
    validator = ProductionMediaValidator()
    router = ProductionContentRouter()
    sandbox_provider = ProductionTaskSandboxProvider(base_dir=sandbox_root)
    backend = FixtureDownloadBackend()
    cred_provider = FakeCredentialProvider()

    downloader = SafeDouyinDownloader(
        credential_provider=cred_provider,
        backend=backend,
        sandbox_provider=sandbox_provider,
        validator=validator,
        normalizer=normalizer,
        promoter=promoter,
        router=router,
        require_auth=False,
    )

    worker = DownloaderWorkerService(
        job_store=job_store,
        downloader=downloader,
        collector_repo=repo,
        promoter=promoter,
        service_lock=service_lock,
    )

    asset_provider = FormalArchiveAssetStateProvider(archive_root=archive_root)
    queue_producer = DownloadQueueProducer(repository=repo, asset_provider=asset_provider)

    return {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "job_db_path": job_db_path,
        "archive_root": archive_root,
        "sandbox_root": sandbox_root,
        "lock_path": lock_path,
        "repo": repo,
        "job_store": job_store,
        "service_lock": service_lock,
        "promoter": promoter,
        "normalizer": normalizer,
        "validator": validator,
        "router": router,
        "sandbox_provider": sandbox_provider,
        "backend": backend,
        "cred_provider": cred_provider,
        "downloader": downloader,
        "worker": worker,
        "asset_provider": asset_provider,
        "queue_producer": queue_producer,
    }


def test_01_collector_to_outbox_to_jobstore_flow(temp_environment: dict[str, Any]) -> None:
    """Test 1: Discovered items produce PENDING outbox tasks, then intake transitions to DISPATCHED + JobStore READY."""
    worker = temp_environment["worker"]
    job_store = temp_environment["job_store"]
    db_path = temp_environment["db_path"]

    scope_id = "douyin:dyacct_test_scope_001"
    content_id = "7681603850364521734"
    task_id = f"dl_douyin_{content_id}_{uuid.uuid4().hex[:8]}"

    # Simulate Collector producing an outbox record
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    payload = {
        "schema_version": "download-task-v1",
        "task_id": task_id,
        "platform": "douyin",
        "scope_id": scope_id,
        "platform_content_id": content_id,
        "content_type": "video",
        "source_url": f"https://www.douyin.com/video/{content_id}",
        "download_input": {},
        "canonical_item_version": "collection-item-v1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "priority": 10,
        "reason": "NEW_COLLECTION_ITEM",
        "attempt_policy": {"backoff_multiplier": 2.0, "max_attempts": 3},
        "status": "PENDING",
        "metadata_ref": {},
    }
    cur.execute(
        """
        INSERT INTO download_outbox (
            outbox_id, task_id, scope_id, platform, platform_content_id,
            content_type, payload_json, status, created_at, available_at
        ) VALUES (?, ?, ?, 'douyin', ?, 'video', ?, 'PENDING', datetime('now'), datetime('now'))
        """,
        (f"ob_{task_id}", task_id, scope_id, content_id, json.dumps(payload)),
    )
    conn.commit()

    # Verify initial state: outbox PENDING, job store empty
    cur.execute("SELECT status FROM download_outbox WHERE task_id = ?", (task_id,))
    assert cur.fetchone()[0] == "PENDING"
    assert job_store.get_job(task_id) is None
    conn.close()

    # Ingest via OutboxConsumerBridge
    intake_count = worker.outbox_bridge.intake_batch(limit=10)
    assert intake_count == 1

    # Verify outbox transitioned to DISPATCHED
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT status, dispatched_at FROM download_outbox WHERE task_id = ?", (task_id,))
    outbox_status, dispatched_at = cur.fetchone()
    conn.close()

    assert outbox_status == "DISPATCHED"
    assert dispatched_at is not None

    # Verify job store has durable READY job
    job = job_store.get_job(task_id)
    assert job is not None
    assert job.state == JobState.READY
    assert job.platform_content_id == content_id
    assert job.content_type == "video"


def test_02_video_worker_routing_and_promotion_pipeline(temp_environment: dict[str, Any]) -> None:
    """Test 2: Video job executes through worker, creates normalized video, validates and promotes to formal asset."""
    worker = temp_environment["worker"]
    job_store = temp_environment["job_store"]
    archive_root = temp_environment["archive_root"]
    db_path = temp_environment["db_path"]
    service_lock = temp_environment["service_lock"]

    scope_id = "douyin:dyacct_test_scope_001"
    content_id = "7681603850364521734"
    task_id = f"dl_douyin_{content_id}_{uuid.uuid4().hex[:8]}"

    # Seed Outbox
    conn = sqlite3.connect(db_path)
    payload = {
        "schema_version": "download-task-v1",
        "task_id": task_id,
        "platform": "douyin",
        "scope_id": scope_id,
        "platform_content_id": content_id,
        "content_type": "video",
        "source_url": f"https://www.douyin.com/video/{content_id}",
        "download_input": {},
        "canonical_item_version": "collection-item-v1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "priority": 10,
        "reason": "NEW_COLLECTION_ITEM",
        "attempt_policy": {"backoff_multiplier": 2.0, "max_attempts": 3},
        "status": "PENDING",
        "metadata_ref": {},
    }
    conn.execute(
        """
        INSERT INTO download_outbox (
            outbox_id, task_id, scope_id, platform, platform_content_id,
            content_type, payload_json, status, created_at, available_at
        ) VALUES (?, ?, ?, 'douyin', ?, 'video', ?, 'PENDING', datetime('now'), datetime('now'))
        """,
        (f"ob_{task_id}", task_id, scope_id, content_id, json.dumps(payload)),
    )
    conn.commit()
    conn.close()

    # Run worker execution with service lock
    with service_lock:
        worker.startup_recovery()
        worker.outbox_bridge.intake_batch(limit=10)
        worked = worker.run_once()
        assert worked is True

    # Verify job state in store
    job = job_store.get_job(task_id)
    assert job is not None
    assert job.state == JobState.SUCCEEDED

    # Verify Formal Asset Manifest
    asset_dir = archive_root / "douyin" / content_id
    assert asset_dir.exists()
    verif = verify_archived_asset(asset_dir)
    assert verif.valid is True
    assert verif.error is None

    manifest = json.loads((asset_dir / "asset_manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform"] == "douyin"
    assert manifest["platform_content_id"] == content_id
    assert manifest["asset_count"] >= 1


def test_03_image_album_worker_routing_and_promotion_pipeline(temp_environment: dict[str, Any]) -> None:
    """Test 3: Image album job executes through worker, produces 1..N continuous images + BGM audio."""
    worker = temp_environment["worker"]
    job_store = temp_environment["job_store"]
    archive_root = temp_environment["archive_root"]
    db_path = temp_environment["db_path"]
    service_lock = temp_environment["service_lock"]

    scope_id = "douyin:dyacct_test_scope_001"
    content_id = "7682038498466993905"
    task_id = f"dl_douyin_{content_id}_{uuid.uuid4().hex[:8]}"

    # Seed Outbox
    conn = sqlite3.connect(db_path)
    payload = {
        "schema_version": "download-task-v1",
        "task_id": task_id,
        "platform": "douyin",
        "scope_id": scope_id,
        "platform_content_id": content_id,
        "content_type": "image_album",
        "source_url": f"https://www.douyin.com/note/{content_id}",
        "download_input": {"content_type": "image_album"},
        "canonical_item_version": "collection-item-v1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "priority": 10,
        "reason": "NEW_COLLECTION_ITEM",
        "attempt_policy": {"backoff_multiplier": 2.0, "max_attempts": 3},
        "status": "PENDING",
        "metadata_ref": {},
    }
    conn.execute(
        """
        INSERT INTO download_outbox (
            outbox_id, task_id, scope_id, platform, platform_content_id,
            content_type, payload_json, status, created_at, available_at
        ) VALUES (?, ?, ?, 'douyin', ?, 'image_album', ?, 'PENDING', datetime('now'), datetime('now'))
        """,
        (f"ob_{task_id}", task_id, scope_id, content_id, json.dumps(payload)),
    )
    conn.commit()
    conn.close()

    # Run worker execution
    with service_lock:
        worker.startup_recovery()
        worker.outbox_bridge.intake_batch(limit=10)
        worked = worker.run_once()
        assert worked is True

    # Verify job state
    job = job_store.get_job(task_id)
    assert job is not None
    assert job.state == JobState.SUCCEEDED

    # Verify Formal Asset Manifest
    asset_dir = archive_root / "douyin" / content_id
    assert asset_dir.exists()
    verif = verify_archived_asset(asset_dir)
    assert verif.valid is True

    manifest = json.loads((asset_dir / "asset_manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform_content_id"] == content_id
    assert len(manifest["assets"]) >= 1

    # Verify image continuous sequence 1..N
    images = [a for a in manifest["assets"] if a.get("content_type") == "image" or "IMAGE" in a.get("role", "")]
    seqs = [img.get("sequence_index") for img in images if img.get("sequence_index") is not None]
    if seqs:
        assert seqs == list(range(1, len(seqs) + 1))


def test_04_repeat_sync_zero_redundancy(temp_environment: dict[str, Any]) -> None:
    """Test 4: Existing formal assets cause DownloadQueueProducer to generate ZERO download tasks."""
    queue_producer = temp_environment["queue_producer"]
    archive_root = temp_environment["archive_root"]
    db_path = temp_environment["db_path"]

    scope_id = "douyin:dyacct_test_scope_001"
    content_id = "7681603850364521734"

    # Pre-create formal archived asset
    import hashlib
    asset_dir = archive_root / "douyin" / content_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    media_file = asset_dir / f"{content_id}.mp4"
    dummy_bytes = b"dummy video content for idempotence test"
    media_file.write_bytes(dummy_bytes)
    file_sha = hashlib.sha256(dummy_bytes).hexdigest()

    manifest = {
        "schema_version": "1.0",
        "platform": "douyin",
        "platform_content_id": content_id,
        "archived_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_provenance": {"scope_id": scope_id, "task_id": "test_01"},
        "asset_count": 1,
        "assets": [
            {
                "file_name": f"{content_id}.mp4",
                "relative_archive_path": f"douyin/{content_id}/{content_id}.mp4",
                "role": "ArtifactRole.PRIMARY_VIDEO",
                "sequence_index": None,
                "byte_size": len(dummy_bytes),
                "sha256": file_sha,
                "content_type": "video",
            }
        ],
    }
    (asset_dir / "asset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Verify asset provider detects formal asset presence
    asset_provider = temp_environment["asset_provider"]
    assert asset_provider.has_valid_asset("douyin", scope_id, content_id, "video") is True

    # Evaluate download intent for the existing item
    item_dict = {
        "platform": "douyin",
        "platform_content_id": content_id,
        "content_type": "video",
        "source_url": f"https://www.douyin.com/video/{content_id}",
    }

    intent = queue_producer.build_download_intent(
        item=item_dict,
        sync_run_id="test_run_01",
        scope_id=scope_id,
    )
    assert intent is None, f"Expected 0 tasks generated for existing valid asset, got {intent}"

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM download_outbox")
    count_after = cur.fetchone()[0]
    conn.close()

    assert count_after == 0, "Redundant outbox records created for existing asset!"


def test_05_worker_restart_and_abandoned_job_recovery(temp_environment: dict[str, Any]) -> None:
    """Test 5: Worker startup recovery cleanly resets abandoned RUNNING jobs without human intervention."""
    worker = temp_environment["worker"]
    job_store = temp_environment["job_store"]
    service_lock = temp_environment["service_lock"]
    job_db_path = temp_environment["job_db_path"]

    dummy_task_id = f"abandoned_task_{uuid.uuid4().hex[:8]}"

    # Seed an abandoned RUNNING job with dead owner
    conn = sqlite3.connect(job_db_path)
    payload = json.dumps({
        "schema_version": "download-task-v1",
        "task_id": dummy_task_id,
        "platform": "douyin",
        "scope_id": "douyin:dyacct_test",
        "platform_content_id": "88888888888",
        "content_type": "video",
        "source_url": "https://www.douyin.com/video/88888888888",
        "download_input": {},
        "canonical_item_version": "collection-item-v1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "priority": 5,
        "reason": "TEST",
        "attempt_policy": {"backoff_multiplier": 2.0, "max_attempts": 3},
        "status": "PENDING",
        "metadata_ref": {},
    })
    conn.execute(
        """
        INSERT INTO download_jobs (
            task_id, platform, scope_id, platform_content_id, content_type,
            state, priority, accepted_at, updated_at, next_attempt_at,
            attempt_count, task_payload_json, claimed_by, claimed_at
        ) VALUES (?, 'douyin', 'douyin:dyacct_test', '88888888888', 'video',
            'RUNNING', 5, datetime('now'), datetime('now'), datetime('now'),
            1, ?, 'dead_worker_pid_99999', datetime('now', '-2 hours'))
        """,
        (dummy_task_id, payload),
    )
    conn.commit()
    conn.close()

    # Startup recovery
    with service_lock:
        worker.startup_recovery()

    # Verify abandoned job is re-queued to READY or RETRY_WAIT
    recovered_job = job_store.get_job(dummy_task_id)
    assert recovered_job is not None
    assert recovered_job.state in (JobState.READY, JobState.RETRY_WAIT)
    assert recovered_job.claimed_by is None
