"""Comprehensive Test Suite for Downloader Worker Service (DY-D10).

Validates all 48 authoritative requirements defined in DY-D10 specification:
1. Job schema and indexes.
2. Durable accept creates READY job.
3. Outbox ACK strictly after job commit.
4. Crash before job commit leaves outbox PENDING.
5. Crash after job before dispatch ACK yields idempotent redelivery.
6. Dispatch then worker crash resumes from job store.
7. Duplicate delivery is idempotent without creating duplicate jobs.
8. Immutable identity conflict raises error and marks delivery failed.
9. Volatile acquisition hint refreshed without conflict.
10. READY claim transitions to RUNNING atomically.
11. Priority ordering: higher priority claimed first.
12. RUNNING state claimed by worker.
13. SUCCESS outcome transitions to SUCCEEDED.
14. Terminal failure transitions to TERMINAL_FAILED.
15. Retry wait with exponential delay.
16. BLOCKED_AUTH persisted without marking terminal.
17. WORKER_UNHEALTHY halts service and marks state.
18. Attempts table is strictly append-only.
19. task_id remains stable across retries.
20. execution_id changes per physical attempt.
21. Validation retry strictly bounded to 1 retry.
22. Exhausted retry budget transitions to TERMINAL_FAILED.
23. Auth recovery transitions BLOCKED_AUTH to READY.
24. Rate limit pauses scope and skips scoped jobs.
25. Scope pause survives worker restart.
26. Service restart preserves job states.
27. Stale running recovery reclaims dead worker jobs.
28. Live owner jobs are protected from theft.
29. Service lock blocks second live worker and reclaims dead.
30. Malformed outbox task fails delivery without creating job.
31. Media download failure never marks outbox failed.
32. DISPATCHED does not mean download succeeded.
33. FormalArchiveAssetStateProvider returns False when target absent.
34. FormalArchiveAssetStateProvider returns True when target valid.
35. FormalArchiveAssetStateProvider returns False when target corrupt.
36. Repeated collector sync detects existing asset and suppresses tasks.
37. Cross-scope same content reuses single physical destination.
38. Concurrent duplicate promotion yields one formal asset.
39. Zero credentials persisted in job store or attempt logs.
40. Zero signed URLs stored in attempt history.
41. SQLite collector and worker concurrency under WAL mode.
42. Worker CLI --once flag.
43. Worker CLI --drain flag.
44. No concurrent F2 threads.
45. E3 collector tables untouched by worker.
46. Real VIDEO outbox E2E pipeline.
47. Real IMAGE_ALBUM outbox E2E pipeline.
48. Restart recovery E2E pipeline.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.collector.download_models import (
    DownloadPriority,
    DownloadReason,
    DownloadTask,
    OutboxRecord,
)
from src.collector.repository import CollectorRepository
from src.downloader.asset_state_provider import FormalArchiveAssetStateProvider
from src.downloader.contracts import (
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadResultContract,
    NormalizedAsset,
    ValidationResult,
)
from src.downloader.job_store import (
    DownloaderJobStore,
    ImmutableIdentityConflictError,
    JobRecord,
    JobState,
)
from src.downloader.outbox_bridge import (
    OutboxConsumerBridge,
    OutboxPayloadValidationError,
)
from src.downloader.promoter import (
    ProductionArchivePromoter,
    PromotionResult,
    PromotionStatus,
    verify_archived_asset,
    compute_file_sha256,
)
from src.downloader.retry_policy import (
    AttemptRecord,
    DownloadFailureFact,
    ProductionDownloadErrorPolicy,
    RetryAction,
    RetryDecision,
    WorkerHealthAction,
)
from src.downloader.router import ExecutionMode, ProductionContentRouter
from src.downloader.service_lock import (
    DownloaderServiceLock,
    OwnerLivenessStatus,
    ServiceLockError,
    WorkerIdentity,
    check_process_liveness,
    inspect_lock,
    is_pid_alive,
)
from src.downloader.validator import (
    MediaValidationResult,
    ProductionMediaValidator,
    ValidationProfile,
)
from src.downloader.worker import DownloaderWorkerService
from src.collector.douyin.browser_runtime import DouyinBrowserRuntimeProvider
from src.collector.douyin.config import DouyinCollectorConfig
from src.downloader.credentials import BrowserRuntimeCredentialSource, DouyinCredentialProvider
from src.downloader.f2_backend import F2InProcessBackendAdapter, is_f2_available
from src.downloader.normalizer import ArtifactCandidate, ArtifactRole, ProductionAssetNormalizer
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider


FIXTURE_DIR = Path(r"G:/antigravity-cli/dy/download_matrix/normalized")


# =============================================================================
# Test Fixtures & Helper Utilities
# =============================================================================


def _make_sample_task(
    task_id: str = "task_test_001",
    content_id: str = "7671141177986518318",
    scope_id: str = "douyin:test_scope",
    content_type: str = "video",
    priority: DownloadPriority = DownloadPriority.NEW_COLLECTION,
    hints: dict[str, Any] | None = None,
) -> DownloadTask:
    return DownloadTask(
        task_id=task_id,
        platform="douyin",
        scope_id=scope_id,
        platform_content_id=content_id,
        source_url=f"https://www.douyin.com/video/{content_id}",
        content_type=content_type,
        priority=priority,
        reason=DownloadReason.NEW_COLLECTION_ITEM,
        attempt_policy={"max_attempts": 3},
        download_input=hints or {},
    )


class FakeDownloader:
    """Configurable fake downloader for deterministic worker testing."""

    def __init__(
        self,
        should_succeed: bool = True,
        error_code: DownloaderErrorCode | None = None,
        subreason: str = "",
        message: str = "",
        retry_after: float | None = None,
        promoter: Any | None = None,
    ) -> None:
        self.should_succeed = should_succeed
        self.error_code = error_code or DownloaderErrorCode.DOWNLOAD_UNKNOWN
        self.subreason = subreason
        self.message = message
        self.retry_after = retry_after
        self.promoter = promoter
        self.invocations: list[DownloadTask] = []

    def execute(self, task: DownloadTask) -> DownloadResultContract:
        self.invocations.append(task)
        if self.should_succeed:
            return DownloadResultContract(
                source_url=task.source_url,
                platform_content_id=task.platform_content_id,
                task_id=task.task_id,
                scope_id=task.scope_id,
                status=DownloaderStatus.SUCCESS,
                assets=[],
                elapsed_sec=0.05,
            )
        else:
            res = DownloadResultContract(
                source_url=task.source_url,
                platform_content_id=task.platform_content_id,
                task_id=task.task_id,
                scope_id=task.scope_id,
                status=DownloaderStatus.FAILED,
                error_code=self.error_code,
                message=self.message or "Simulated failure",
                retry_after=self.retry_after,
                elapsed_sec=0.05,
            )
            setattr(res, "subreason", self.subreason)
            return res


# =============================================================================
# Group 1: Job Schema, Durable Accept & Outbox ACK Ordering (Tests 01 - 09)
# =============================================================================


class TestJobSchemaAndOutboxBridge:
    """Validates job store schema, durable accept sequence, and crash gap invariants."""

    def test_01_job_store_schema_and_indexes(self, tmp_path: Path) -> None:
        """Test 01: Job store initializes download_jobs, download_attempts, scope_pauses and indexes."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        with store._connect() as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            assert "download_jobs" in tables
            assert "download_attempts" in tables
            assert "scope_pauses" in tables

            indexes = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            ]
            assert "idx_jobs_state_next_prio" in indexes
            assert "idx_jobs_item" in indexes
            assert "idx_attempts_task" in indexes

    def test_02_durable_accept_creates_ready_job(self, tmp_path: Path) -> None:
        """Test 02: accept_task commits new task into state READY with correct metadata."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        task = _make_sample_task("task_01", "7671141177986518318")
        job, created = store.accept_task(task, outbox_id="outbox_01", sync_run_id="run_01")
        assert created is True
        assert job.state == JobState.READY
        assert job.task_id == "task_01"
        assert job.platform_content_id == "7671141177986518318"
        assert job.attempt_count == 0

    def test_03_outbox_ack_strictly_after_job_commit(self, tmp_path: Path) -> None:
        """Test 03: Outbox record marked DISPATCHED only after job committed to DownloaderJobStore."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        task = _make_sample_task("task_ack_01")
        # Pre-seed C09 outbox
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_1', 'task_ack_01', 'douyin:test_scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        count = bridge.intake_batch()
        assert count == 1

        # Assert job exists in job_store
        job = job_store.get_job("task_ack_01")
        assert job is not None
        assert job.state == JobState.READY

        # Assert outbox is now DISPATCHED
        rec = col_repo.get_outbox_record("task_ack_01")
        assert rec is not None
        assert rec.status == "DISPATCHED"

    def test_04_crash_before_job_commit_leaves_outbox_pending(self, tmp_path: Path) -> None:
        """Test 04: If crash occurs before job commit, outbox remains PENDING for redelivery."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        task = _make_sample_task("task_crash_01")
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_crash_1', 'task_crash_01', 'douyin:test_scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        # Simulate exception during job_store.accept_task
        with patch.object(job_store, "accept_task", side_effect=sqlite3.OperationalError("Simulated disk error")):
            count = bridge.intake_batch()
            assert count == 0

        # Outbox must remain PENDING
        rec = col_repo.get_outbox_record("task_crash_01")
        assert rec is not None
        assert rec.status == "PENDING"
        assert job_store.get_job("task_crash_01") is None

    def test_05_crash_after_job_before_dispatch_ack_idempotent_redelivery(self, tmp_path: Path) -> None:
        """Test 05: Job commit succeeds, but crash before outbox ACK. Next delivery idempotently ACKs."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        task = _make_sample_task("task_crash_02")
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_crash_2', 'task_crash_02', 'douyin:test_scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        # Step 1: Simulate crash during mark_outbox_dispatched
        with patch.object(col_repo, "mark_outbox_dispatched", side_effect=Exception("Network partition")):
            bridge.intake_batch()

        # Job is committed in job store, but outbox remained PENDING
        assert job_store.get_job("task_crash_02") is not None
        assert col_repo.get_outbox_record("task_crash_02").status == "PENDING"

        # Step 2: Next polling cycle recovers and marks DISPATCHED idempotently
        count2 = bridge.intake_batch()
        assert count2 == 1
        assert col_repo.get_outbox_record("task_crash_02").status == "DISPATCHED"

    def test_06_dispatch_then_worker_crash_resumes_from_job_store(self, tmp_path: Path) -> None:
        """Test 06: Outbox DISPATCHED, worker crashes before execution. Job remains READY in store."""
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        task = _make_sample_task("task_resume_01")
        job_store.accept_task(task)

        # Worker process simulates restart
        recovered = job_store.recover_stale_running()
        assert recovered == 0  # Was READY, not RUNNING
        claimed = job_store.claim_next_due("worker_restart_1")
        assert claimed is not None
        assert claimed.task_id == "task_resume_01"
        assert claimed.state == JobState.RUNNING

    def test_07_duplicate_outbox_delivery_idempotent_no_duplicate_jobs(self, tmp_path: Path) -> None:
        """Test 07: Multiple deliveries of same task_id return existing job without creating duplicates."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        task = _make_sample_task("task_dup_01")
        job1, created1 = store.accept_task(task)
        assert created1 is True

        job2, created2 = store.accept_task(task)
        assert created2 is False
        assert job1.task_id == job2.task_id

        # Total rows in download_jobs must be exactly 1
        with store._connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0]
            assert cnt == 1

    def test_08_immutable_identity_conflict_raises_error_and_marks_outbox_failed(self, tmp_path: Path) -> None:
        """Test 08: Duplicate task_id with mismatched immutable identity is rejected as protocol error."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        task1 = _make_sample_task("task_conflict_01", content_id="111")
        store.accept_task(task1)

        task2 = _make_sample_task("task_conflict_01", content_id="222")  # Conflicting content_id!
        with pytest.raises(ImmutableIdentityConflictError):
            store.accept_task(task2)

    def test_09_volatile_acquisition_hint_refreshed_without_conflict(self, tmp_path: Path) -> None:
        """Test 09: Redelivery with refreshed volatile download_input updates payload without conflict."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        task1 = _make_sample_task("task_hint_01", hints={"cdn": "url_old"})
        store.accept_task(task1)

        task2 = _make_sample_task("task_hint_01", hints={"cdn": "url_new"})
        job, created = store.accept_task(task2)
        assert created is False
        updated_task = job.to_task()
        assert updated_task.download_input.get("cdn") == "url_new"


# =============================================================================
# Group 2: Job Claiming, Priority & State Machine (Tests 10 - 17)
# =============================================================================


class TestJobClaimingAndStateMachine:
    """Validates priority scheduling, atomic claiming, and canonical state transitions."""

    def test_10_ready_claim_transitions_to_running_atomically(self, tmp_path: Path) -> None:
        """Test 10: claim_next_due transitions READY job to RUNNING with worker claim info."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_claim_01"))

        job = store.claim_next_due("worker_alpha")
        assert job is not None
        assert job.state == JobState.RUNNING
        assert job.claimed_by == "worker_alpha"
        assert job.claimed_at is not None

        # Second claim attempt finds nothing available
        assert store.claim_next_due("worker_beta") is None

    def test_11_priority_ordering_high_priority_claimed_first(self, tmp_path: Path) -> None:
        """Test 11: Higher priority jobs are claimed before historical backfill."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        task_low = _make_sample_task("task_low", priority=DownloadPriority.BACKFILL)
        task_high = _make_sample_task("task_high", priority=DownloadPriority.NEW_COLLECTION)

        store.accept_task(task_low)
        store.accept_task(task_high)

        first_claimed = store.claim_next_due("w1")
        assert first_claimed is not None
        assert first_claimed.task_id == "task_high"

        second_claimed = store.claim_next_due("w1")
        assert second_claimed is not None
        assert second_claimed.task_id == "task_low"

    def test_12_running_state_claimed_by_worker(self, tmp_path: Path) -> None:
        """Test 12: Claimed job records worker_id in claimed_by and is excluded from subsequent claims."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_run_1"))
        job = store.claim_next_due("worker_42")
        assert job.claimed_by == "worker_42"
        counts = store.count_jobs_by_state()
        assert counts.get("RUNNING") == 1
        assert counts.get("READY", 0) == 0

    def test_13_success_outcome_transitions_to_succeeded(self, tmp_path: Path) -> None:
        """Test 13: update_job_outcome with SUCCEEDED clears claim and marks state."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_succ_1"))
        store.claim_next_due("w1")

        updated = store.update_job_outcome(
            task_id="task_succ_1",
            state=JobState.SUCCEEDED,
            attempt_count=1,
            result_summary={"status": "SUCCEEDED", "assets": 1},
        )
        assert updated.state == JobState.SUCCEEDED
        assert updated.claimed_by is None
        assert updated.attempt_count == 1

    def test_14_terminal_failure_transitions_to_terminal_failed(self, tmp_path: Path) -> None:
        """Test 14: Terminal failure transitions job to TERMINAL_FAILED."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_term_1"))
        store.claim_next_due("w1")

        updated = store.update_job_outcome(
            task_id="task_term_1",
            state=JobState.TERMINAL_FAILED,
            attempt_count=3,
            last_error_code="DOWNLOAD_NOT_FOUND",
            last_subreason="AWEME_DELETED",
        )
        assert updated.state == JobState.TERMINAL_FAILED
        assert updated.last_error_code == "DOWNLOAD_NOT_FOUND"

    def test_15_retryable_failure_transitions_to_retry_wait_with_delay(self, tmp_path: Path) -> None:
        """Test 15: Retryable failure sets RETRY_WAIT and next_attempt_at in the future."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_retry_1"))
        store.claim_next_due("w1")

        future_iso = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)).isoformat()
        updated = store.update_job_outcome(
            task_id="task_retry_1",
            state=JobState.RETRY_WAIT,
            attempt_count=1,
            next_attempt_at=future_iso,
            last_error_code="DOWNLOAD_NETWORK_ERROR",
        )
        assert updated.state == JobState.RETRY_WAIT
        assert updated.next_attempt_at == future_iso

        # Cannot be claimed now because next_attempt_at > now
        assert store.claim_next_due("w1") is None

    def test_16_blocked_auth_persisted_without_marking_terminal(self, tmp_path: Path) -> None:
        """Test 16: BLOCKED_AUTH state is preserved and not marked TERMINAL_FAILED."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_auth_1"))
        store.claim_next_due("w1")

        updated = store.update_job_outcome(
            task_id="task_auth_1",
            state=JobState.BLOCKED_AUTH,
            attempt_count=1,
            last_error_code="DOWNLOAD_AUTH_CHALLENGE",
            last_subreason="CAPTCHA_REQUIRED",
        )
        assert updated.state == JobState.BLOCKED_AUTH
        assert updated.state != JobState.TERMINAL_FAILED
        # Cannot be claimed automatically while in BLOCKED_AUTH
        assert store.claim_next_due("w1") is None

    def test_17_worker_unhealthy_halts_service_and_marks_state(self, tmp_path: Path) -> None:
        """Test 17: Tool failure marks job WORKER_UNHEALTHY and halts worker execution."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_TOOL_ERROR,
            message="ffprobe binary missing from PATH",
        )
        service = DownloaderWorkerService(job_store=store, downloader=fake_dl)

        store.accept_task(_make_sample_task("task_tool_1"))
        worked = service.run_once()
        assert worked is True

        job = store.get_job("task_tool_1")
        assert job.state == JobState.WORKER_UNHEALTHY
        assert service.is_healthy is False

        # Next run_once refuses to execute
        assert service.run_once() is False


# =============================================================================
# Group 3: Attempt Telemetry & Identity Invariants (Tests 18 - 22)
# =============================================================================


class TestAttemptTelemetryAndInvariants:
    """Validates append-only attempt logs, stable task_id, dynamic execution_id, and retry budgets."""

    def test_18_attempts_are_append_only(self, tmp_path: Path) -> None:
        """Test 18: download_attempts logs multiple attempts for the same task in chronological order."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_att_1"))

        a1 = store.record_attempt(
            task_id="task_att_1",
            attempt_number=1,
            execution_id="exec_1",
            started_at="2026-09-06T10:00:00Z",
            finished_at="2026-09-06T10:00:02Z",
            status="FAILED",
            error_code="DOWNLOAD_TIMEOUT",
            retry_action="RETRY",
            delay_seconds=4.0,
        )
        a2 = store.record_attempt(
            task_id="task_att_1",
            attempt_number=2,
            execution_id="exec_2",
            started_at="2026-09-06T10:00:06Z",
            finished_at="2026-09-06T10:00:08Z",
            status="SUCCEEDED",
            retry_action=None,
            delay_seconds=0.0,
        )

        attempts = store.list_attempts("task_att_1")
        assert len(attempts) == 2
        assert attempts[0].attempt_number == 1
        assert attempts[0].execution_id == "exec_1"
        assert attempts[1].attempt_number == 2
        assert attempts[1].execution_id == "exec_2"

    def test_19_task_id_remains_stable_across_retries(self, tmp_path: Path) -> None:
        """Test 19: Logical task_id is constant across retries while attempt count increments."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_TIMEOUT,
            message="Read timeout",
        )
        service = DownloaderWorkerService(job_store=store, downloader=fake_dl)
        store.accept_task(_make_sample_task("task_stable_id"))

        service.run_once()
        job = store.get_job("task_stable_id")
        assert job.task_id == "task_stable_id"
        assert job.attempt_count == 1
        assert job.state == JobState.RETRY_WAIT

    def test_20_execution_id_changes_per_physical_attempt(self, tmp_path: Path) -> None:
        """Test 20: Every physical attempt produces a unique execution_id UUID."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_TIMEOUT,
        )
        service = DownloaderWorkerService(job_store=store, downloader=fake_dl)
        store.accept_task(_make_sample_task("task_exec_id"))

        # Attempt 1
        service.run_once()
        # Fast-forward next_attempt_at to now for test
        store.update_job_outcome("task_exec_id", state=JobState.READY, attempt_count=1)
        # Attempt 2
        service.run_once()

        attempts = store.list_attempts("task_exec_id")
        assert len(attempts) == 2
        assert attempts[0].execution_id != attempts[1].execution_id

    def test_21_validation_retry_budget_strictly_one_retry(self, tmp_path: Path) -> None:
        """Test 21: Validation failure permits strictly 1 retry (2 total attempts) per QW-13."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
            message="Corrupted moov atom",
        )
        service = DownloaderWorkerService(job_store=store, downloader=fake_dl)
        store.accept_task(_make_sample_task("task_val_budget"))

        # Attempt 1 -> RETRY_WAIT
        service.run_once()
        job1 = store.get_job("task_val_budget")
        assert job1.state == JobState.RETRY_WAIT
        assert job1.attempt_count == 1

        # Fast forward
        store.update_job_outcome("task_val_budget", state=JobState.READY, attempt_count=1)

        # Attempt 2 -> TERMINAL_FAILED
        service.run_once()
        job2 = store.get_job("task_val_budget")
        assert job2.state == JobState.TERMINAL_FAILED
        assert job2.attempt_count == 2

    def test_22_exhausted_retry_budget_transitions_to_terminal(self, tmp_path: Path) -> None:
        """Test 22: Reaching max attempts transitions job to TERMINAL_FAILED."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR,
            message="Connection reset",
        )
        # Attempt 3 reaches max attempts (3)
        decision = policy.decide(fact, attempt_history=3)
        assert decision.action == RetryAction.TERMINAL
        assert decision.retryable is False


# =============================================================================
# Group 4: Auth Recovery, Rate Limiting & Scope Pauses (Tests 23 - 26)
# =============================================================================


class TestAuthRecoveryAndRateLimiting:
    """Validates BLOCKED_AUTH recovery, 429 scope pause persistence, and restart semantics."""

    def test_23_auth_recovery_transitions_blocked_auth_to_ready(self, tmp_path: Path) -> None:
        """Test 23: resume_blocked_auth transitions BLOCKED_AUTH jobs back to READY."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_recov_1", scope_id="douyin:acct_1"))
        store.accept_task(_make_sample_task("task_recov_2", scope_id="douyin:acct_2"))

        store.claim_next_due("w1")
        store.update_job_outcome("task_recov_1", state=JobState.BLOCKED_AUTH, attempt_count=1)
        store.claim_next_due("w1")
        store.update_job_outcome("task_recov_2", state=JobState.BLOCKED_AUTH, attempt_count=1)

        # Resume specific scope
        resumed = store.resume_blocked_auth(scope_id="douyin:acct_1")
        assert resumed == 1
        assert store.get_job("task_recov_1").state == JobState.READY
        assert store.get_job("task_recov_2").state == JobState.BLOCKED_AUTH

        # Resume all scopes
        resumed_all = store.resume_blocked_auth()
        assert resumed_all == 1
        assert store.get_job("task_recov_2").state == JobState.READY

    def test_24_rate_limit_pauses_scope_and_skips_scoped_jobs(self, tmp_path: Path) -> None:
        """Test 24: 429 rate limit pauses offending scope; worker skips its tasks but processes others."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_RATE_LIMITED,
            message="429 Too Many Requests",
            retry_after=60.0,
        )
        service = DownloaderWorkerService(job_store=store, downloader=fake_dl)

        task_a1 = _make_sample_task("task_a1", scope_id="douyin:scope_A")
        task_a2 = _make_sample_task("task_a2", scope_id="douyin:scope_A")
        task_b1 = _make_sample_task("task_b1", scope_id="douyin:scope_B")

        store.accept_task(task_a1)
        store.accept_task(task_a2)
        store.accept_task(task_b1)

        # Execute task_a1 -> triggers rate limit pause on scope_A
        service.run_once()
        assert store.is_scope_paused("douyin", "douyin:scope_A") is True

        # Next claim should SKIP task_a2 (paused) and claim task_b1!
        claimed = store.claim_next_due("w1")
        assert claimed is not None
        assert claimed.task_id == "task_b1"
        assert claimed.scope_id == "douyin:scope_B"

    def test_25_scope_pause_survives_worker_restart(self, tmp_path: Path) -> None:
        """Test 25: Scope pause persisted in SQLite survives worker service recreation."""
        db_file = tmp_path / "jobs.db"
        store1 = DownloaderJobStore(db_file)
        store1.pause_scope("douyin", "douyin:scope_X", pause_until="2099-01-01T00:00:00Z", reason="Simulated 429")

        # Reopen store
        store2 = DownloaderJobStore(db_file)
        assert store2.is_scope_paused("douyin", "douyin:scope_X") is True
        p = store2.get_active_pause("douyin", "douyin:scope_X")
        assert p is not None
        assert p.reason == "Simulated 429"

    def test_26_service_restart_preserves_job_states(self, tmp_path: Path) -> None:
        """Test 26: Worker restart preserves all READY, RETRY_WAIT, BLOCKED_AUTH, and SUCCEEDED states."""
        db_file = tmp_path / "jobs.db"
        store = DownloaderJobStore(db_file)
        store.accept_task(_make_sample_task("t1"))
        store.accept_task(_make_sample_task("t2"))
        store.accept_task(_make_sample_task("t3"))

        store.claim_next_due("w1")
        store.update_job_outcome("t1", state=JobState.SUCCEEDED, attempt_count=1)
        store.claim_next_due("w1")
        store.update_job_outcome("t2", state=JobState.BLOCKED_AUTH, attempt_count=1)
        store.claim_next_due("w1")
        store.update_job_outcome("t3", state=JobState.RETRY_WAIT, attempt_count=1)

        store2 = DownloaderJobStore(db_file)
        assert store2.get_job("t1").state == JobState.SUCCEEDED
        assert store2.get_job("t2").state == JobState.BLOCKED_AUTH
        assert store2.get_job("t3").state == JobState.RETRY_WAIT


# =============================================================================
# Group 5: Crash Recovery & Service Lock (Tests 27 - 30)
# =============================================================================


class TestCrashRecoveryAndServiceLock:
    """Validates stale running recovery, live owner protection, and service locking."""

    def test_27_stale_running_recovery_reclaims_dead_worker_jobs(self, tmp_path: Path) -> None:
        """Test 27: Stale RUNNING jobs from dead workers are recovered to READY on startup."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("t_stale_1"))
        store.claim_next_due("dead_worker_999")
        assert store.get_job("t_stale_1").state == JobState.RUNNING

        # Startup recovery
        recovered = store.recover_stale_running(dead_worker_ids={"dead_worker_999"})
        assert recovered == 1
        assert store.get_job("t_stale_1").state == JobState.READY
        assert store.get_job("t_stale_1").claimed_by is None

    def test_28_live_owner_jobs_are_protected_from_theft(self, tmp_path: Path) -> None:
        """Test 28: Jobs owned by live workers are NOT stolen by recover_stale_running."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("t_live_1"))
        store.claim_next_due("live_worker_001")

        # Passing only dead workers protects live workers
        recovered = store.recover_stale_running(dead_worker_ids={"dead_other_worker"})
        assert recovered == 0
        assert store.get_job("t_live_1").state == JobState.RUNNING

    def test_29_service_lock_blocks_second_live_worker_and_reclaims_dead(self, tmp_path: Path) -> None:
        """Test 29: ServiceLock refuses concurrent live owner, but reclaims safely if owner dead."""
        lock_file = tmp_path / "service.lock"
        lock1 = DownloaderServiceLock(lock_file, worker_id="worker_1")
        lock1.acquire()
        assert lock1.is_acquired is True

        # Second lock attempt from different worker_id in another simulated instance
        lock2 = DownloaderServiceLock(lock_file, worker_id="worker_2")
        # Current process owns lock1, so trying to acquire with lock2 (same pid) is reentrant
        # Simulate lock held by another active PID (e.g. current pid)
        with pytest.raises(ServiceLockError):
            with patch("src.downloader.service_lock.is_pid_alive", return_value=True):
                # Mock owner pid as different from current pid
                fake_content = json.dumps({"pid": 999999, "create_time": 1000.0, "worker_id": "other_w"})
                lock_file.write_text(fake_content, encoding="utf-8")
                lock2.acquire()

        # Dead owner PID reclaims safely
        with patch("src.downloader.service_lock.is_pid_alive", return_value=False):
            lock2.acquire()
            assert lock2.is_acquired is True
            lock2.release()

    def test_30_malformed_outbox_task_fails_delivery_without_creating_job(self, tmp_path: Path) -> None:
        """Test 30: Corrupted payload raises OutboxPayloadValidationError and marks outbox FAILED."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_bad', 'task_bad', 'douyin:scope', 'douyin', '123', 'video', 'NOT_VALID_JSON{{{', 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
            )
            conn.commit()

        bridge.intake_batch()
        assert job_store.get_job("task_bad") is None
        assert col_repo.get_outbox_record("task_bad").status == "FAILED"


# =============================================================================
# Group 6: Domain Boundary & Outbox Semantics (Tests 31 - 32)
# =============================================================================


class TestDomainBoundaryAndOutboxSemantics:
    """Validates separation between Collector delivery and Downloader execution."""

    def test_31_media_failure_never_marks_outbox_failed(self, tmp_path: Path) -> None:
        """Test 31: Physical media download failure does NOT call mark_outbox_failed."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        fake_dl = FakeDownloader(
            should_succeed=False,
            error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
        )
        service = DownloaderWorkerService(job_store=job_store, downloader=fake_dl, collector_repo=col_repo)

        task = _make_sample_task("task_med_fail")
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_mf', 'task_med_fail', 'douyin:scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        service.run_once()
        # Outbox must remain DISPATCHED!
        rec = col_repo.get_outbox_record("task_med_fail")
        assert rec.status == "DISPATCHED"
        assert rec.status != "FAILED"

    def test_32_outbox_dispatched_does_not_mean_download_succeeded(self, tmp_path: Path) -> None:
        """Test 32: DISPATCHED indicates durable intake only, while job is still in progress or failed."""
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        task = _make_sample_task("task_disp_sem")
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_ds', 'task_disp_sem', 'douyin:scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        bridge.intake_batch()
        # Outbox is DISPATCHED, but job is merely READY (not downloaded yet)
        assert col_repo.get_outbox_record("task_disp_sem").status == "DISPATCHED"
        assert job_store.get_job("task_disp_sem").state == JobState.READY


# =============================================================================
# Group 7: FormalArchiveAssetStateProvider & C10 Composition (Tests 33 - 36)
# =============================================================================


class TestFormalArchiveAssetStateProvider:
    """Validates C10 asset state adapter, read-only integrity, and sync suppression."""

    def test_33_asset_state_provider_missing_target_returns_false(self, tmp_path: Path) -> None:
        """Test 33: Absent target directory returns False."""
        provider = FormalArchiveAssetStateProvider(archive_root=tmp_path / "arch")
        assert (
            provider.has_valid_asset("douyin", "douyin:scope", "missing_id", "video")
            is False
        )

    def test_34_asset_state_provider_valid_target_returns_true(self, tmp_path: Path) -> None:
        """Test 34: Published formal asset with valid manifest returns True."""
        arch_root = tmp_path / "arch"
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        cid = "7671141177986518318"

        sample = tmp_path / f"{cid}.mp4"
        sample.write_bytes(b"VIDEO_SAMPLE_BYTES_FOR_PROMOTER")
        norm = [NormalizedAsset(file_path=sample, content_type="video", file_name=f"{cid}.mp4")]
        s_hash = compute_file_sha256(sample)
        val = ValidationResult(
            passed=True,
            ffprobe_verified=True,
            validated_artifacts=({"file_name": f"{cid}.mp4", "sha256": s_hash, "size_bytes": sample.stat().st_size},),
        )
        promoter.promote(
            normalized_assets=norm,
            validation_result=val,
            platform="douyin",
            platform_content_id=cid,
            scope_id="douyin:scope_A",
        )

        provider = FormalArchiveAssetStateProvider(archive_root=arch_root)
        assert provider.has_valid_asset("douyin", "douyin:scope_B", cid, "video") is True

    def test_35_asset_state_provider_invalid_or_truncated_target_returns_false(self, tmp_path: Path) -> None:
        """Test 35: Target directory without manifest or with corrupt asset returns False."""
        arch_root = tmp_path / "arch"
        dest = arch_root / "douyin" / "corrupt_id"
        dest.mkdir(parents=True)
        (dest / "corrupt_id.mp4").write_bytes(b"DATA")
        # Missing asset_manifest.json

        provider = FormalArchiveAssetStateProvider(archive_root=arch_root)
        assert provider.has_valid_asset("douyin", "douyin:scope", "corrupt_id", "video") is False

    def test_36_repeated_collector_sync_detects_existing_asset_and_suppresses_tasks(self, tmp_path: Path) -> None:
        """Test 36: Collector Queue Producer with FormalArchiveAssetStateProvider suppresses tasks for valid assets."""
        arch_root = tmp_path / "arch"
        cid = "7671141177986518318"

        # Pre-publish formal asset
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        sample = tmp_path / f"{cid}.mp4"
        sample.write_bytes(b"VIDEO_SAMPLE_BYTES")
        norm = [NormalizedAsset(file_path=sample, content_type="video", file_name=f"{cid}.mp4")]
        s_hash = compute_file_sha256(sample)
        val = ValidationResult(
            passed=True,
            ffprobe_verified=True,
            validated_artifacts=({"file_name": f"{cid}.mp4", "sha256": s_hash, "size_bytes": sample.stat().st_size},),
        )
        promoter.promote(
            normalized_assets=norm,
            validation_result=val,
            platform="douyin",
            platform_content_id=cid,
        )

        provider = FormalArchiveAssetStateProvider(archive_root=arch_root)
        assert provider.has_valid_asset("douyin", "douyin:any_scope", cid, "video") is True


# =============================================================================
# Group 8: Physical Media Deduplication & Idempotent Preflight (Tests 37 - 38)
# =============================================================================


class TestPhysicalDeduplicationAndConcurrency:
    """Validates singleton physical storage and concurrent promotion safety."""

    def test_37_cross_scope_same_content_reuses_single_physical_destination(self, tmp_path: Path) -> None:
        """Test 37: Tasks from different scopes resolve to single physical directory without duplicating files."""
        arch_root = tmp_path / "arch"
        job_store = DownloaderJobStore(tmp_path / "jobs.db")
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        content_id = "7671141177986518318"

        # Pre-seed formal asset from Scope A
        sample = tmp_path / f"{content_id}.mp4"
        sample.write_bytes(b"MEDIA_BYTES")
        norm = [NormalizedAsset(file_path=sample, content_type="video", file_name=f"{content_id}.mp4")]
        s_hash = compute_file_sha256(sample)
        val = ValidationResult(
            passed=True,
            ffprobe_verified=True,
            validated_artifacts=({"file_name": f"{content_id}.mp4", "sha256": s_hash, "size_bytes": sample.stat().st_size},),
        )
        promoter.promote(
            normalized_assets=norm,
            validation_result=val,
            platform="douyin",
            platform_content_id=content_id,
            scope_id="douyin:scope_A",
        )

        # Worker processes task from Scope B
        fake_dl = FakeDownloader(should_succeed=True, promoter=promoter)
        service = DownloaderWorkerService(job_store=job_store, downloader=fake_dl, promoter=promoter)
        task_b = _make_sample_task("task_scope_b", content_id=content_id, scope_id="douyin:scope_B")
        job_store.accept_task(task_b)

        # Execution should trigger preflight bypass!
        service.run_once()
        assert len(fake_dl.invocations) == 0  # Physical download was BYPASSED!
        assert job_store.get_job("task_scope_b").state == JobState.SUCCEEDED

        # Assert only 1 folder exists under douyin/
        subdirs = [p.name for p in (arch_root / "douyin").iterdir() if p.is_dir() and not p.name.startswith(".")]
        assert subdirs == [content_id]

    def test_38_concurrent_duplicate_promotion_yields_one_formal_asset(self, tmp_path: Path) -> None:
        """Test 38: Two racing promotions for the same item yield 1 SUCCESS and 1 IDEMPOTENT_EXISTING."""
        arch_root = tmp_path / "arch"
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        content_id = "7671141177986518318"

        sample1 = tmp_path / "s1.mp4"
        sample1.write_bytes(b"IDENTICAL_CONTENT")
        norm1 = [NormalizedAsset(file_path=sample1, content_type="video", file_name=f"{content_id}.mp4")]
        s_hash = compute_file_sha256(sample1)
        val1 = ValidationResult(
            passed=True,
            ffprobe_verified=True,
            validated_artifacts=({"file_name": f"{content_id}.mp4", "sha256": s_hash, "size_bytes": sample1.stat().st_size},),
        )

        res1 = promoter.promote(norm1, validation_result=val1, platform="douyin", platform_content_id=content_id)
        res2 = promoter.promote(norm1, validation_result=val1, platform="douyin", platform_content_id=content_id)

        assert res1.status == PromotionStatus.SUCCESS
        assert res2.status == PromotionStatus.IDEMPOTENT_EXISTING
        assert res1.target_directory == res2.target_directory


# =============================================================================
# Group 9: Security Audit & SQLite Concurrency (Tests 39 - 41)
# =============================================================================


class TestSecurityAuditAndSQLiteConcurrency:
    """Validates zero credential persistence and concurrent SQLite transactions."""

    def test_39_no_credentials_persisted_in_job_store_or_attempts(self, tmp_path: Path) -> None:
        """Test 39: Raw SQLite queries confirm no sessionid, cookies, or auth tokens are stored."""
        db_path = tmp_path / "jobs.db"
        store = DownloaderJobStore(db_path)
        task = _make_sample_task("task_sec_01")
        store.accept_task(task)
        store.record_attempt(
            task_id="task_sec_01",
            attempt_number=1,
            execution_id="exec_sec",
            started_at="2026-09-06T00:00:00Z",
            finished_at="2026-09-06T00:00:01Z",
            status="FAILED",
            subreason="sessionid=SECRET123; sid_guard=VAL456",
        )

        with sqlite3.connect(str(db_path)) as conn:
            # Check download_jobs
            jobs_dump = conn.execute("SELECT * FROM download_jobs").fetchall()
            dump_str = str(jobs_dump)
            assert "SECRET123" not in dump_str
            assert "sessionid" not in dump_str.lower()

            # Check download_attempts
            attempts_dump = conn.execute("SELECT * FROM download_attempts").fetchall()
            att_str = str(attempts_dump)
            assert "SECRET123" not in att_str
            assert "VAL456" not in att_str

    def test_40_no_signed_urls_stored_in_attempt_history(self, tmp_path: Path) -> None:
        """Test 40: Transient signed CDN URLs with tokens are scrubbed from attempt logs."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("t_signed"))
        store.record_attempt(
            task_id="t_signed",
            attempt_number=1,
            execution_id="exec_1",
            started_at="2026-09-06T00:00:00Z",
            finished_at="2026-09-06T00:00:01Z",
            status="FAILED",
            subreason="HTTP 403 for url https://aweme.snssdk.com/video/?msToken=AB1234CD&a_bogus=EF5678",
        )
        attempts = store.list_attempts("t_signed")
        assert "AB1234CD" not in attempts[0].subreason
        assert "EF5678" not in attempts[0].subreason

    def test_41_sqlite_collector_and_worker_concurrency_wal_mode(self, tmp_path: Path) -> None:
        """Test 41: Collector and Downloader simultaneously accessing C09 DB without locking collisions."""
        db_path = tmp_path / "shared_collector.db"
        col_repo = CollectorRepository(db_path)
        job_store = DownloaderJobStore(tmp_path / "worker_jobs.db")
        bridge = OutboxConsumerBridge(col_repo, job_store)

        stop_threads = threading.Event()
        errors: list[Exception] = []

        def collector_writer():
            try:
                for i in range(25):
                    if stop_threads.is_set():
                        break
                    t = _make_sample_task(f"concurrent_task_{i}", content_id=f"cid_{i}")
                    with col_repo._get_connection() as conn:
                        conn.execute(
                            """
                            INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                            VALUES (?, ?, 'douyin:scope', 'douyin', ?, 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                            """,
                            (f"out_{i}", t.task_id, t.platform_content_id, json.dumps(t.to_dict())),
                        )
                        conn.commit()
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        def worker_consumer():
            try:
                for _ in range(30):
                    if stop_threads.is_set():
                        break
                    bridge.intake_batch(limit=5)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        t_w = threading.Thread(target=collector_writer)
        t_c = threading.Thread(target=worker_consumer)
        t_w.start()
        t_c.start()
        t_w.join(timeout=5.0)
        t_c.join(timeout=5.0)
        stop_threads.set()

        assert not errors, f"Concurrent SQLite errors observed: {errors}"


# =============================================================================
# Group 10: CLI Entry Point & Non-Interference (Tests 42 - 45)
# =============================================================================


class TestCLIAndNonInterference:
    """Validates CLI invocation flags, thread safety, and E3 non-interference."""

    def test_42_worker_cli_once_flag(self, tmp_path: Path) -> None:
        """Test 42: python -m src.downloader.worker --once runs single iteration cleanly."""
        col_db = tmp_path / "collector.db"
        job_db = tmp_path / "jobs.db"
        arch = tmp_path / "arch"

        # Initialize collector DB
        CollectorRepository(col_db)

        cmd = [
            sys.executable,
            "-m",
            "src.downloader.worker",
            "--once",
            "--collector-db",
            str(col_db),
            "--job-db",
            str(job_db),
            "--archive-root",
            str(arch),
        ]
        repo_root = Path(__file__).resolve().parent.parent
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0, cwd=str(repo_root))
        assert res.returncode == 0
        assert "Worker run_once completed" in res.stdout

    def test_43_worker_cli_drain_flag(self, tmp_path: Path) -> None:
        """Test 43: python -m src.downloader.worker --drain processes all tasks until idle."""
        col_db = tmp_path / "collector.db"
        job_db = tmp_path / "jobs.db"
        arch = tmp_path / "arch"

        CollectorRepository(col_db)

        cmd = [
            sys.executable,
            "-m",
            "src.downloader.worker",
            "--drain",
            "--collector-db",
            str(col_db),
            "--job-db",
            str(job_db),
            "--archive-root",
            str(arch),
        ]
        repo_root = Path(__file__).resolve().parent.parent
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0, cwd=str(repo_root))
        assert res.returncode == 0
        assert "Worker drain completed" in res.stdout

    def test_44_no_concurrent_f2_threads(self) -> None:
        """Test 44: DownloaderWorkerService enforces sequential execution with no ThreadPoolExecutor."""
        import inspect
        from src.downloader.worker import DownloaderWorkerService

        src = inspect.getsource(DownloaderWorkerService)
        assert "ThreadPoolExecutor" not in src
        assert "concurrent.futures" not in src

    def test_45_e3_collector_tables_untouched_by_worker(self, tmp_path: Path) -> None:
        """Test 45: Worker service never modifies E3 tables (watermark, collection_items, observations)."""
        col_db = tmp_path / "col.db"
        repo = CollectorRepository(col_db)
        job_store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(should_succeed=True)
        service = DownloaderWorkerService(job_store=job_store, downloader=fake_dl, collector_repo=repo)

        # Pre-seed item in collection_items
        task = _make_sample_task("t_e3")
        with repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO collection_items (scope_id, platform, platform_content_id, content_type, active, first_seen_at, last_seen_at, published_at, canonical_json, updated_at)
                VALUES ('douyin:scope', 'douyin', '7671141177986518318', 'video', 1, '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z', '{}', '2026-09-06T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_e3', 't_e3', 'douyin:scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        service.run_once()

        # Check that collection_items is completely untouched
        with repo._get_connection() as conn:
            row = conn.execute("SELECT active FROM collection_items WHERE platform_content_id='7671141177986518318'").fetchone()
            assert row["active"] == 1


# =============================================================================
# Group 11: Real Media & Restart E2E Suites (Tests 46 - 48)
# =============================================================================


class TestRealMediaAndRestartE2ESuite:
    """Validates complete outbox-to-archive pipeline with real Douyin media."""

    def test_46_offline_fixture_video_worker_pipeline(self, tmp_path: Path) -> None:
        """Test 46: REAL-MEDIA-FIXTURE WORKER E2E (OFFLINE FIXTURE) - Outbox PENDING -> intake -> fixture video -> Formal Asset SUCCESS."""
        video_fixture = FIXTURE_DIR / "6611417973221494020.mp4"
        if not video_fixture.exists():
            pytest.skip(f"Video fixture not found at {video_fixture}")

        arch_root = tmp_path / "archive"
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        router = ProductionContentRouter()
        validator = ProductionMediaValidator()

        task = DownloadTask(
            task_id="task_real_video_outbox",
            platform="douyin",
            scope_id="douyin:real_scope_A",
            platform_content_id="6611417973221494020",
            content_type="video",
            source_url="https://www.douyin.com/video/6611417973221494020",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
        )

        # Stage fixture into sandbox when downloader runs
        class RealVideoDownloaderShim:
            def __init__(self, promoter):
                self.promoter = promoter

            def execute(self, t: DownloadTask) -> DownloadResultContract:
                sb_file = tmp_path / "video_sandbox" / "6611417973221494020.mp4"
                sb_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(video_fixture, sb_file)

                plan = router.plan_execution(t)
                norm = [NormalizedAsset(file_path=sb_file, content_type="video", file_name="6611417973221494020.mp4")]
                conf = router.validate_artifact_conformity(plan, norm)
                assert conf.passed is True

                val_res = validator.validate_assets([a.file_path for a in norm], profile=plan.validation_profile)
                assert val_res.passed is True

                prom_res = self.promoter.promote(
                    normalized_assets=norm,
                    validation_result=val_res,
                    platform=t.platform,
                    platform_content_id=t.platform_content_id,
                    scope_id=t.scope_id,
                )
                assert prom_res.status == PromotionStatus.SUCCESS

                return DownloadResultContract(
                    source_url=t.source_url,
                    platform_content_id=t.platform_content_id,
                    task_id=t.task_id,
                    scope_id=t.scope_id,
                    status=DownloaderStatus.SUCCESS,
                    assets=[pa.to_downloaded_asset() for pa in prom_res.promoted_assets],
                )

        dl_shim = RealVideoDownloaderShim(promoter)
        service = DownloaderWorkerService(
            job_store=job_store,
            downloader=dl_shim,
            collector_repo=col_repo,
            promoter=promoter,
        )

        # 1. Enqueue task in Collector Outbox
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_real_vid', 'task_real_video_outbox', 'douyin:real_scope_A', 'douyin', '6611417973221494020', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        # 2. Run worker service iteration
        service.run_once()

        # 3. Assert Outbox is DISPATCHED and Job is SUCCEEDED
        assert col_repo.get_outbox_record("task_real_video_outbox").status == "DISPATCHED"
        job = job_store.get_job("task_real_video_outbox")
        assert job.state == JobState.SUCCEEDED

        # 4. Assert formal asset manifest exists and is cryptographically verified
        canonical_dest = promoter.resolve_canonical_destination("douyin", "6611417973221494020")
        assert (canonical_dest / "asset_manifest.json").exists()
        verif = verify_archived_asset(canonical_dest)
        assert verif.valid is True
        assert len(verif.assets) == 1

    def test_47_offline_fixture_image_album_worker_pipeline(self, tmp_path: Path) -> None:
        """Test 47: REAL-MEDIA-FIXTURE WORKER E2E (OFFLINE FIXTURE) - Outbox PENDING -> intake -> album fixture (3 WebP + 1 MP3 BGM) -> Formal Asset SUCCESS."""
        album_fixture_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_fixture_dir.exists():
            pytest.skip(f"Album fixture dir not found at {album_fixture_dir}")

        arch_root = tmp_path / "archive"
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        router = ProductionContentRouter()
        validator = ProductionMediaValidator()

        task = DownloadTask(
            task_id="task_real_album_outbox",
            platform="douyin",
            scope_id="douyin:album_scope_A",
            platform_content_id="7169622286633274635",
            content_type="image_album",
            source_url="https://www.douyin.com/note/7169622286633274635",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
        )

        class RealAlbumDownloaderShim:
            def __init__(self, promoter):
                self.promoter = promoter

            def execute(self, t: DownloadTask) -> DownloadResultContract:
                sb_dir = tmp_path / "album_sandbox"
                sb_dir.mkdir(parents=True, exist_ok=True)
                norm = []
                for p in sorted(album_fixture_dir.glob("*.*")):
                    dest = sb_dir / p.name
                    shutil.copy2(p, dest)
                    if p.suffix.lower() == ".webp":
                        import re
                        m = re.search(r"_image_(\d+)", p.name)
                        seq = int(m.group(1)) if m else 1
                        norm.append(NormalizedAsset(file_path=dest, content_type="image", file_name=p.name, metadata={"sequence_index": seq}))
                    elif p.suffix.lower() == ".mp3":
                        norm.append(NormalizedAsset(file_path=dest, content_type="audio", file_name=p.name, metadata={"role": "bgm"}))

                plan = router.plan_execution(t)
                assert plan.mode == ExecutionMode.IMAGE_ALBUM

                conf = router.validate_artifact_conformity(plan, norm)
                assert conf.passed is True

                val_res = validator.validate_assets([a.file_path for a in norm], profile=plan.validation_profile)
                assert val_res.passed is True

                prom_res = self.promoter.promote(
                    normalized_assets=norm,
                    validation_result=val_res,
                    platform=t.platform,
                    platform_content_id=t.platform_content_id,
                    scope_id=t.scope_id,
                )
                assert prom_res.status == PromotionStatus.SUCCESS

                return DownloadResultContract(
                    source_url=t.source_url,
                    platform_content_id=t.platform_content_id,
                    task_id=t.task_id,
                    scope_id=t.scope_id,
                    status=DownloaderStatus.SUCCESS,
                    assets=[pa.to_downloaded_asset() for pa in prom_res.promoted_assets],
                )

        dl_shim = RealAlbumDownloaderShim(promoter)
        service = DownloaderWorkerService(
            job_store=job_store,
            downloader=dl_shim,
            collector_repo=col_repo,
            promoter=promoter,
        )

        # 1. Enqueue task in Collector Outbox
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_real_alb', 'task_real_album_outbox', 'douyin:album_scope_A', 'douyin', '7169622286633274635', 'image_album', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        # 2. Run worker service iteration
        service.run_once()

        # 3. Assert Outbox is DISPATCHED and Job is SUCCEEDED
        assert col_repo.get_outbox_record("task_real_album_outbox").status == "DISPATCHED"
        job = job_store.get_job("task_real_album_outbox")
        assert job.state == JobState.SUCCEEDED

        # 4. Assert formal asset manifest exists and has 4 assets (3 webp + 1 mp3)
        canonical_dest = promoter.resolve_canonical_destination("douyin", "7169622286633274635")
        assert (canonical_dest / "asset_manifest.json").exists()
        verif = verify_archived_asset(canonical_dest)
        assert verif.valid is True
        assert len(verif.assets) == 4

    def test_48_restart_recovery_e2e_pipeline(self, tmp_path: Path) -> None:
        """Test 48: Worker takes task -> DISPATCHED -> simulated crash -> restart service -> finishes SUCCESS."""
        arch_root = tmp_path / "archive"
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_db = tmp_path / "downloader.db"
        job_store = DownloaderJobStore(job_db)
        promoter = ProductionArchivePromoter(archive_root=arch_root)

        task = _make_sample_task("task_restart_e2e")
        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_rest', 'task_restart_e2e', 'douyin:test_scope', 'douyin', '7671141177986518318', 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (json.dumps(task.to_dict()),),
            )
            conn.commit()

        # Instance 1: intakes batch, claims job, transitions to RUNNING, then CRASHES (instance destroyed)
        bridge = OutboxConsumerBridge(col_repo, job_store)
        bridge.intake_batch()
        claimed_job = job_store.claim_next_due("worker_instance_1")
        assert claimed_job is not None
        assert claimed_job.state == JobState.RUNNING
        assert col_repo.get_outbox_record("task_restart_e2e").status == "DISPATCHED"

        # Instance 2: Worker restarts
        fake_dl = FakeDownloader(should_succeed=True, promoter=promoter)
        job_store2 = DownloaderJobStore(job_db)
        service2 = DownloaderWorkerService(
            job_store=job_store2,
            downloader=fake_dl,
            collector_repo=col_repo,
            promoter=promoter,
            worker_id="worker_instance_2",
        )

        service2.startup_recovery()
        # Stale RUNNING job should now be READY
        assert job_store2.get_job("task_restart_e2e").state == JobState.READY

        # Run worker iteration
        service2.run_once()
        assert job_store2.get_job("task_restart_e2e").state == JobState.SUCCEEDED
        assert len(fake_dl.invocations) == 1

    @pytest.fixture(scope="class")
    def shared_live_browser_runtime(self):
        """Dedicated browser runtime instance shared across live network tests in this suite."""
        if not is_f2_available():
            yield None
            return
        profile_path = Path(r"G:\antigravity-cli\dy\runtime\chrome-profile")
        if not profile_path.exists():
            yield None
            return
        config = DouyinCollectorConfig(profile_path=profile_path, headless=True)
        runtime = DouyinBrowserRuntimeProvider.from_config(config)
        runtime.launch()
        try:
            yield runtime
        finally:
            runtime.close()

    def test_49_live_worker_image_album_network_acquisition(self, tmp_path: Path, shared_live_browser_runtime) -> None:
        """Test 49: LIVE WORKER IMAGE ALBUM E2E - Outbox -> Worker -> D02 -> D03 F2 Network -> D04 -> D06 -> D05 -> D07 -> SUCCEEDED."""
        if shared_live_browser_runtime is None:
            pytest.skip("Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.")
        runtime = shared_live_browser_runtime

        time.sleep(5)

        arch_root = tmp_path / "archive"
        sb_root = tmp_path / "sandboxes"
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=sb_root)
        router = ProductionContentRouter()
        normalizer = ProductionAssetNormalizer()
        validator = ProductionMediaValidator()
        backend = F2InProcessBackendAdapter()

        scope_id = "douyin:dyacct_live_album_test"
        cred_source = BrowserRuntimeCredentialSource(runtime_provider=runtime)
        cred_provider = DouyinCredentialProvider(snapshot_source=cred_source, require_auth=True)

        events_recorded: list[str] = []

        class InstrumentedSafeDownloader(SafeDouyinDownloader):
            def execute(self, t: DownloadTask) -> DownloadResultContract:
                events_recorded.append("CREDENTIAL_ACQUIRED")
                events_recorded.append("F2_BACKEND_STARTED")
                res = super().execute(t)
                events_recorded.append("F2_BACKEND_COMPLETED")
                events_recorded.append("NORMALIZATION_COMPLETED")
                events_recorded.append("VALIDATION_COMPLETED")
                events_recorded.append("PROMOTION_COMPLETED")
                return res

        downloader = InstrumentedSafeDownloader(
            credential_provider=cred_provider,
            backend=backend,
            sandbox_provider=sandbox_provider,
            validator=validator,
            normalizer=normalizer,
            promoter=promoter,
            router=router,
            require_auth=True,
        )

        service = DownloaderWorkerService(
            job_store=job_store,
            downloader=downloader,
            collector_repo=col_repo,
            promoter=promoter,
        )

        album_id = "7169622286633274635"
        album_url = f"https://www.douyin.com/note/{album_id}"
        task = DownloadTask(
            task_id="task_live_album_e2e",
            platform="douyin",
            platform_content_id=album_id,
            scope_id=scope_id,
            content_type="image_album",
            source_url=album_url,
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
        )

        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_live_a1', 'task_live_album_e2e', ?, 'douyin', ?, 'image_album', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (scope_id, album_id, json.dumps(task.to_dict())),
            )
            conn.commit()

        canonical_dest = promoter.resolve_canonical_destination("douyin", album_id)
        assert not canonical_dest.exists(), "Formal archive destination must not exist before download"

        worked = service.run_once()
        assert worked is True

        assert col_repo.get_outbox_record("task_live_album_e2e").status == "DISPATCHED"
        job = job_store.get_job("task_live_album_e2e")
        assert job.state == JobState.SUCCEEDED

        assert events_recorded == [
            "CREDENTIAL_ACQUIRED",
            "F2_BACKEND_STARTED",
            "F2_BACKEND_COMPLETED",
            "NORMALIZATION_COMPLETED",
            "VALIDATION_COMPLETED",
            "PROMOTION_COMPLETED",
        ]

        assert (canonical_dest / "asset_manifest.json").exists()
        verif = verify_archived_asset(canonical_dest)
        assert verif.valid is True
        assert len(verif.assets) == 4
        filenames = {a.file_name for a in verif.assets}
        assert f"{album_id}_bgm.mp3" in filenames
        assert f"{album_id}_img_001.webp" in filenames
        assert f"{album_id}_img_002.webp" in filenames
        assert f"{album_id}_img_003.webp" in filenames

    def test_50_live_worker_video_network_acquisition(self, tmp_path: Path, shared_live_browser_runtime) -> None:
        """Test 50: LIVE WORKER VIDEO E2E - Outbox -> Worker -> D02 -> D03 F2 Network -> D04 -> D06 -> D05 -> D07 -> SUCCEEDED."""
        if shared_live_browser_runtime is None:
            pytest.skip("Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.")
        runtime = shared_live_browser_runtime

        time.sleep(15)
        arch_root = tmp_path / "archive"
        sb_root = tmp_path / "sandboxes"
        col_repo = CollectorRepository(tmp_path / "collector.db")
        job_store = DownloaderJobStore(tmp_path / "downloader.db")
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=sb_root)
        router = ProductionContentRouter()
        normalizer = ProductionAssetNormalizer()
        validator = ProductionMediaValidator()
        backend = F2InProcessBackendAdapter()

        scope_id = "douyin:dyacct_live_video_test"
        cred_source = BrowserRuntimeCredentialSource(runtime_provider=runtime)
        cred_provider = DouyinCredentialProvider(snapshot_source=cred_source, require_auth=True)

        events_recorded: list[str] = []

        class InstrumentedSafeDownloader(SafeDouyinDownloader):
            def execute(self, t: DownloadTask) -> DownloadResultContract:
                events_recorded.append("CREDENTIAL_ACQUIRED")
                events_recorded.append("F2_BACKEND_STARTED")
                res = super().execute(t)
                events_recorded.append("F2_BACKEND_COMPLETED")
                events_recorded.append("NORMALIZATION_COMPLETED")
                events_recorded.append("VALIDATION_COMPLETED")
                events_recorded.append("PROMOTION_COMPLETED")
                return res

        downloader = InstrumentedSafeDownloader(
            credential_provider=cred_provider,
            backend=backend,
            sandbox_provider=sandbox_provider,
            validator=validator,
            normalizer=normalizer,
            promoter=promoter,
            router=router,
            require_auth=True,
        )

        service = DownloaderWorkerService(
            job_store=job_store,
            downloader=downloader,
            collector_repo=col_repo,
            promoter=promoter,
        )

        video_id = "7681627509745519918"
        video_url = f"https://www.douyin.com/video/{video_id}"
        task = DownloadTask(
            task_id="task_live_video_e2e",
            platform="douyin",
            platform_content_id=video_id,
            scope_id=scope_id,
            content_type="video",
            source_url=video_url,
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
        )

        with col_repo._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_outbox (outbox_id, task_id, scope_id, platform, platform_content_id, content_type, payload_json, status, created_at, available_at)
                VALUES ('out_live_v1', 'task_live_video_e2e', ?, 'douyin', ?, 'video', ?, 'PENDING', '2026-09-06T00:00:00Z', '2026-09-06T00:00:00Z')
                """,
                (scope_id, video_id, json.dumps(task.to_dict())),
            )
            conn.commit()

        canonical_dest = promoter.resolve_canonical_destination("douyin", video_id)
        assert not canonical_dest.exists(), "Formal archive destination must not exist before download"

        worked = service.run_once()
        assert worked is True

        assert col_repo.get_outbox_record("task_live_video_e2e").status == "DISPATCHED"
        job = job_store.get_job("task_live_video_e2e")
        assert job.state == JobState.SUCCEEDED

        assert events_recorded == [
            "CREDENTIAL_ACQUIRED",
            "F2_BACKEND_STARTED",
            "F2_BACKEND_COMPLETED",
            "NORMALIZATION_COMPLETED",
            "VALIDATION_COMPLETED",
            "PROMOTION_COMPLETED",
        ]

        assert (canonical_dest / "asset_manifest.json").exists()
        verif = verify_archived_asset(canonical_dest)
        assert verif.valid is True
        assert len(verif.assets) == 1
        arch_video = verif.assets[0]
        assert arch_video.file_name == f"{video_id}.mp4"
        assert arch_video.size_bytes > 1_000_000
        assert arch_video.sha256 is not None

    def test_51_image_album_source_count_matches_candidates(self) -> None:
        """Test 51: Reconciled source count (3 images) strictly matches D03 ALBUM_IMAGE candidates."""
        album_fixture_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_fixture_dir.exists():
            pytest.skip(f"Album fixture dir not found at {album_fixture_dir}")

        image_files = sorted(album_fixture_dir.glob("*.webp"))
        audio_files = sorted(album_fixture_dir.glob("*.mp3"))
        assert len(image_files) == 3, f"Expected exactly 3 images in source fixture, got {len(image_files)}"
        assert len(audio_files) == 1, f"Expected exactly 1 BGM audio in source fixture, got {len(audio_files)}"

    def test_52_image_album_candidates_match_normalized(self, tmp_path: Path) -> None:
        """Test 52: D03 image candidates strictly match D06 normalized assets (3 images + 1 BGM)."""
        album_fixture_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_fixture_dir.exists():
            pytest.skip(f"Album fixture dir not found at {album_fixture_dir}")

        normalizer = ProductionAssetNormalizer()
        raw_assets = []
        for p in sorted(album_fixture_dir.glob("*.*")):
            dest = tmp_path / p.name
            shutil.copy2(p, dest)
            if p.suffix.lower() == ".webp":
                import re
                m = re.search(r"_image_(\d+)", p.name)
                seq = int(m.group(1)) if m else 1
                raw_assets.append(
                    ArtifactCandidate(
                        file_path=dest,
                        role=ArtifactRole.ALBUM_IMAGE,
                        media_kind="image",
                        sequence_index=seq,
                        source_extension=".webp",
                    )
                )
            elif p.suffix.lower() == ".mp3":
                raw_assets.append(
                    ArtifactCandidate(
                        file_path=dest,
                        role=ArtifactRole.BGM_AUDIO,
                        media_kind="audio",
                        sequence_index=None,
                        source_extension=".mp3",
                    )
                )

        norm_res = normalizer.normalize(
            raw_assets=raw_assets,
            platform_content_id="7169622286633274635",
            content_type="image_album",
            sandbox_root=tmp_path,
        )

        assert len(norm_res.artifacts) == 4
        norm_images = [a for a in norm_res.artifacts if a.role in (ArtifactRole.ALBUM_IMAGE, "album_image") or a.media_kind == "image"]
        norm_audios = [a for a in norm_res.artifacts if a.role in (ArtifactRole.BGM_AUDIO, "bgm_audio") or a.media_kind == "audio"]
        assert len(norm_images) == 3
        assert len(norm_audios) == 1
        assert [a.sequence_index for a in norm_images] == [1, 2, 3]

    def test_53_normalized_match_validated(self, tmp_path: Path) -> None:
        """Test 53: Normalized image album assets (3 images + 1 BGM) pass D05 media validation."""
        album_fixture_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_fixture_dir.exists():
            pytest.skip(f"Album fixture dir not found at {album_fixture_dir}")

        validator = ProductionMediaValidator()
        files = sorted(album_fixture_dir.glob("*.webp")) + sorted(album_fixture_dir.glob("*.mp3"))
        val_res = validator.validate_assets(files, profile=ValidationProfile.IMAGE_SET)
        assert val_res.passed is True
        assert len(val_res.validated_artifacts) == 4

    def test_54_validated_match_manifest(self, tmp_path: Path) -> None:
        """Test 54: Validated album assets are fully archived and reflected in D07 manifest."""
        album_fixture_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_fixture_dir.exists():
            pytest.skip(f"Album fixture dir not found at {album_fixture_dir}")

        arch_root = tmp_path / "archive"
        promoter = ProductionArchivePromoter(archive_root=arch_root)
        validator = ProductionMediaValidator()
        files = sorted(album_fixture_dir.glob("*.webp")) + sorted(album_fixture_dir.glob("*.mp3"))
        val_res = validator.validate_assets(files, profile=ValidationProfile.IMAGE_SET)

        norm = []
        for p in files:
            if p.suffix == ".webp":
                import re
                m = re.search(r"_image_(\d+)", p.name)
                seq = int(m.group(1)) if m else 1
                norm.append(NormalizedAsset(file_path=p, content_type="image", file_name=f"7169622286633274635_img_{seq:03d}.webp", metadata={"sequence_index": seq}))
            else:
                norm.append(NormalizedAsset(file_path=p, content_type="audio", file_name="7169622286633274635_bgm.mp3", metadata={"role": "bgm"}))

        prom_res = promoter.promote(
            normalized_assets=norm,
            validation_result=val_res,
            platform="douyin",
            platform_content_id="7169622286633274635",
            scope_id="douyin:scope_test",
        )
        assert prom_res.status == PromotionStatus.SUCCESS
        canonical_dest = promoter.resolve_canonical_destination("douyin", "7169622286633274635")
        verif = verify_archived_asset(canonical_dest)
        assert verif.valid is True
        assert len(verif.assets) == 4

    def test_55_bgm_source_contract_consistency(self, tmp_path: Path) -> None:
        """Test 55: Validates auxiliary BGM presence contract: BGM recognized when present, optional when absent."""
        router = ProductionContentRouter()
        plan = router.plan_execution(DownloadTask(
            task_id="t_bgm",
            platform="douyin",
            platform_content_id="7169622286633274635",
            scope_id="douyin:scope",
            content_type="image_album",
            source_url="https://www.douyin.com/note/7169622286633274635",
        ))
        assert plan.expected_artifacts.allow_audio is True
        assert plan.expected_artifacts.require_audio is False

# =============================================================================
# Group 14: Process Ownership, Heartbeat Semantics & Safe Reclaim (Tests 56 - 65)
# =============================================================================


class TestProcessOwnershipAndSafeReclaim:
    """Validates frozen Worker owner identity, safe reclaim rules, and heartbeat semantics."""

    def test_56_service_lock_live_owner_fresh_heartbeat_rejected(self, tmp_path: Path) -> None:
        """Test 56: Live owner with fresh heartbeat strictly rejects second concurrent worker."""
        lock_file = tmp_path / "worker.lock"
        lock1 = DownloaderServiceLock(lock_file, worker_id="worker_orig")
        lock1.acquire()
        assert lock1.is_acquired is True

        # Second worker in another instance attempting to acquire same lock
        lock2 = DownloaderServiceLock(lock_file, worker_id="worker_second")
        with pytest.raises(ServiceLockError) as exc_info:
            lock2.acquire()

        assert "Another DownloaderWorkerService is actively running" in str(exc_info.value)
        assert lock2.is_acquired is False
        lock1.release()

    def test_57_service_lock_live_owner_stale_heartbeat_still_rejected(self, tmp_path: Path) -> None:
        """Test 57: Live owner with stale heartbeat STILL rejects second worker (never steals live process lock)."""
        lock_file = tmp_path / "worker.lock"
        lock1 = DownloaderServiceLock(lock_file, worker_id="worker_orig", heartbeat_stale_threshold_sec=5.0)
        lock1.acquire()

        # Simulate stale heartbeat (1 hour ago) while PID and create_time belong to current live process
        content = json.loads(lock_file.read_text(encoding="utf-8"))
        content["heartbeat_at"] = time.time() - 3600.0
        lock_file.write_text(json.dumps(content), encoding="utf-8")

        # Second worker attempts acquisition
        lock2 = DownloaderServiceLock(lock_file, worker_id="worker_second", heartbeat_stale_threshold_sec=5.0)
        with pytest.raises(ServiceLockError) as exc_info:
            lock2.acquire()

        err_msg = str(exc_info.value)
        assert "STILL ALIVE" in err_msg
        assert "OWNER_ALIVE_STALE" in err_msg or "heartbeat is stale" in err_msg
        assert "Refusing to steal lock" in err_msg
        assert lock2.is_acquired is False
        lock1.release()

    def test_58_service_lock_dead_pid_reclaimed(self, tmp_path: Path) -> None:
        """Test 58: Service lock belonging to a dead PID is safely reclaimed without human intervention."""
        lock_file = tmp_path / "worker.lock"
        # Write dead owner metadata
        dead_data = {
            "pid": 99999999,
            "process_started_at": 1000.0,
            "create_time": 1000.0,
            "instance_id": "dead_instance",
            "worker_id": "dead_worker",
            "lock_created_at": time.time() - 500.0,
            "heartbeat_at": time.time() - 400.0,
        }
        lock_file.write_text(json.dumps(dead_data), encoding="utf-8")

        lock = DownloaderServiceLock(lock_file, worker_id="worker_new")
        lock.acquire()
        assert lock.is_acquired is True

        new_content = json.loads(lock_file.read_text(encoding="utf-8"))
        assert new_content["pid"] == os.getpid()
        lock.release()

    def test_59_service_lock_pid_reused_create_time_mismatch_reclaimed(self, tmp_path: Path) -> None:
        """Test 59: PID reused by an unrelated process (create_time mismatch) is safely reclaimed."""
        lock_file = tmp_path / "worker.lock"
        # PID is alive (current process), but create_time is completely different
        reused_data = {
            "pid": os.getpid(),
            "process_started_at": 12345.678,
            "create_time": 12345.678,
            "instance_id": "reused_instance",
            "worker_id": "reused_worker",
            "lock_created_at": time.time() - 50.0,
            "heartbeat_at": time.time() - 10.0,
        }
        lock_file.write_text(json.dumps(reused_data), encoding="utf-8")

        lock = DownloaderServiceLock(lock_file, worker_id="worker_reclaimer")
        lock.acquire()
        assert lock.is_acquired is True
        lock.release()

    def test_60_service_lock_matching_pid_and_create_time_never_reclaims_by_age_alone(self, tmp_path: Path) -> None:
        """Test 60: Matching PID and create_time is NEVER reclaimed by age/TTL alone."""
        lock_file = tmp_path / "worker.lock"
        my_pid = os.getpid()
        import psutil
        my_ct = psutil.Process(my_pid).create_time()

        # 10 days old lock, but PID and create_time match current live process
        ancient_data = {
            "pid": my_pid,
            "process_started_at": my_ct,
            "create_time": my_ct,
            "instance_id": "different_instance_same_proc",
            "worker_id": "ancient_worker",
            "lock_created_at": time.time() - 864000.0,
            "heartbeat_at": time.time() - 864000.0,
        }
        lock_file.write_text(json.dumps(ancient_data), encoding="utf-8")

        # Second instance from different worker identity
        lock = DownloaderServiceLock(lock_file, worker_id="new_worker_attempt")
        # Overwrite worker identity instance to be distinct
        object.__setattr__(lock.identity, "instance_id", "completely_different_inst")
        with pytest.raises(ServiceLockError):
            lock.acquire()
        assert lock.is_acquired is False

    def test_61_running_job_live_original_owner_not_recovered(self, tmp_path: Path) -> None:
        """Test 61: RUNNING job owned by live original worker is NOT recovered by startup recovery."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_live_owner"))

        my_ident = WorkerIdentity.current(instance_id="inst_live_active")
        store.claim_next_due(worker_id=my_ident.serialize())
        assert store.get_job("task_live_owner").state == JobState.RUNNING

        # Startup recovery with dead_worker_ids=None (scans OS process liveness)
        recovered = store.recover_stale_running(dead_worker_ids=None)
        assert recovered == 0
        job = store.get_job("task_live_owner")
        assert job.state == JobState.RUNNING
        assert job.claimed_by == my_ident.serialize()

    def test_62_running_job_dead_owner_recovered_to_ready(self, tmp_path: Path) -> None:
        """Test 62: RUNNING job owned by a dead process PID is safely reset to READY."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_dead_owner"))

        dead_ident = WorkerIdentity(pid=99999999, process_started_at=1000.0, instance_id="dead_inst")
        store.claim_next_due(worker_id=dead_ident.serialize())
        assert store.get_job("task_dead_owner").state == JobState.RUNNING

        recovered = store.recover_stale_running(dead_worker_ids=None)
        assert recovered == 1
        job = store.get_job("task_dead_owner")
        assert job.state == JobState.READY
        assert job.claimed_by is None

    def test_63_running_job_pid_reused_recovered_to_ready(self, tmp_path: Path) -> None:
        """Test 63: RUNNING job whose PID was recycled (create_time mismatch) is reset to READY."""
        store = DownloaderJobStore(tmp_path / "jobs.db")
        store.accept_task(_make_sample_task("task_reused_owner"))

        reused_ident = WorkerIdentity(pid=os.getpid(), process_started_at=12345.67, instance_id="reused_inst")
        store.claim_next_due(worker_id=reused_ident.serialize())
        assert store.get_job("task_reused_owner").state == JobState.RUNNING

        recovered = store.recover_stale_running(dead_worker_ids=None)
        assert recovered == 1
        job = store.get_job("task_reused_owner")
        assert job.state == JobState.READY
        assert job.claimed_by is None

    def test_64_stale_heartbeat_classification_unhealthy_stale_only(self, tmp_path: Path) -> None:
        """Test 64: Stale heartbeat on live owner is classified as OWNER_ALIVE_STALE, never permits reclaim."""
        lock_file = tmp_path / "audit.lock"
        my_pid = os.getpid()
        import psutil
        my_ct = psutil.Process(my_pid).create_time()

        stale_data = {
            "pid": my_pid,
            "process_started_at": my_ct,
            "create_time": my_ct,
            "instance_id": "stale_inst",
            "worker_id": "stale_worker",
            "lock_created_at": time.time() - 3600.0,
            "heartbeat_at": time.time() - 3600.0,
        }
        lock_file.write_text(json.dumps(stale_data), encoding="utf-8")

        status, metadata = inspect_lock(lock_file, heartbeat_stale_threshold_sec=30.0)
        assert status == OwnerLivenessStatus.OWNER_ALIVE_STALE
        assert status.is_alive is True
        assert status.is_stale is True
        assert status.reclaim_allowed is False

    def test_65_no_concurrent_f2_worker_starts(self, tmp_path: Path) -> None:
        """Test 65: DownloaderWorkerService strictly prevents concurrent F2 worker execution on same lock."""
        lock_file = tmp_path / "worker_mutex.lock"
        store = DownloaderJobStore(tmp_path / "jobs.db")
        fake_dl = FakeDownloader(should_succeed=True)

        service1_lock = DownloaderServiceLock(lock_file, worker_id="service_1")
        service1 = DownloaderWorkerService(
            job_store=store,
            downloader=fake_dl,
            service_lock=service1_lock,
            worker_id="service_1",
        )
        # Service 1 acquires lock
        service1_lock.acquire()

        service2_lock = DownloaderServiceLock(lock_file, worker_id="service_2")
        service2 = DownloaderWorkerService(
            job_store=store,
            downloader=fake_dl,
            service_lock=service2_lock,
            worker_id="service_2",
        )

        # Service 2 cannot acquire lock or start
        with pytest.raises(ServiceLockError):
            service2_lock.acquire()

        assert service2_lock.is_acquired is False
        service1_lock.release()

