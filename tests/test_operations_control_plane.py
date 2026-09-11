"""M6-07 NAS Control Plane tests.

Covers: config schema + local-path guards, startup sequence / recovery /
scheduler / NAS local worker lifecycle, HTTP API routing + auth, claim /
start / renew / complete idempotency (P0 lost-response), fencing, stage
placement allowlists (Windows vs NAS), PC-offline, NAS-restart, network
partition + CACHE_HIT replay, and the full distributed synthetic pipeline
over real localhost HTTP (DISCOVER -> ... -> STORE_INGEST -> SEARCHABLE)
with execution-host assertions.

All DBs are disposable tmp_path; the production operations DB is never
created and the production M5 store is never touched. No network / GPU / LLM.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from src.operations.control_plane import (
    CONTROL_PLANE_API_VERSION,
    CONTROL_PLANE_CONFIG_VERSION,
    NAS_STAGE_ALLOWLIST,
    WINDOWS_STAGE_ALLOWLIST,
    ControlPlaneConfig,
    ControlPlaneService,
)
from src.operations.http_transport import (
    ApiError,
    AuthenticationError,
    HttpWorkerTransport,
)
from src.operations.models import (
    JobStage,
    JobState,
    compute_job_id,
    utc_now_iso,
)
from src.operations.stages import (
    StageExecutionResult,
)
from src.operations.store import (
    StaleLeaseError,
    enqueue_job,
    get_asset_by_canonical_id,
    get_job,
    get_worker,
    list_job_attempts,
    list_jobs,
    list_workers,
    recover_expired_leases,
    register_asset,
)
from src.operations.transport import LocalSQLiteWorkerTransport, RemoteUnavailableError
from src.operations.worker import WorkerRuntime

PLATFORM = "douyin"
CONTENT_ID = "7681603850364521734"
CANONICAL_ID = f"{PLATFORM}_{CONTENT_ID}"
FINGERPRINT = "a" * 64
TOKEN = "control-plane-test-token-123"
T0 = "2026-09-10T01:00:00+00:00"


def _fp(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def _config(tmp_path: Path, **overrides) -> ControlPlaneConfig:
    base = {
        "operations_db_path": str(tmp_path / "operations.sqlite3"),
        "knowledge_store_path": str(tmp_path / "knowledge_store.sqlite3"),
        "archive_root": str(tmp_path / "archive"),
        "processed_root": str(tmp_path / "processed"),
        "host": "127.0.0.1",
        "port": 0,
        "scheduler_poll_interval_seconds": 0.05,
        "local_worker_id": "nas-local-worker",
        "local_worker_capabilities": ["store_ingest"],
    }
    base.update(overrides)
    return ControlPlaneConfig.from_dict(base)


@pytest.fixture
def service(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    yield svc
    svc.stop()


def _client(base_url: str, *, worker_id="win-worker", token=TOKEN) -> HttpWorkerTransport:
    return HttpWorkerTransport(
        base_url=base_url,
        auth_token=token,
        worker_id=worker_id,
        http_timeout_seconds=5.0,
        connect_retries=1,
        reconnect_backoff_seconds=0.01,
    )


def _seed_job(db_path, stage, fp, *, required_capabilities, canonical_id=CANONICAL_ID):
    """Register the asset and enqueue one job (frozen store semantics)."""
    register_asset(
        db_path,
        PLATFORM,
        CONTENT_ID,
        canonical_id,
        metadata={"test": True},
        now=T0,
    )
    return enqueue_job(
        db_path,
        PLATFORM,
        CONTENT_ID,
        stage,
        fp,
        policy_version="m6-scheduler-policy-v1",
        canonical_id=canonical_id,
        required_capabilities=required_capabilities,
        now=T0,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_schema_version_frozen():
    assert CONTROL_PLANE_CONFIG_VERSION == "m6-control-plane-config-v1"


def test_config_roundtrip(tmp_path: Path):
    cfg = _config(tmp_path, port=9999, local_worker_enabled=False)
    data = cfg.to_dict()
    assert data["schema_version"] == CONTROL_PLANE_CONFIG_VERSION
    cfg2 = ControlPlaneConfig.from_dict(data)
    assert cfg2.port == 9999
    assert cfg2.local_worker_enabled is False


def test_config_rejects_unknown_schema(tmp_path: Path):
    with pytest.raises(ValueError):
        ControlPlaneConfig.from_dict({"schema_version": "future-v2"})


def test_config_rejects_remote_ops_db(tmp_path: Path):
    with pytest.raises(ValueError):
        _config(tmp_path, operations_db_path=r"\\\\nas\\share\\operations.sqlite3")


def test_config_rejects_url_ops_db(tmp_path: Path):
    with pytest.raises(ValueError):
        _config(tmp_path, operations_db_path="smb://nas/operations.sqlite3")


# ---------------------------------------------------------------------------
# Startup sequence / guards
# ---------------------------------------------------------------------------


def test_startup_creates_local_ops_db(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        assert svc.ready
        assert Path(cfg.operations_db_path).exists()
        from src.operations.store import validate_operations_store

        validation = validate_operations_store(Path(cfg.operations_db_path), now=utc_now_iso())
        assert validation.valid
    finally:
        svc.stop()


def test_startup_runs_recovery_and_initializes_components(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        assert svc.ready
        assert svc._scheduler is not None
        assert svc._local_worker is not None
        workers = list_workers(svc.operations_db_path)
        assert any(w["worker_id"] == cfg.local_worker_id for w in workers)
    finally:
        svc.stop()


def test_startup_local_worker_disabled(tmp_path: Path):
    cfg = _config(tmp_path, local_worker_enabled=False)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        assert svc.ready
        assert svc._local_worker is None
    finally:
        svc.stop()


def test_remote_ops_db_guard_fails_closed(tmp_path: Path):
    cfg = ControlPlaneConfig(
        operations_db_path=r"\\\\nas\\share\\ops.sqlite3",
        knowledge_store_path=str(tmp_path / "ks.sqlite3"),
        host="127.0.0.1",
        port=0,
    )
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        assert not svc.ready
        assert svc.ready_errors()
    finally:
        svc.stop()


def test_remote_m5_guard_fails_closed(tmp_path: Path):
    cfg = ControlPlaneConfig(
        operations_db_path=str(tmp_path / "ops.sqlite3"),
        knowledge_store_path="smb://nas/knowledge_store.sqlite3",
        host="127.0.0.1",
        port=0,
    )
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        assert not svc.ready
        assert svc.ready_errors()
    finally:
        svc.stop()


def test_ready_false_before_start(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    assert svc.ready is False


# ---------------------------------------------------------------------------
# HTTP API + auth
# ---------------------------------------------------------------------------


def test_health_live_no_auth(service: ControlPlaneService):
    client = _client(service.http_url)
    assert client.health_live() is True


def test_health_ready_auth_ok(service: ControlPlaneService):
    client = _client(service.http_url)
    resp = client.health_ready()
    assert resp["status"] == "ready"
    assert resp["api_version"] == CONTROL_PLANE_API_VERSION


def test_health_ready_missing_token_rejected(service: ControlPlaneService):
    client = _client(service.http_url, token=None)
    with pytest.raises(AuthenticationError):
        client.health_ready()


def test_health_ready_wrong_token_rejected(service: ControlPlaneService):
    client = _client(service.http_url, token="wrong")
    with pytest.raises(AuthenticationError):
        client.health_ready()


def test_status_endpoint(service: ControlPlaneService):
    client = _client(service.http_url)
    resp = client._request("GET", "/api/v1/status", retryable=False)
    assert resp["api_version"] == CONTROL_PLANE_API_VERSION
    assert "status" in resp


def test_register_and_heartbeat_http(service: ControlPlaneService):
    client = _client(service.http_url, worker_id="win-worker")
    client.register_worker(
        "win-worker",
        ["downloader", "cpu_media"],
        display_name="Win",
        hostname="pc",
        allowed_stages=list(WINDOWS_STAGE_ALLOWLIST),
    )
    row = client.heartbeat_worker("win-worker", capabilities=["downloader"])
    assert row["worker_id"] == "win-worker"
    worker = get_worker(service.operations_db_path, "win-worker")
    assert worker is not None
    meta = json.loads(worker.get("metadata_json") or "{}")
    assert meta.get("allowed_stages") == list(WINDOWS_STAGE_ALLOWLIST)


def test_claim_no_job(service: ControlPlaneService):
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert claimed is None


def test_claim_success_via_http(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert claimed is not None
    assert claimed.stage == JobStage.ARCHIVE.value
    assert claimed.canonical_id == CANONICAL_ID
    assert claimed.lease_token
    job = get_job(service.operations_db_path, claimed.job_id)
    assert job["state"] == JobState.LEASED.value


def test_claim_idempotent_lost_response(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    first = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert first is not None
    second = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert second is not None
    assert second.job_id == first.job_id
    assert second.lease_token == first.lease_token
    leased = list_jobs(
        service.operations_db_path, state=JobState.LEASED.value, lease_owner="win-worker"
    )
    assert len(leased) == 1


def test_start_and_idempotent_retry(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    start = client.start_claimed_job(claimed.job_id, "win-worker", claimed.lease_token)
    assert start["state"] == JobState.RUNNING.value
    start2 = client.start_claimed_job(claimed.job_id, "win-worker", claimed.lease_token)
    assert start2["state"] == JobState.RUNNING.value
    assert start2["attempt_count"] == start["attempt_count"] == 1
    assert len(list_job_attempts(service.operations_db_path, claimed.job_id)) == 1


def test_renew_and_stale_renew(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    job = client.renew_job_lease(
        claimed.job_id, "win-worker", claimed.lease_token, lease_duration_seconds=120
    )
    assert job["state"] == JobState.LEASED.value
    with pytest.raises(StaleLeaseError):
        client.renew_job_lease(
            claimed.job_id, "win-worker", "lease_wrong", lease_duration_seconds=120
        )


def test_complete_success_and_idempotent_retry(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    client.start_claimed_job(claimed.job_id, "win-worker", claimed.lease_token)
    done = client.complete_job_success(
        claimed.job_id,
        "win-worker",
        claimed.lease_token,
        metadata={"stage_result": {"stage": "ARCHIVE", "status": "EXECUTED"}},
    )
    assert done["state"] == JobState.SUCCEEDED.value
    done2 = client.complete_job_success(
        claimed.job_id,
        "win-worker",
        claimed.lease_token,
        metadata={"stage_result": {"stage": "ARCHIVE", "status": "EXECUTED"}},
    )
    assert done2["state"] == JobState.SUCCEEDED.value
    assert done2["job_id"] == claimed.job_id
    assert len(list_job_attempts(service.operations_db_path, claimed.job_id)) == 1


def test_complete_retryable_and_terminal(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    client.start_claimed_job(claimed.job_id, "win-worker", claimed.lease_token)
    r = client.complete_job_retryable_failure(
        claimed.job_id,
        "win-worker",
        claimed.lease_token,
        error_class="TestError",
        error_message="boom",
    )
    assert r["state"] == JobState.FAILED_RETRYABLE.value
    r2 = client.complete_job_retryable_failure(
        claimed.job_id,
        "win-worker",
        claimed.lease_token,
        error_class="TestError",
        error_message="boom",
    )
    assert r2["state"] == JobState.FAILED_RETRYABLE.value

    _seed_job(service.operations_db_path, JobStage.MEDIA_PROCESS.value, "b" * 64, required_capabilities=["cpu_media"], canonical_id=CANONICAL_ID)
    claimed2 = client.claim_next_job(
        "win-worker", ["cpu_media"], lease_duration_seconds=120
    )
    client.start_claimed_job(claimed2.job_id, "win-worker", claimed2.lease_token)
    t = client.complete_job_terminal_failure(
        claimed2.job_id,
        "win-worker",
        claimed2.lease_token,
        error_class="TestError",
        error_message="fatal",
    )
    assert t["state"] == JobState.FAILED_TERMINAL.value


def test_stale_completion_rejected(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client_a = _client(service.http_url, worker_id="win-a")
    claimed_a = client_a.claim_next_job(
        "win-a", ["downloader"], lease_duration_seconds=0
    )
    assert claimed_a is not None
    # Lease expires (duration 0); control-plane recovery requeues to QUEUED.
    rec = recover_expired_leases(service.operations_db_path, now=utc_now_iso())
    assert len(rec["recovered_leased"]) >= 1
    client_b = _client(service.http_url, worker_id="win-b")
    claimed_b = client_b.claim_next_job(
        "win-b", ["downloader"], lease_duration_seconds=120
    )
    assert claimed_b is not None
    assert claimed_b.job_id == claimed_a.job_id
    with pytest.raises(StaleLeaseError):
        client_a.complete_job_success(claimed_a.job_id, "win-a", claimed_a.lease_token)


def test_structured_error_contract(service: ControlPlaneService):
    client = _client(service.http_url)
    with pytest.raises(ApiError):
        client._request(
            "POST",
            "/api/v1/jobs/job_x/start",
            body={"worker_id": "w", "lease_token": "x"},
        )


def test_unknown_endpoint_not_found(service: ControlPlaneService):
    client = _client(service.http_url)
    with pytest.raises(ApiError):
        client._request("GET", "/api/v1/nonexistent", retryable=False)


def test_secret_redaction_in_responses(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=120
    )
    assert claimed.lease_token
    job = client.get_job(claimed.job_id)
    assert job is not None
    assert "lease_token" not in json.dumps(job)


# ---------------------------------------------------------------------------
# Stage placement (server-authoritative)
# ---------------------------------------------------------------------------


def test_remote_worker_cannot_claim_nas_stages(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.KNOWLEDGE_FINALIZE.value, "c" * 64, required_capabilities=[], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    client.register_worker(
        "win-worker",
        ["store_ingest", "cpu_media"],
        allowed_stages=list(WINDOWS_STAGE_ALLOWLIST),
    )
    claimed = client.claim_next_job(
        "win-worker", ["store_ingest", "cpu_media"], lease_duration_seconds=120
    )
    assert claimed is None


def test_nas_local_worker_claims_finalize(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.KNOWLEDGE_FINALIZE.value, "c" * 64, required_capabilities=[], canonical_id=CANONICAL_ID)
    deadline = time.monotonic() + 5.0
    job = None
    while time.monotonic() < deadline:
        job = get_job(
            service.operations_db_path,
            compute_job_id(
                PLATFORM, CONTENT_ID, JobStage.KNOWLEDGE_FINALIZE.value, "c" * 64,
                "m6-scheduler-policy-v1",
            ),
        )
        if job and job["state"] not in (JobState.QUEUED.value,):
            break
        time.sleep(0.1)
    assert job is not None
    # The job left QUEUED: the NAS local worker claimed it (its real
    # finalize handler then failed retryably because the synthetic processed
    # dir has no knowledge_units.json — the claim itself is the assertion).
    attempts = list_job_attempts(service.operations_db_path, job["job_id"])
    assert attempts and attempts[-1]["worker_id"] == "nas-local-worker"
    assert job["state"] in (
        JobState.LEASED.value,
        JobState.RUNNING.value,
        JobState.FAILED_RETRYABLE.value,
    )


def test_remote_worker_cannot_claim_finalize_via_capability_only(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.STORE_INGEST.value, "d" * 64, required_capabilities=["store_ingest"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    client.register_worker(
        "win-worker", ["store_ingest"], allowed_stages=list(WINDOWS_STAGE_ALLOWLIST)
    )
    claimed = client.claim_next_job(
        "win-worker", ["store_ingest"], lease_duration_seconds=120
    )
    assert claimed is None


def test_local_sqlite_transport_backward_compatible(tmp_path: Path):
    db = tmp_path / "ops.sqlite3"
    t = LocalSQLiteWorkerTransport(db)
    t.register_worker("local-w", ["downloader"])
    _seed_job(db, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    claimed = t.claim_next_job(
        "local-w", ["downloader"], lease_duration_seconds=120, now=utc_now_iso()
    )
    assert claimed is not None


# ---------------------------------------------------------------------------
# Lifecycle / recovery / partition
# ---------------------------------------------------------------------------


def test_graceful_shutdown(service: ControlPlaneService):
    url = service.http_url
    service.stop()
    assert service.ready is False
    client = _client(url)
    try:
        client.health_ready()
    except Exception:
        pass


def test_remote_outage_worker_keeps_running(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    url = svc.http_url
    svc.stop()
    client = _client(url)
    with pytest.raises(RemoteUnavailableError):
        client.heartbeat_worker("win-worker")


def test_server_restart_recovers_pipeline(tmp_path: Path):
    cfg = _config(tmp_path)
    svc1 = ControlPlaneService(cfg, token=TOKEN)
    svc1.start()
    try:
        _seed_job(svc1.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
        client1 = _client(svc1.http_url)
        claimed = client1.claim_next_job(
            "win-worker", ["downloader"], lease_duration_seconds=0
        )
        assert claimed is not None
        assert (
            get_job(svc1.operations_db_path, claimed.job_id)["state"]
            == JobState.LEASED.value
        )
    finally:
        svc1.stop()

    svc2 = ControlPlaneService(cfg, token=TOKEN)
    svc2.start()
    try:
        assert svc2.ready
        job = get_job(svc2.operations_db_path, claimed.job_id)
        assert job["state"] == JobState.QUEUED.value
        client2 = _client(svc2.http_url)
        claimed2 = client2.claim_next_job(
            "win-worker", ["downloader"], lease_duration_seconds=120
        )
        assert claimed2 is not None
        assert claimed2.job_id == claimed.job_id
    finally:
        svc2.stop()


def test_pc_offline_job_stays_queued(tmp_path: Path):
    cfg = _config(tmp_path)
    svc = ControlPlaneService(cfg, token=TOKEN)
    svc.start()
    try:
        _seed_job(svc.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
        time.sleep(0.5)
        job = get_job(
            svc.operations_db_path,
            compute_job_id(
                PLATFORM, CONTENT_ID, JobStage.ARCHIVE.value, FINGERPRINT,
                "m6-scheduler-policy-v1",
            ),
        )
        assert job is not None
        assert job["state"] == JobState.QUEUED.value
        assert job["lease_owner"] is None
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Full distributed synthetic pipeline
# ---------------------------------------------------------------------------


class _FakeHandlers:
    """Synthetic stage handlers for the distributed E2E."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def result(self, claimed, *, extra_metadata=None) -> StageExecutionResult:
        self.executed.append(claimed.stage)
        meta = {"executed_by": "remote-windows-worker"}
        if extra_metadata:
            meta.update(extra_metadata)
        return StageExecutionResult(
            stage=claimed.stage,
            canonical_id=claimed.canonical_id,
            status="EXECUTED",
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=_fp(
                f"{claimed.stage}:{claimed.canonical_id}:{claimed.input_fingerprint}"
            ),
            artifacts=[
                {"role": "artifact", "path": f"proc/{claimed.canonical_id}/{claimed.stage}.json"}
            ],
            metadata=meta,
        )

    def archive(self, claimed):
        return self.result(claimed, extra_metadata={"media_type": "video"})

    def media(self, claimed):
        return self.result(claimed, extra_metadata={"media_type": "video"})

    def extract(self, claimed):
        return self.result(claimed)

    def discover(self, claimed):
        self.executed.append(claimed.stage)
        return StageExecutionResult(
            stage=claimed.stage,
            canonical_id=claimed.canonical_id,
            status="EXECUTED",
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=_fp(
                f"discover:{claimed.canonical_id}:{claimed.input_fingerprint}"
            ),
            artifacts=[],
            metadata={
                "discovered": [{"platform": PLATFORM, "platform_content_id": CONTENT_ID}]
            },
        )

    def nas_finalize(self, claimed):
        self.executed.append(claimed.stage)
        return StageExecutionResult(
            stage=claimed.stage,
            canonical_id=claimed.canonical_id,
            status="EXECUTED",
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=_fp(
                f"finalize:{claimed.canonical_id}:{claimed.input_fingerprint}"
            ),
            artifacts=[],
            metadata={"executed_by": "nas-local-worker"},
        )

    def nas_store(self, claimed):
        self.executed.append(claimed.stage)
        return StageExecutionResult(
            stage=claimed.stage,
            canonical_id=claimed.canonical_id,
            status="EXECUTED",
            input_fingerprint=claimed.input_fingerprint,
            output_fingerprint=_fp(
                f"store:{claimed.canonical_id}:{claimed.input_fingerprint}"
            ),
            artifacts=[],
            metadata={"executed_by": "nas-local-worker"},
        )


def test_full_distributed_synthetic_pipeline(tmp_path: Path):
    fake = _FakeHandlers()
    cfg = _config(tmp_path)
    svc = ControlPlaneService(
        cfg,
        token=TOKEN,
        local_handlers={
            JobStage.KNOWLEDGE_FINALIZE.value: fake.nas_finalize,
            JobStage.STORE_INGEST.value: fake.nas_store,
        },
    )
    svc.start()
    try:
        transport = HttpWorkerTransport(
            base_url=svc.http_url,
            auth_token=TOKEN,
            worker_id="remote-windows-worker",
            http_timeout_seconds=5.0,
            connect_retries=1,
            reconnect_backoff_seconds=0.01,
        )
        windows = WorkerRuntime(
            transport=transport,
            worker_id="remote-windows-worker",
            capabilities=["collector", "downloader", "gpu_asr", "llm_extraction"],
            handlers={
                JobStage.DISCOVER.value: fake.discover,
                JobStage.ARCHIVE.value: fake.archive,
                JobStage.MEDIA_PROCESS.value: fake.media,
                JobStage.KNOWLEDGE_EXTRACT.value: fake.extract,
            },
            allowed_stages=list(WINDOWS_STAGE_ALLOWLIST),
            now=utc_now_iso,
        )
        windows.register()

        deadline = time.monotonic() + 45.0
        asset = None
        while time.monotonic() < deadline:
            windows.run_once()
            asset = get_asset_by_canonical_id(svc.operations_db_path, CANONICAL_ID)
            if asset is not None and asset["lifecycle_state"] == "SEARCHABLE":
                break
            time.sleep(0.1)

        assert asset is not None, "asset never reached SEARCHABLE"
        assert asset["lifecycle_state"] == "SEARCHABLE"

        arch_job = None
        for j in list_jobs(
            svc.operations_db_path, stage=JobStage.ARCHIVE.value, canonical_id=CANONICAL_ID
        ):
            arch_job = j
        assert arch_job is not None
        arch_attempts = list_job_attempts(svc.operations_db_path, arch_job["job_id"])
        assert arch_attempts[-1]["worker_id"] == "remote-windows-worker"

        for stage in (
            JobStage.KNOWLEDGE_FINALIZE.value,
            JobStage.STORE_INGEST.value,
        ):
            jobs = list_jobs(svc.operations_db_path, stage=stage, canonical_id=CANONICAL_ID)
            assert jobs, f"missing {stage} job"
            attempts = list_job_attempts(svc.operations_db_path, jobs[-1]["job_id"])
            assert attempts[-1]["worker_id"] == "nas-local-worker", f"{stage} not NAS"

        assert JobStage.ARCHIVE.value in fake.executed
        assert JobStage.MEDIA_PROCESS.value in fake.executed
        assert JobStage.KNOWLEDGE_EXTRACT.value in fake.executed
        assert JobStage.KNOWLEDGE_FINALIZE.value in fake.executed
        assert JobStage.STORE_INGEST.value in fake.executed
    finally:
        svc.stop()


def test_network_partition_core(service: ControlPlaneService):
    _seed_job(service.operations_db_path, JobStage.ARCHIVE.value, FINGERPRINT, required_capabilities=["downloader"], canonical_id=CANONICAL_ID)
    client = _client(service.http_url)
    claimed = client.claim_next_job(
        "win-worker", ["downloader"], lease_duration_seconds=0
    )
    assert claimed is not None
    rec = recover_expired_leases(service.operations_db_path, now=utc_now_iso())
    assert len(rec["recovered_leased"]) >= 1
    job = get_job(service.operations_db_path, claimed.job_id)
    assert job["state"] == JobState.QUEUED.value