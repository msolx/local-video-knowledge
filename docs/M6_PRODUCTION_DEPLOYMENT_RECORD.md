# Milestone M6 · Phase M6-08 Production Deployment Record

**Date**: 2026-09-11  
**Project**: `personal-knowledge-pipeline`  
**Branch**: `feat/m6-automated-knowledge-operations`  
**Baseline M6-07 Commit**: `e252ac9feedc9f71e61f5318bc6aa3e39e230313`  
**Collector Fix Commit**: `7297fcd`  
**Status**: COMPLETE / FROZEN / VERIFIED  

---

## 1. Executive Summary & Topology

Milestone **M6-08 Production Deployment E2E** is successfully completed and sealed. The automated knowledge operations architecture (Topology A: Synology NAS Control Plane + Windows PC GPU Worker) has been deployed in production, verified with a live canary Douyin asset, and handed over to the automated Windows Task Scheduler service.

### 1.1 Deployment Topology

```
+-----------------------------------------------------------------------------------+
| SYNOLOGY NAS (192.168.1.131)                                                      |
| Docker Container: pkp-control-plane                                               |
| - Control Plane Service: HTTP API on port 8765                                    |
| - Operations Store: /var/lib/pkp/operations/operations.sqlite3                    |
| - M5 Knowledge Store: /var/lib/pkp/knowledge/knowledge_store.sqlite3              |
| - Local Worker (nas-local-worker): KNOWLEDGE_FINALIZE, STORE_INGEST               |
| - Shared Storage Root: \\192.168.1.131\AI_Work\knowledge                          |
+-----------------------------------------^-----------------------------------------+
                                          | HTTP (port 8765)
                                          | SMB File Sync
+-----------------------------------------v-----------------------------------------+
| WINDOWS PC WORKSTATION (LOCAL-PC, RTX 4090)                                       |
| Task Scheduler: PkpM6WindowsWorker (user: Sean, Elevated)                         |
| - Worker Runtime: windows-pc-4090 (SingleInstanceLock at C:\PKP\windows-worker.lock)|
| - Transport: HttpWorkerTransport (Bearer Auth via PKP_CONTROL_PLANE_TOKEN)        |
| - Allowed Stages: DISCOVER, ARCHIVE, MEDIA_PROCESS, KNOWLEDGE_EXTRACT             |
| - Runtimes: Faster-Whisper (CUDA float16), LM Studio Qwen3-8B (port 12345)        |
| - Isolated Downloader Env: .venv-f2 (f2 CLI isolated from main virtualenv)        |
+-----------------------------------------------------------------------------------+
```

---

## 2. Canary Pipeline Lifecycle Verification

The real live canary asset (`douyin_7660044343020916006`) successfully traversed all 6 pipeline stages from favorite detection to searchable knowledge:

$$\text{DISCOVER} \longrightarrow \text{ARCHIVE} \longrightarrow \text{MEDIA\_PROCESS} \longrightarrow \text{KNOWLEDGE\_EXTRACT} \longrightarrow \text{KNOWLEDGE\_FINALIZE} \longrightarrow \text{STORE\_INGEST} \longrightarrow \mathbf{SEARCHABLE}$$

### 2.1 Canary Asset Metadata
- **Platform**: `douyin`
- **Platform Content ID**: `7660044343020916006`
- **Canonical ID**: `douyin_7660044343020916006`
- **Author**: `Ai算法工程师Future`
- **Title**: `#ai #skills`
- **Pipeline Run ID**: `run_edf135d277b0c27d`
- **Final Lifecycle State**: `SEARCHABLE`
- **Pipeline Run Status**: `SUCCEEDED` (duration: ~28 minutes across all stages)

### 2.2 Stage Execution Details

| Stage | Worker Node | Job ID | Attempt | Status | Output Details |
|---|---|---|---|---|---|
| **DISCOVER** | Windows PC | `job_a7f3a8aeb655d735` | 1/5 | `SUCCEEDED` | Discovered 82 Douyin items from favorites |
| **ARCHIVE** | Windows PC | `job_a51e0fe4d5845683` | 4/5 | `SUCCEEDED` | Downloaded via `SafeDouyinDownloader` + `_ProfileCookieProvider`, promoted to `archive/` |
| **MEDIA_PROCESS** | Windows PC | `job_2b6b477cad490aa1` | 2/5 | `SUCCEEDED` | Faster-Whisper ASR (large-v3, float16) + chunking; artifacts synced to shared `processed/` |
| **KNOWLEDGE_EXTRACT** | Windows PC | `job_af622bbc1c0960eb` | 1/5 | `SUCCEEDED` | LM Studio (port 12345, Qwen3-8B); candidate extraction & enrichment |
| **KNOWLEDGE_FINALIZE** | NAS Local | `job_2542f11282175087` | 1/5 | `SUCCEEDED` | 94 validated `knowledge-units-v1` rendered to `knowledge_units.json` |
| **STORE_INGEST** | NAS Local | `job_30414774405002be` | 1/5 | `SUCCEEDED` | 94 KUs ingested into M5 SQLite store; asset transitioned to `SEARCHABLE` |

---

## 3. M5 Knowledge Store Parity & Retrieval Verification

### 3.1 Historical KU Parity
The production Knowledge Store on Synology NAS (`/var/lib/pkp/knowledge/knowledge_store.sqlite3`) was verified for strict historical preservation:
- **Historical Asset 1**: `douyin_7681603850364521734` (62 KUs) — 100% preserved
- **Historical Asset 2**: `douyin_7682038498466993905` (6 KUs) — 100% preserved
- **Canary Asset**: `douyin_7660044343020916006` (94 KUs) — successfully added
- **Total KUs**: **162** ($68 + 94 = 162$) at canary acceptance
- **Historical Parity**: **68/68 preserved** (0 deleted, 0 mutated, 0 corrupted)

### 3.2 Live Lexical Search Verification
Executed live inside `pkp-control-plane` using `src.knowledge.retrieval`:
1. **Query `"skills"` (Canary verification)**:
   - Returns 10 hits from `douyin_7660044343020916006`
   - Top Hit: `ku_ac7d6bc478e6f487` ("nature skills")
   - Second Hit: `ku_7c63cde3430f402d` ("UP主整理了10个热门的科研skills。")
2. **Query `"主机"` (Historical C10 verification)**:
   - Returns 5 hits from `douyin_7681603850364521734`
   - Top Hit: `ku_f3c1570309a90cb6` ("说话者在养病期间闲得没事，于是整了一台Strax Halo的V1主机来玩。")

---

## 4. Production Fixes & Hardening

During M6-08 deployment, four real-world production defects were identified and comprehensively resolved:

1. **Douyin Detail API Crawler Protection (`_ProfileCookieProvider`)**:
   - *Problem*: Downloader encountered Douyin anti-crawler verification when fetching video detail.
   - *Fix*: Implemented `_ProfileCookieProvider` in `src/operations/windows_worker.py` to securely read decrypting cookies from local Chrome profile in-memory.
   - *Security*: Cookies are zeroed after use, never persisted to repository, SQLite DBs, or log files.

2. **Cross-Node Artifact Synchronization (`_sync_processed_artifacts`)**:
   - *Problem*: `MediaProcessAdapter` wrote evidence artifacts to local PC disk (`config.data_root / "processed" / canonical_id`), but NAS worker required artifacts at shared SMB mount (`\\192.168.1.131\AI_Work\knowledge\processed`).
   - *Fix*: In `src/operations/stages.py`, added atomic file copy from local output to shared processed root before stage completion.

3. **Task Scheduler XML Schema Limitation (`RestartCount = 999`)**:
   - *Problem*: `install_m6_worker_task.ps1` used `RestartCount = 999999`, which exceeded Windows Task Scheduler schema maximum (`999`), causing task registration failure.
   - *Fix*: Corrected to `RestartCount = 999` in `install_m6_worker_task.ps1` and runbooks.
   - *Context Guard*: Added `Push-Location $RepoRoot` / `Pop-Location` around preflight config validation so `src` module is reliably resolvable from any working directory.

4. **Main Virtualenv Isolation from `f2` CLI (`.venv-f2`)**:
   - *Problem*: Installing `f2` in main virtual environment broke M2 architectural isolation tests (`test_29_main_env_no_f2_dependency`).
   - *Fix*: Maintained separate isolated virtualenv `.venv-f2`. `build_production_handler_registry` appends `.venv-f2` site-packages to `sys.path` only within worker execution scope. Main `.venv` remains free of `f2`.

---

## 5. Production Cutover & Windows Task Scheduler Verification

The Windows PC worker was successfully cut over from manual foreground debugging to automated Windows Task Scheduler:

- **Task Name**: `PkpM6WindowsWorker`
- **State**: `Ready` / `Running`
- **Identity**: Single logical worker instance `windows-pc-4090`
- **Single Instance Enforcement**: `C:\PKP\windows-worker.lock` exclusively locked
- **DB Isolation**: Verified **0** local SQLite databases opened by worker process
- **Heartbeat & Leases**: Continuous 10s heartbeat to NAS control plane; 0 expired leases; 0 orphaned claims

---

## 6. Control Plane Restart & Fault Tolerance Verification

The NAS control plane container was restarted to verify cluster resilience:
- **Command**: `docker restart pkp-control-plane`
- **Liveness Endpoint (`/health/live`)**: HTTP 200 `{"status": "ok"}`
- **Readiness Endpoint (`/health/ready`)**: HTTP 200 `{"status": "ready", "api_version": "m6-control-plane-api-v1", "operations_store_valid": true, "scheduler_initialized": true, "local_worker_initialized": true, "errors": []}`
- **Re-registration**: `nas-local-worker` initialized and active immediately
- **Worker Reconnect**: `windows-pc-4090` automatically reconnected via HTTP retry/backoff, refreshed heartbeat, and continued pipeline execution without data loss or duplicate execution

---

## 7. Secret & Observability Audit

Audit of all production components confirmed strict adherence to the secrets boundary:
- **Windows Worker Logs (`C:\PKP\logs\windows-worker.log`)**:
  - `sessionid`: 0 occurrences
  - `passport_csrf_token`: 0 occurrences
  - `lease_token`: 0 occurrences
  - `Authorization: Bearer`: 0 occurrences
  - Heartbeat spam: None (only state changes and cycle summaries logged)
- **NAS Docker Logs (`docker logs pkp-control-plane`)**:
  - Zero token or credential leaks
  - Clean request handling
- **Observability API & CLI**:
  - `python -m src.operations.cli workers`: Displays clean worker status table without tokens
  - `python -m src.operations.cli asset <id>`: Strips execution secrets before output
  - `python -m src.operations.cli timeline <id>`: Displays transition events with masked payloads

---

## 8. Post-Deployment Database Backups

Post-deployment snapshots of all production databases were taken inside the NAS control plane container using SQLite `VACUUM INTO`:

### 8.1 Operations Database
- **Source**: `/var/lib/pkp/operations/operations.sqlite3`
- **Backup File**: `/var/lib/pkp/operations/backups/operations_m6_08.sqlite3`
- **File Size**: 19,890,176 bytes (~19.0 MB)
- **SHA-256 Checksum**: `1077325af6bd45a8cde99053551f7784118bc31405f9ce6ff0db8b65feb198ea`
- **Schema Version (`PRAGMA user_version`)**: `1`
- **Table Count**: 10 tables

### 8.2 Knowledge Store Database
- **Source**: `/var/lib/pkp/knowledge/knowledge_store.sqlite3`
- **Backup File**: `/var/lib/pkp/knowledge/backups/knowledge_store_m6_08.sqlite3`
- **File Size**: 2,691,072 bytes (~2.57 MB)
- **SHA-256 Checksum**: `75af12a7494d72640dd071d37d1e09a42c14a7e448c8fe29f1ee454524925f98`
- **Schema Version (`PRAGMA user_version`)**: `1`
- **Table Count**: 12 tables

---

## 9. Test Suite Verification

Full test gates passed with zero regressions:
- **Operations Suite (`pytest tests/test_operations*.py`)**: **432 passed**
- **M4 Incident Recovery Suite**: **54 passed**
- **Full Test Regression (`pytest tests -q`)**: **1726 passed, 4 skipped, 2 warnings, 0 failed**

---

## 10. Conclusion & Handoff to M6-09

Milestone M6-08 is **COMPLETE, FROZEN, AND VERIFIED**.
The live Canary asset has been indexed and made searchable with zero data loss to historical assets.
The Windows PC worker is running autonomously under Task Scheduler, and the NAS Control Plane is healthy and resilient.
The project is now ready for **M6-09 Final Acceptance**.
