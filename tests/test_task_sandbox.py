"""Comprehensive Unit and Component Tests for UUID Task Sandbox & Orphan GC (DY-D04).

Validates:
1. Production TaskSandbox & TaskSandboxProvider lifecycle and layout.
2. Unique execution_id (UUID4) vs. logical task_id (C09).
3. Metadata atomic persistence and strict secret rejection firewall.
4. Path containment firewall: rejection of ../, absolute path escape, outside artifacts.
5. Multi-sandbox concurrency (20 parallel sandboxes) and artifact isolation.
6. Retention policies: DELETE_ON_SUCCESS, PRESERVE_ON_FAILURE, DELETE_ON_FAILURE, PRESERVE_ALWAYS.
7. Multi-factor orphan detection: alive owner PID protection (even past TTL), crash recovery, TTL GC.
8. Safe quarantine of corrupt/unrecognized directories (zero indiscriminate rmtree).
9. Integration with SafeDouyinDownloader (DY-D01) and final archive isolation firewall.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from src.collector.download_models import DownloadTask
from src.downloader.contracts import (
    DownloaderStatus,
    ExecutionStage,
)
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import (
    GcSummary,
    OrphanRecord,
    ProductionTaskSandboxProvider,
    SandboxDiskSpaceError,
    SandboxMetadataCorruptError,
    SandboxPathEscapeError,
    SandboxRetentionPolicy,
    SandboxState,
    TaskSandbox,
    TaskSandboxMetadata,
    is_pid_alive,
)
from src.downloader.stubs import FakeDownloadBackend


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_sandbox_base(tmp_path: Path) -> Path:
    """Provides an isolated root directory for sandbox tests."""
    base = tmp_path / "sandbox_base"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def provider(temp_sandbox_base: Path) -> ProductionTaskSandboxProvider:
    return ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.PRESERVE_ON_FAILURE,
        success_ttl_seconds=300.0,
        failure_ttl_seconds=86400.0,
        orphan_grace_period_seconds=600.0,
    )


@pytest.fixture
def sample_task() -> DownloadTask:
    return DownloadTask(
        task_id="dl_douyin_7671141177986518318_a548f124e58e8e6a",
        platform="douyin",
        scope_id="douyin:dyacct_test",
        platform_content_id="7671141177986518318",
        content_type="video",
        source_url="https://www.douyin.com/video/7671141177986518318",
        download_input={"play_addr_h264": "https://example.com/video.mp4"},
    )


# =============================================================================
# 1. Sandbox Creation, Layout & Metadata Tests
# =============================================================================


def test_01_create_sandbox(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 01: Creates a valid TaskSandbox instance with physical root on disk."""
    sb = provider.create_sandbox(sample_task)
    assert isinstance(sb, TaskSandbox)
    assert sb.root.is_dir()
    assert sb.task_id == sample_task.task_id
    assert len(sb.execution_id) == 32  # UUID4 hex length


def test_02_unique_execution_id(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 02: Consecutive sandboxes for identical task_id have distinct execution_ids."""
    sb1 = provider.create_sandbox(sample_task)
    sb2 = provider.create_sandbox(sample_task)
    assert sb1.task_id == sb2.task_id
    assert sb1.execution_id != sb2.execution_id
    assert sb1.root != sb2.root


def test_03_deterministic_task_association(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 03: Sandbox metadata correctly associates logical task identity and platform content ID."""
    sb = provider.create_sandbox(sample_task)
    assert sb.metadata.task_id == sample_task.task_id
    assert sb.metadata.platform == "douyin"
    assert sb.metadata.platform_content_id == sample_task.platform_content_id
    assert sb.metadata.owner_pid == os.getpid()


def test_04_safe_directory_layout(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 04: Sandbox provisions safe layout: input/, work/, output/, logs/."""
    sb = provider.create_sandbox(sample_task)
    assert sb.input_dir.is_dir()
    assert sb.work_dir.is_dir()
    assert sb.output_dir.is_dir()
    assert sb.logs_dir.is_dir()
    assert (sb.root / "sandbox.json").is_file()


def test_05_metadata_atomic_persistence(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 05: sandbox.json is created atomically and deserializes accurately."""
    sb = provider.create_sandbox(sample_task)
    meta_file = sb.root / "sandbox.json"
    meta = TaskSandboxMetadata.load(meta_file)

    assert meta.execution_id == sb.execution_id
    assert meta.task_id == sb.task_id
    assert meta.state == SandboxState.ACTIVE.value
    assert meta.owner_pid == os.getpid()


def test_06_no_secrets_in_metadata() -> None:
    """Test 06 (Security Invariant): Rejects injection of authentication secrets in metadata."""
    with pytest.raises(ValueError, match="Security violation"):
        TaskSandboxMetadata(
            execution_id="exec_1",
            task_id="dl_1",
            platform="douyin",
            platform_content_id="123",
            owner_pid=1000,
            failure_reason="Failed with sessionid=super_secret_leak_12345",
        )


# =============================================================================
# 2. Path Containment & Escape Defense Tests
# =============================================================================


def test_07_relative_containment_success(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 07: Files placed within output_dir register successfully."""
    sb = provider.create_sandbox(sample_task)
    media_file = sb.output_dir / "video.mp4"
    media_file.write_bytes(b"DATA")

    registered = sb.register_artifact(media_file)
    assert registered == media_file.resolve()
    assert "output/video.mp4" in sb.metadata.artifacts


def test_08_parent_traversal_escape_rejected(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 08: ../ path traversal outside sandbox root is strictly rejected."""
    sb = provider.create_sandbox(sample_task)
    escape_path = sb.work_dir / ".." / ".." / "escaped.mp4"

    with pytest.raises(SandboxPathEscapeError):
        sb.register_artifact(escape_path)


def test_09_absolute_escape_rejected(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask, tmp_path: Path) -> None:
    """Test 09: Absolute path pointing outside sandbox is strictly rejected."""
    sb = provider.create_sandbox(sample_task)
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"OUTSIDE")

    with pytest.raises(SandboxPathEscapeError):
        sb.register_artifact(outside_file)


def test_10_artifact_registration_persists_to_json(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 10: Registering multiple artifacts updates sandbox.json on disk."""
    sb = provider.create_sandbox(sample_task)
    f1 = sb.output_dir / "video.mp4"
    f2 = sb.output_dir / "cover.jpg"
    f1.write_bytes(b"V")
    f2.write_bytes(b"C")

    sb.register_artifact(f1)
    sb.register_artifact(f2)

    reloaded = TaskSandboxMetadata.load(sb.root / "sandbox.json")
    assert "output/video.mp4" in reloaded.artifacts
    assert "output/cover.jpg" in reloaded.artifacts


def test_11_outside_artifact_rejection_leaves_metadata_untouched(
    provider: ProductionTaskSandboxProvider, sample_task: DownloadTask, tmp_path: Path
) -> None:
    """Test 11: Failed escape attempt does not corrupt or pollute registered artifacts."""
    sb = provider.create_sandbox(sample_task)
    f1 = sb.output_dir / "valid.mp4"
    f1.write_bytes(b"V")
    sb.register_artifact(f1)

    outside = tmp_path / "malicious.exe"
    with pytest.raises(SandboxPathEscapeError):
        sb.register_artifact(outside)

    reloaded = TaskSandboxMetadata.load(sb.root / "sandbox.json")
    assert len(reloaded.artifacts) == 1
    assert reloaded.artifacts[0] == "output/valid.mp4"


def test_12_list_artifacts_current_sandbox_only(
    provider: ProductionTaskSandboxProvider, sample_task: DownloadTask
) -> None:
    """Test 12: list_artifacts returns files strictly inside the sandbox output/work folders."""
    sb = provider.create_sandbox(sample_task)
    (sb.output_dir / "vid.mp4").write_bytes(b"V")
    (sb.work_dir / "temp.ts").write_bytes(b"T")

    artifacts = sb.list_artifacts()
    names = [a.name for a in artifacts]
    assert "vid.mp4" in names
    assert "temp.ts" in names
    assert len(artifacts) == 2


# =============================================================================
# 3. Concurrency & Cross-Task Isolation Tests
# =============================================================================


def test_13_concurrent_20_sandboxes(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 13: 20 concurrent sandbox creations have distinct UUIDs and zero collisions."""
    def _create_one(i: int) -> tuple[str, Path]:
        task = DownloadTask(
            task_id=f"dl_concurrent_{i}",
            platform="douyin",
            scope_id="douyin:test",
            platform_content_id=f"id_{i}",
            content_type="video",
            source_url="https://douyin.com/1",
        )
        sb = provider.create_sandbox(task)
        (sb.output_dir / f"file_{i}.mp4").write_bytes(f"DATA_{i}".encode())
        return sb.execution_id, sb.root

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(_create_one, range(20)))

    exec_ids = [r[0] for r in results]
    roots = [r[1] for r in results]

    assert len(set(exec_ids)) == 20
    assert len(set(roots)) == 20
    for r in roots:
        assert r.is_dir()


def test_14_cleanup_one_sandbox_does_not_affect_another(
    provider: ProductionTaskSandboxProvider, sample_task: DownloadTask
) -> None:
    """Test 14: Cleaning up Sandbox A does not delete or alter Sandbox B."""
    sb_a = provider.create_sandbox(sample_task)
    sb_b = provider.create_sandbox(sample_task)

    (sb_a.output_dir / "video_a.mp4").write_bytes(b"A")
    (sb_b.output_dir / "video_b.mp4").write_bytes(b"B")

    # Finalize and clean A
    sb_a.finalize_success()
    # By default PRESERVE_ON_FAILURE, but finalize_success under DELETE_ON_SUCCESS deletes.
    # Let's explicitly delete A
    sb_a._delete_root()

    assert not sb_a.root.exists()
    assert sb_b.root.exists()
    assert (sb_b.output_dir / "video_b.mp4").is_file()


# =============================================================================
# 4. Retention Policy Lifecycle Tests
# =============================================================================


def test_15_success_deletion_policy(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 15: DELETE_ON_SUCCESS cleans up directory on finalize_success."""
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.DELETE_ON_SUCCESS,
    )
    sb = prov.create_sandbox(sample_task)
    assert sb.root.is_dir()

    sb.finalize_success()
    assert not sb.root.exists()
    assert sb.is_cleaned_up is True


def test_16_failure_preserve_policy(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 16: PRESERVE_ON_FAILURE retains directory on finalize_failure for post-mortem analysis."""
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.PRESERVE_ON_FAILURE,
    )
    sb = prov.create_sandbox(sample_task)
    (sb.logs_dir / "worker.log").write_text("Crash details", encoding="utf-8")

    sb.finalize_failure("Network reset")
    assert sb.root.is_dir(), "Directory must be preserved on failure under PRESERVE_ON_FAILURE!"
    reloaded = TaskSandboxMetadata.load(sb.root / "sandbox.json")
    assert reloaded.state == SandboxState.FAILED.value
    assert reloaded.failure_reason == "Network reset"


def test_17_delete_on_failure_policy(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 17: DELETE_ON_FAILURE deletes directory upon failure."""
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.DELETE_ON_FAILURE,
    )
    sb = prov.create_sandbox(sample_task)
    sb.finalize_failure("Fatal error")
    assert not sb.root.exists()


def test_18_preserve_always_policy(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 18: PRESERVE_ALWAYS retains directory on both success and failure."""
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.PRESERVE_ALWAYS,
    )
    sb1 = prov.create_sandbox(sample_task)
    sb1.finalize_success()
    assert sb1.root.is_dir()

    sb2 = prov.create_sandbox(sample_task)
    sb2.finalize_failure("Error")
    assert sb2.root.is_dir()


# =============================================================================
# 5. Multi-Factor Orphan Detection & GC Tests
# =============================================================================


def test_19_live_owner_pid_never_marked_orphan(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 19 (Ownership Invariant): Active sandbox owned by current live PID is NEVER eligible for GC."""
    sb = provider.create_sandbox(sample_task)
    records = provider.scan_orphans()

    matching = [r for r in records if r.execution_id == sb.execution_id]
    assert len(matching) == 1
    assert matching[0].is_pid_alive is True
    assert matching[0].eligible_for_gc is False
    assert "Active" in matching[0].reason


def test_20_old_live_owner_never_marked_for_gc(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 20 (AGE IS NOT OWNERSHIP): Even if age exceeds TTL, living owner PID is NEVER deleted."""
    sb = provider.create_sandbox(sample_task)
    # Simulate age being 100 days old, but owner_pid is os.getpid() (alive!)
    simulated_future_time = time.time() + (100 * 86400)

    records = provider.scan_orphans(now_epoch=simulated_future_time)
    matching = [r for r in records if r.execution_id == sb.execution_id]
    assert len(matching) == 1
    assert matching[0].is_pid_alive is True
    assert matching[0].eligible_for_gc is False

    # Run GC
    summary = provider.gc_orphans(now_epoch=simulated_future_time)
    assert sb.root.is_dir(), "Sandbox with alive PID must NOT be deleted even if age > TTL!"
    assert summary.active_live_count >= 1


def test_21_dead_owner_active_state_marked_orphan(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 21: Sandbox in ACTIVE state whose owner PID is dead is marked ORPHANED."""
    prov = ProductionTaskSandboxProvider(base_dir=temp_sandbox_base, orphan_grace_period_seconds=100.0)
    sb = prov.create_sandbox(sample_task)

    # Manually overwrite owner_pid to a definitely non-existent dead PID
    dead_pid = 99999999
    assert not is_pid_alive(dead_pid)
    sb.metadata.owner_pid = dead_pid
    sb.metadata.save_atomic(sb.root / "sandbox.json")

    # Within grace period -> orphan detected, but not yet eligible
    records = prov.scan_orphans(now_epoch=time.time() + 10)
    matching = [r for r in records if r.execution_id == sb.execution_id]
    assert len(matching) == 1
    assert matching[0].state == SandboxState.ORPHANED.value
    assert matching[0].is_pid_alive is False
    assert matching[0].eligible_for_gc is False

    # Past grace period -> eligible for GC
    records_past = prov.scan_orphans(now_epoch=time.time() + 150)
    matching_past = [r for r in records_past if r.execution_id == sb.execution_id]
    assert matching_past[0].eligible_for_gc is True


def test_22_crash_simulation_and_recovery(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 22 (Crash Recovery): Worker crashes mid-download leaving work files; next worker scans and GCs cleanly."""
    prov = ProductionTaskSandboxProvider(base_dir=temp_sandbox_base, orphan_grace_period_seconds=60.0)

    # Simulate crashed worker
    crashed_sb = prov.create_sandbox(sample_task)
    (crashed_sb.work_dir / "incomplete_stream.part").write_bytes(b"INCOMPLETE_PARTIAL_BYTES")
    crashed_sb.metadata.owner_pid = 99999999  # Dead PID
    crashed_sb.metadata.save_atomic(crashed_sb.root / "sandbox.json")

    # Next worker starts up and scans orphans
    simulated_now = time.time() + 120.0
    orphans = prov.scan_orphans(now_epoch=simulated_now)
    target = next(o for o in orphans if o.execution_id == crashed_sb.execution_id)
    assert target.eligible_for_gc is True
    assert target.state == SandboxState.ORPHANED.value

    # Run GC
    summary = prov.gc_orphans(now_epoch=simulated_now)
    assert summary.deleted_count >= 1
    assert not crashed_sb.root.exists(), "Crashed orphan must be safely cleaned up by GC!"


def test_23_corrupt_metadata_quarantined_never_deleted(
    temp_sandbox_base: Path, provider: ProductionTaskSandboxProvider
) -> None:
    """Test 23: Sandbox directory with unparseable JSON is quarantined and NEVER deleted by GC."""
    corrupt_dir = temp_sandbox_base / "corrupt_exec_dir"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    (corrupt_dir / "sandbox.json").write_text("{CORRUPT_JSON_DATA", encoding="utf-8")

    records = provider.scan_orphans()
    matching = [r for r in records if r.execution_id == "corrupt_exec_dir"]
    assert len(matching) == 1
    assert matching[0].state == "CORRUPT_METADATA"
    assert matching[0].eligible_for_gc is False

    summary = provider.gc_orphans(now_epoch=time.time() + 100000)
    assert corrupt_dir.exists(), "Corrupt metadata directory must NOT be deleted by GC!"
    assert summary.quarantined_count >= 1


def test_24_unknown_directory_without_metadata_quarantined(
    temp_sandbox_base: Path, provider: ProductionTaskSandboxProvider
) -> None:
    """Test 24: Stray directory lacking sandbox.json is quarantined and NEVER deleted by GC."""
    stray_dir = temp_sandbox_base / "stray_unknown_folder"
    stray_dir.mkdir(parents=True, exist_ok=True)
    (stray_dir / "random.txt").write_text("Hello", encoding="utf-8")

    records = provider.scan_orphans()
    matching = [r for r in records if r.execution_id == "stray_unknown_folder"]
    assert len(matching) == 1
    assert matching[0].state == "UNKNOWN_NO_METADATA"
    assert matching[0].eligible_for_gc is False

    summary = provider.gc_orphans(now_epoch=time.time() + 100000)
    assert stray_dir.exists(), "Unknown directory must NOT be wiped indiscriminately!"


def test_25_gc_expired_failure_orphan(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 25: Preserved failed sandbox is deleted once failure TTL expires and owner PID is dead."""
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.PRESERVE_ON_FAILURE,
        failure_ttl_seconds=3600.0,  # 1 hour
    )
    sb = prov.create_sandbox(sample_task)
    sb.finalize_failure("Network timeout")
    sb.metadata.owner_pid = 99999999  # Dead PID
    sb.metadata.save_atomic(sb.root / "sandbox.json")

    # Before TTL expires -> retained
    summary_early = prov.gc_orphans(now_epoch=time.time() + 1800)
    assert sb.root.is_dir()
    assert summary_early.retained_failure_count >= 1

    # After TTL expires -> deleted
    summary_late = prov.gc_orphans(now_epoch=time.time() + 4000)
    assert not sb.root.exists()
    assert summary_late.deleted_count >= 1


# =============================================================================
# 6. Integration, Cross-Platform & Firewall Tests
# =============================================================================


def test_26_restart_discovery(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 26: A newly instantiated provider accurately discovers preexisting sandboxes on disk."""
    prov1 = ProductionTaskSandboxProvider(base_dir=temp_sandbox_base)
    sb = prov1.create_sandbox(sample_task)

    # Simulate application restart: instantiate new provider on same directory
    prov2 = ProductionTaskSandboxProvider(base_dir=temp_sandbox_base)
    records = prov2.scan_orphans()

    assert any(r.execution_id == sb.execution_id for r in records)


def test_27_disk_space_guard(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 27: Provider raises SandboxDiskSpaceError if disk space is below required threshold."""
    huge_threshold = 1000 * 1024 * 1024 * 1024 * 1024  # 1000 TB
    prov = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        min_disk_space_bytes=huge_threshold,
    )
    with pytest.raises(SandboxDiskSpaceError, match="Insufficient disk space"):
        prov.create_sandbox(sample_task)


def test_28_windows_ntfs_path_resolution(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 28 (Windows NTFS Support): Handles Windows Path objects, backward/forward slashes."""
    prov = ProductionTaskSandboxProvider(base_dir=temp_sandbox_base)
    sb = prov.create_sandbox(sample_task)

    # Subdirectory resolution using Path / operators
    sub = sb.output_dir / "nested" / "file.mp4"
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_bytes(b"DATA")

    registered = sb.register_artifact(sub)
    assert registered.is_file()
    assert sb.root.is_dir()


def test_29_d01_integration_with_safe_downloader(temp_sandbox_base: Path, sample_task: DownloadTask) -> None:
    """Test 29 (D01 Integration): Injects ProductionTaskSandboxProvider into SafeDouyinDownloader."""
    provider = ProductionTaskSandboxProvider(
        base_dir=temp_sandbox_base,
        retention_policy=SandboxRetentionPolicy.DELETE_ON_SUCCESS,
    )
    backend = FakeDownloadBackend(success=True, files_to_create=("real_download.mp4",))
    downloader = SafeDouyinDownloader(
        backend=backend,
        sandbox_provider=provider,
    )

    result = downloader.execute(sample_task)

    assert result.status == DownloaderStatus.SUCCESS
    assert result.stage == ExecutionStage.SUCCESS
    assert len(result.assets) == 1
    # Under DELETE_ON_SUCCESS, sandbox should have been deleted upon success
    # Verify by scanning:
    orphans = provider.scan_orphans()
    assert len(orphans) == 0, "Sandbox must have been cleaned up after success!"


def test_30_final_archive_firewall(provider: ProductionTaskSandboxProvider, sample_task: DownloadTask) -> None:
    """Test 30 (Firewall Invariant): TaskSandbox and Provider have zero knowledge of canonical archive directory."""
    sb = provider.create_sandbox(sample_task)
    # Assert sandbox has no archive path attribute or reference
    assert not hasattr(sb, "archive_dir")
    assert not hasattr(sb, "canonical_path")
    assert not hasattr(provider, "archive_dir")
    assert not hasattr(provider, "canonical_path")
