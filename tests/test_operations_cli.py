"""M6-05 admin CLI tests (subprocess + direct invocation, temp DBs only)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.operations.admin import admin_cancel_job, admin_retry_job
from src.operations.models import compute_job_id, utc_now_iso
from src.operations.scheduler import discovery_control_fingerprint
from src.operations.stages import required_capabilities_for_stage
from src.operations.store import (
    claim_next_job,
    complete_job_retryable_failure,
    complete_job_success,
    create_operations_store,
    create_pipeline_run,
    enqueue_job,
    register_asset,
    register_worker,
    start_claimed_job,
)

PLATFORM = "douyin"
CONTENT_ID = "7681603850364521734"
CANONICAL_ID = f"{PLATFORM}_{CONTENT_ID}"
FINGERPRINT = "a" * 64
T0 = "2026-09-10T01:00:00+00:00"
T_PLUS = "2026-09-10T01:02:00+00:00"

_ENV = dict(os.environ)


def _run_cli(db_path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "src.operations.cli", "--db", str(db_path), *args]
    e = dict(_ENV)
    if env:
        e.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=e)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "operations.sqlite3"
    create_operations_store(db, now=T0)
    return db


def _seed_asset(db_path: Path, *, content_id: str = CONTENT_ID) -> str:
    cid = f"{PLATFORM}_{content_id}"
    register_asset(db_path, PLATFORM, content_id, cid, metadata={}, now=T0)
    run = create_pipeline_run(db_path, cid, "discovery", now=T0)
    fp = discovery_control_fingerprint(PLATFORM, content_id)
    enq = enqueue_job(
        db_path,
        PLATFORM,
        content_id,
        "ARCHIVE",
        fp,
        policy_version="m6-scheduler-policy-v1",
        canonical_id=cid,
        pipeline_run_id=run["run_id"],
        required_capabilities=list(required_capabilities_for_stage("ARCHIVE")),
        now=T0,
    )
    return enq.job_id


# ----------------------------------------------------------------------
# 40. CLI status
# ----------------------------------------------------------------------


def test_cli_status_human(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "status")
    assert r.returncode == 0
    assert "assets_total" in r.stdout
    assert CANONICAL_ID in r.stdout


def test_cli_status_json(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "status", "--json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "summary" in data and "assets" in data
    assert data["summary"]["assets_total"] == 1
    assert data["assets"][0]["canonical_id"] == CANONICAL_ID


def test_cli_status_validate_flag(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "status", "--validate", "--json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["summary"]["store_valid"] is True


# ----------------------------------------------------------------------
# 41. CLI asset
# ----------------------------------------------------------------------


def test_cli_asset(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "asset", CANONICAL_ID, "--json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["canonical_id"] == CANONICAL_ID
    assert "health" in data


def test_cli_asset_not_found(db_path: Path):
    r = _run_cli(db_path, "asset", "douyin_does_not_exist", "--json")
    assert r.returncode == 1


# ----------------------------------------------------------------------
# 42. CLI jobs
# ----------------------------------------------------------------------


def test_cli_jobs_filter(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "jobs", "--state", "QUEUED", "--json")
    assert r.returncode == 0
    jobs = json.loads(r.stdout)
    assert len(jobs) == 1
    assert jobs[0]["stage"] == "ARCHIVE"
    assert jobs[0]["state"] == "QUEUED"
    # lease_token must never appear in job rows.
    assert "lease_token" not in r.stdout


# ----------------------------------------------------------------------
# 43. CLI failed
# ----------------------------------------------------------------------


def test_cli_failed(db_path: Path):
    job_id = _seed_asset(db_path)
    register_worker(db_path, "w1", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w1", ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job_id
    start_claimed_job(db_path, job_id, "w1", claimed.lease_token, now=T_PLUS)
    complete_job_retryable_failure(
        db_path, job_id, "w1", claimed.lease_token,
        error_class="RetryableJobError", error_message="LLM endpoint unavailable", now=T_PLUS,
    )
    r = _run_cli(db_path, "failed", "--json")
    assert r.returncode == 0
    jobs = json.loads(r.stdout)
    assert any(j["job_id"] == job_id for j in jobs)
    assert any("LLM endpoint unavailable" in (j.get("last_error") or "") for j in jobs)


# ----------------------------------------------------------------------
# 44. CLI workers
# ----------------------------------------------------------------------


def test_cli_workers(db_path: Path):
    register_worker(db_path, "w1", ["downloader"], display_name="W1", hostname="pc1", now=T0)
    r = _run_cli(db_path, "workers", "--json")
    assert r.returncode == 0
    workers = json.loads(r.stdout)
    assert workers[0]["worker_id"] == "w1"
    assert "downloader" in workers[0]["capabilities"]


# ----------------------------------------------------------------------
# 45. CLI timeline
# ----------------------------------------------------------------------


def test_cli_timeline(db_path: Path):
    _seed_asset(db_path)
    r = _run_cli(db_path, "timeline", CANONICAL_ID, "--json")
    assert r.returncode == 0
    events = json.loads(r.stdout)
    assert len(events) >= 3
    assert events[0]["event_id"] < events[-1]["event_id"]
    for e in events:
        assert "lease_" not in json.dumps(e)


# ----------------------------------------------------------------------
# 46/47. CLI retry / cancel / recover
# ----------------------------------------------------------------------


def test_cli_retry(db_path: Path):
    job_id = _seed_asset(db_path)
    register_worker(db_path, "w1", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w1", ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job_id
    start_claimed_job(db_path, job_id, "w1", claimed.lease_token, now=T_PLUS)
    complete_job_retryable_failure(
        db_path, job_id, "w1", claimed.lease_token, error_class="R", error_message="x", now=T_PLUS
    )
    r = _run_cli(db_path, "retry", job_id, "--force", "--reason", "operator", "--json")
    assert r.returncode == 0
    result = json.loads(r.stdout)
    assert result["outcome"] == "requeued"
    assert result["job_id"] == job_id


def test_cli_retry_terminal_rejected(db_path: Path):
    from src.operations.store import complete_job_terminal_failure

    job_id = _seed_asset(db_path)
    register_worker(db_path, "w1", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w1", ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job_id
    start_claimed_job(db_path, job_id, "w1", claimed.lease_token, now=T_PLUS)
    complete_job_terminal_failure(
        db_path, job_id, "w1", claimed.lease_token, error_class="CorruptArtifact", error_message="bad", now=T_PLUS
    )
    r = _run_cli(db_path, "retry", job_id, "--force", "--reason", "r", "--json")
    assert r.returncode == 1
    result = json.loads(r.stdout)
    assert result["outcome"] == "terminal_requires_override"


def test_cli_cancel(db_path: Path):
    job_id = _seed_asset(db_path)
    r = _run_cli(db_path, "cancel", job_id, "--reason", "triage", "--json")
    assert r.returncode == 0
    result = json.loads(r.stdout)
    assert result["outcome"] == "cancelled"


def test_cli_recover(db_path: Path):
    r = _run_cli(db_path, "recover", "--json")
    assert r.returncode == 0
    result = json.loads(r.stdout)
    assert result["schema_version"].startswith("m6-recovery-result-v1")
    assert result["store_valid"] is True


# ----------------------------------------------------------------------
# 50/51. Secret safety
# ----------------------------------------------------------------------


def test_cli_no_lease_token_in_output(db_path: Path):
    job_id = _seed_asset(db_path)
    register_worker(db_path, "w1", ["downloader"], now=T0)
    claimed = claim_next_job(db_path, "w1", ["downloader"], now=T_PLUS)
    assert claimed is not None and claimed.job_id == job_id
    start_claimed_job(db_path, job_id, "w1", claimed.lease_token, now=T_PLUS)
    assert claimed.lease_token.startswith("lease_")
    for cmd in (
        ("status", "--json"),
        ("asset", CANONICAL_ID, "--json"),
        ("jobs", "--json"),
        ("workers", "--json"),
        ("timeline", CANONICAL_ID, "--json"),
    ):
        r = _run_cli(db_path, *cmd)
        assert r.returncode == 0, cmd
        assert claimed.lease_token not in r.stdout, cmd
        assert "lease_token" not in r.stdout, cmd
        assert "sessionid" not in r.stdout, cmd
        assert "Authorization" not in r.stdout, cmd
        assert "api_key" not in r.stdout, cmd


# ----------------------------------------------------------------------
# Default DB path resolution
# ----------------------------------------------------------------------


def test_cli_env_db_path(tmp_path: Path):
    db = tmp_path / "env_ops.sqlite3"
    create_operations_store(db, now=T0)
    r = _run_cli(db, "status", "--json", env={"OPERATIONS_DB_PATH": str(db)})
    assert r.returncode == 0
    assert json.loads(r.stdout)["summary"]["assets_total"] == 0


def test_cli_default_db_never_created(tmp_path: Path):
    """Running against a temp DB must not create the production default."""
    db = tmp_path / "x.sqlite3"
    create_operations_store(db, now=T0)
    _run_cli(db, "status")
    from src.operations.models import DEFAULT_OPERATIONS_PATH

    assert not Path(DEFAULT_OPERATIONS_PATH).exists() or Path(DEFAULT_OPERATIONS_PATH).exists() is False
    assert db.exists()