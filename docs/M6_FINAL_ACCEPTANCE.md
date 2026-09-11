# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Final Acceptance & Seal

**Date**: 2026-09-11  
**Project**: `personal-knowledge-pipeline`  
**Branch**: `feat/m6-automated-knowledge-operations`  
**Milestone Status**: COMPLETE / SEALED  
**Baseline M6-07 Commit**: `e252ac9feedc9f71e61f5318bc6aa3e39e230313`  
**M6-08 Deployment Commit**: `c0954084155b148ac50fcb3b713cc509f6967bfe`  
**Final Acceptance Tag**: `m6-automated-knowledge-operations-complete`  

---

## 1. Executive Summary & Product Claim

Milestone **M6 (Automated Knowledge Operations & NAS/PC Orchestration)** delivers the complete, unattended, production-grade operations layer for the Personal Knowledge Pipeline. It transforms the offline batch libraries developed in Milestones M2 through M5 into an automated, self-healing, distributed pipeline across Synology NAS and a Windows PC workstation.

### Frozen Final Product Claim
The end-to-end user capability guaranteed by M6 is:
$$\text{Operator favorites a new Douyin item} \longrightarrow \text{Unattended DISCOVER} \longrightarrow \text{ARCHIVE} \longrightarrow \text{MEDIA\_PROCESS} \longrightarrow \text{KNOWLEDGE\_EXTRACT} \longrightarrow \text{KNOWLEDGE\_FINALIZE} \longrightarrow \text{STORE\_INGEST} \longrightarrow \mathbf{SEARCHABLE} \longrightarrow \text{Evidence-grounded lexical retrieval}$$

Under normal operations:
- **Synology NAS (Control Plane & Storage)**:
  - Runs the Control Plane daemon (`pkp-control-plane` Docker container)
  - Manages the authoritative Operations SQLite database (`/var/lib/pkp/operations/operations.sqlite3`)
  - Orchestrates downstream transitions via single-writer Scheduler and startup crash recovery
  - Hosts the NAS local worker (`nas-local-worker`) executing `KNOWLEDGE_FINALIZE` and `STORE_INGEST`
  - Hosts the canonical M5 Knowledge Store (`/var/lib/pkp/knowledge/knowledge_store.sqlite3`)
- **Windows PC Workstation (LOCAL-PC, RTX 4090)**:
  - Runs the Windows Worker service (`windows-pc-4090`) via Windows Task Scheduler (`PkpM6WindowsWorker`)
  - Executes `DISCOVER`, `ARCHIVE`, `MEDIA_PROCESS`, and `KNOWLEDGE_EXTRACT`
  - Connects to NAS Control Plane exclusively via authenticated HTTP transport
  - Never opens SQLite databases directly across SMB
- **Shared Storage**:
  - SMB share `\\192.168.1.131\AI_Work\knowledge` stores immutable raw archives and processed evidence artifacts.

---

## 2. Milestone Summary (M6-00 through M6-09)

| Phase | Title | Scope & Deliverables | Status |
|---|---|---|---|
| **M6-00** | Architecture & Orchestration Contract | Stage graph, dual state machines, deterministic IDs, lease protocol, Topology A contract. | `SEALED` |
| **M6-01** | Durable Operations Store & State Machine | SQLite store (`PRAGMA user_version=1`), 7 tables, atomic transactions, idempotency. | `SEALED` |
| **M6-02** | Local Worker Runtime & Lease Protocol | Capability matching, atomic job claiming, renewal daemon, stale-lease fencing. | `SEALED` |
| **M6-03** | Pipeline Stage Adapters | Uniform `StageExecutionResult` contract wrapping M2→M5 with invariant-gated success. | `SEALED` |
| **M6-04** | Scheduler & Automatic Downstream | Single-writer DAG traversal, lifecycle advancement, retry backoff calculation. | `SEALED` |
| **M6-05** | Crash Recovery, Retry & Observability | Startup orphan cleanup, expired lease reclamation, read-only CLI and diagnostics. | `SEALED` |
| **M6-06** | Windows Worker Host & Autostart | PowerShell Task Scheduler installer/uninstaller, single-instance file lock, env validation. | `SEALED` |
| **M6-07** | NAS Control Plane & HTTP Transport | Docker control plane, `HttpWorkerTransport`, Bearer auth, stage placement allowlists. | `SEALED` |
| **M6-08** | Real Douyin Favorite → Searchable E2E | Live production Canary (`douyin_7660044343020916006`), M5 parity, scheduled task cutover. | `SEALED` |
| **M6-09** | Final Acceptance & Milestone Seal | Full system audit, test regression gates, cold backups, frozen documentation and tag. | `SEALED` |

---

## 3. Production Topology & Storage Ownership

```
+-----------------------------------------------------------------------------------+
| SYNOLOGY NAS (192.168.1.131)                                                      |
| Docker Container: pkp-control-plane (Linux, Python 3.12)                         |
| - Control Plane HTTP Daemon: Port 8765                                            |
| - Operations Store: /var/lib/pkp/operations/operations.sqlite3 (LOCAL FS ONLY)    |
| - M5 Knowledge Store: /var/lib/pkp/knowledge/knowledge_store.sqlite3 (LOCAL ONLY) |
| - Local Worker: nas-local-worker (Allowed stages: KNOWLEDGE_FINALIZE, STORE_INGEST)|
| - Shared Storage Root: \\192.168.1.131\AI_Work\knowledge                          |
+-----------------------------------------^-----------------------------------------+
                                          | HTTP API (Bearer Token Auth)
                                          | SMB File Share (Artifact Sync)
+-----------------------------------------v-----------------------------------------+
| WINDOWS PC WORKSTATION (LOCAL-PC, RTX 4090)                                       |
| Task Scheduler: PkpM6WindowsWorker (elevated, user: Sean)                         |
| - Worker Runtime: windows-pc-4090 (Allowed: DISCOVER, ARCHIVE, MEDIA_PROCESS,      |
|                                             KNOWLEDGE_EXTRACT)                    |
| - Single Instance Lock: C:\PKP\windows-worker.lock (Exclusively Held)             |
| - Direct SQLite Access: NONE (0 Operations or M5 SQLite DBs opened)               |
| - Execution Engines: Faster-Whisper (CUDA float16), LM Studio Qwen3-8B (port 12345)|
| - Downloader Isolation: .venv-f2 virtual environment                              |
+-----------------------------------------------------------------------------------+
```

### Storage Ownership Rules:
1. **Operations SQLite DB**: Owned exclusively by NAS Control Plane process on local ext4/btrfs filesystem. No remote SQLite connection permitted over SMB.
2. **Knowledge Store SQLite DB**: Owned exclusively by NAS Control Plane process on local filesystem. Ingested solely by `nas-local-worker`.
3. **Artifact Storage**: Shared SMB filesystem. Windows worker writes archive/processed artifacts locally and syncs them atomically to NAS before job completion.

---

## 4. State Machine & Stage Placement Contract

### 4.1 Asset Lifecycle State Machine
$$\text{DISCOVERED} \longrightarrow \text{ARCHIVED} \longrightarrow \text{EVIDENCE\_READY} \longrightarrow \text{KNOWLEDGE\_READY} \longrightarrow \mathbf{SEARCHABLE}$$

### 4.2 Job State Machine
$$\text{QUEUED} \longrightarrow \text{LEASED} \longrightarrow \text{RUNNING} \longrightarrow \begin{cases} \mathbf{SUCCEEDED} \\ \text{FAILED\_RETRYABLE} \ (\text{with backoff } \rightarrow \text{QUEUED}) \\ \mathbf{FAILED\_TERMINAL} \ (\text{exhausted or unrecoverable}) \\ \mathbf{CANCELLED} \ (\text{admin action}) \end{cases}$$

### 4.3 Stage Placement Allowlists
Enforced by server-authoritative claim filter on NAS:
- **Windows Worker (`windows-pc-4090`)**:
  - `DISCOVER` (requires Chrome browser profile, Windows desktop environment)
  - `ARCHIVE` (requires network, downloader, cookie provider)
  - `MEDIA_PROCESS` (requires GPU RTX 4090, Faster-Whisper large-v3)
  - `KNOWLEDGE_EXTRACT` (requires LM Studio local LLM runtime)
- **NAS Local Worker (`nas-local-worker`)**:
  - `KNOWLEDGE_FINALIZE` (deterministic CPU merge, enrichment, and rendering)
  - `STORE_INGEST` (direct local SQLite write to M5 Knowledge Store)

---

## 5. Production Canary E2E Verification

The live canary Douyin asset was verified end-to-end in production without mocking:
- **Canonical ID**: `douyin_7660044343020916006`
- **Platform Content ID**: `7660044343020916006`
- **Video Title**: `#ai #skills` (Author: `Ai算法工程师Future`)
- **Pipeline Run ID**: `run_edf135d277b0c27d`
- **Final Lifecycle**: `SEARCHABLE`
- **Final Run Status**: `SUCCEEDED`

### Stage Trace:
1. `DISCOVER` (`job_a7f3a8aeb655d735`, attempt 1): Discovered live favorite item.
2. `ARCHIVE` (`job_a51e0fe4d5845683`, attempt 4): Downloaded MP4 via `SafeDouyinDownloader` + `_ProfileCookieProvider`.
3. `MEDIA_PROCESS` (`job_2b6b477cad490aa1`, attempt 2): Faster-Whisper ASR on RTX 4090; artifacts synced to NAS SMB.
4. `KNOWLEDGE_EXTRACT` (`job_af622bbc1c0960eb`, attempt 1): Qwen3-8B extraction via LM Studio port 12345.
5. `KNOWLEDGE_FINALIZE` (`job_2542f11282175087`, attempt 1): Rendered 94 canonical units on NAS.
6. `STORE_INGEST` (`job_30414774405002be`, attempt 1): Ingested into NAS Knowledge Store.

### Real Lexical Retrieval Proof:
Querying `"skills"` in NAS container returns 10 hits from Canary asset:
- Top 1: `ku_ac7d6bc478e6f487` (`score: -1.2405`): `"nature skills"`
- Top 2: `ku_7c63cde3430f402d` (`score: -1.1892`): `"UP主整理了10个热门的科研skills。"`
- Top 3: `ku_36bca29463195480` (`score: -1.1540`): `"scientific agent skills"`

---

## 6. M5 Historical Parity & M4 Incident Contract

### 6.1 Historical Knowledge Units
- **Historical Asset 1 (`douyin_7681603850364521734`)**: 62 KUs (100% preserved)
- **Historical Asset 2 (`douyin_7682038498466993905`)**: 6 KUs (100% preserved)
- **Historical Parity**: **68 / 68 (100%)**
- **Query `"主机"` Proof**: Returns top hit `ku_f3c1570309a90cb6` from `douyin_7681603850364521734`.

### 6.2 M4 Incident Status & Explicit Contract
The incident classification is permanently retained as:
$$\mathbf{RECOVERED\_WITH\_INTERMEDIATE\_PROVENANCE\_LOSS}$$
- Final canonical knowledge units survived intact.
- M3 Evidence manifest and chunks survived intact.
- M5 SQLite store projection survived intact.
- Original M4 C10 intermediate extraction bytes were permanently lost during prior incident.
- Fresh reruns of extraction on C10 assets are forensic-only and not canonical.
- Historical source-enriched anchors remain preserved.
- The system is explicitly NOT classified as `FULLY_RECOVERED`.

---

## 7. Security & Secret Audit

Strict boundary auditing confirmed zero credential leakage across all layers:
- **Log Audit**: `C:\PKP\logs\windows-worker.log` and NAS Docker logs contain:
  - `sessionid`: 0 occurrences
  - `passport_csrf_token`: 0 occurrences
  - `lease_token`: 0 occurrences
  - `Authorization: Bearer`: 0 occurrences
- **Browser Profile**: Chrome profile data remains strictly on Windows local disk; cookies are extracted in-memory and never written to disk, git, or SQLite.
- **Observability Projections**: `src.operations.cli` and `/api/v1/status` strip all execution tokens prior to serialization.

---

## 8. Post-Deployment Database Backups

Cold-consistent SQLite backups were produced using `VACUUM INTO` on the NAS:

| Database | Backup File Path | File Size | SHA-256 Checksum | Schema Version | Tables |
|---|---|---|---|---|---|
| **Operations DB** | `/var/lib/pkp/operations/backups/operations_m6_08.sqlite3` | 19,890,176 bytes | `1077325af6bd45a8cde99053551f7784118bc31405f9ce6ff0db8b65feb198ea` | 1 | 10 |
| **Knowledge Store** | `/var/lib/pkp/knowledge/backups/knowledge_store_m6_08.sqlite3` | 2,691,072 bytes | `75af12a7494d72640dd071d37d1e09a42c14a7e448c8fe29f1ee454524925f98` | 1 | 12 |

---

## 9. Test Verification Gates

All automated test gates passed with 0 failures:
- **Targeted Operations Test Suite**: **432 passed**
- **M4 Incident Recovery Suite**: **54 passed**
- **Full Test Regression (`pytest tests -q`)**: **1726 passed, 4 skipped, 2 warnings, 0 failed**

---

## 10. Known Operational Limitations

1. **Douyin Interactive Challenge**:
   - Normal operations are unattended.
   - However, Douyin anti-scraping risk control may occasionally present `INTERACTIVE_CHALLENGE` (CAPTCHA/SMS/slide verification).
   - This requires the operator to log in once via the dedicated Chrome profile window. Once session cookies are renewed, unattended operation automatically resumes.
   - 100% permanently unattended operation is NOT claimed.
2. **Cold-Start Backlog Consumption**:
   - Initial sync of a rich Douyin account discovers all existing favorites (e.g. 82 items in M6-08).
   - The worker processes items sequentially according to priority and capability constraints. Backlog presence is normal and expected.
3. **Worker Draining Mechanism**:
   - Setting `allowed_stages: []` directly in the database is not persistent across worker re-registration.
   - Future improvement: implement an explicit `PAUSE/DRAIN` administrative state.

---

## 11. Deferred Work (Not in M6)

The following features were intentionally excluded from Milestone M6 scope:
- External truth verification and fact checking
- Dense vector embedding generation and hybrid retrieval
- Cross-encoder reranker
- RAG answer generation and synthesis
- Obsidian publishing / export workflows
- Web end-user UI
- Global entity relationship graph
- Advanced interactive worker drain UX

---

## 12. Conclusion & Handoff to Milestone M7

Milestone **M6 is COMPLETE, SEALED, AND VERIFIED**.
The automated knowledge operations architecture is fully operational in production.

**Proposed Next Milestone**:
$$\mathbf{Milestone\ M7 \cdot Knowledge\ Verification}$$
