# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Task Board

> **Status**: M6-00 DONE; M6-01 DONE; M6-02 DONE; M6-03..M6-09 TODO.

---

## Status Matrix

| Milestone | Status | Deliverable |
|---|---|---|
| M6-00 | DONE | `docs/M6_OPERATIONS_ARCHITECTURE.md`, `docs/M6_DECISIONS.md`, `docs/M6_TASKS.md`, `docs/M6_HANDOFF.md` |
| M6-01 | DONE | `src/operations/` durable store + job state machine, `tests/test_operations_store.py` |
| M6-02 | DONE | Local Worker Runtime + Capability/Lease Protocol |
| M6-03 | TODO | Pipeline Stage Adapters for M2→M5 |
| M6-04 | TODO | Scheduler + Automatic Downstream Orchestration |
| M6-05 | TODO | Crash Recovery / Retry / Observability |
| M6-06 | TODO | Windows PC Worker Autostart |
| M6-07 | TODO | NAS Docker Control Plane Deployment |
| M6-08 | TODO | Real Douyin Favorite → Searchable Knowledge E2E |
| M6-09 | TODO | Final Acceptance |

---

## M6-00: Operations Architecture & Orchestration Contract Design (`DONE`)

### Objective
Design — and only design — the M6 operations architecture and orchestration contract. Freeze the stage graph, asset/job state machines, identity/idempotency rules, lease/retry semantics, deployment topology, worker capability model, storage ownership, security boundary, observability, admin contract, and task tree.

### Constraints honored
- Docs-only: no change to `src/`, `tests/`, `scripts/`, `config/`.
- No Docker deployment, no operations DB creation, no service registration, no NAS changes, no runtime/model start.
- No M2–M5 sealed production logic modified.

### Delivered
- `docs/M6_OPERATIONS_ARCHITECTURE.md` — the full architecture and contract (product goal, real M2→M5 entry-point audit, portability audit, Topology A, stage graph with input/output/success/failure/idempotency/retry per stage, asset vs job lifecycles, identity & idempotency rule, operations DB schema sketch, lease model, retry policy, PC-offline semantics, capabilities, LLM runtime policy, GPU single-slot rule, storage ownership, logical↔physical paths, file transfer contract, stage completion invariants, M5 incremental ingest, trigger/polling model, uncollection semantics, observability, admin contract, autostart, secrets, network failure, recovery test plan, task tree, deferred features).
- `docs/M6_DECISIONS.md` — 20 decisions (1–20) covering orchestration-only scope, separate operations DB, SQLite, dual state machines, frozen asset identity, stage idempotency, Topology A, capability workers, lease+heartbeat, retry classes, PC-offline, GPU single-slot, artifact-invariant success, incremental M5 ingest, uncollection≠deletion, network resilience, secrets boundary, docs-only M6-00, logical paths, shared-filesystem transfer v1.
- `docs/M6_TASKS.md` — this task board.
- `docs/M6_HANDOFF.md` — handoff to M6-01.

---

## M6-01: Durable Operations Store + Job State Machine (`DONE`)

### Objective
Create `data/operations/operations.sqlite3` (logical path `ops.db_path`) with the designed tables (`assets`, `pipeline_runs`, `jobs`, `job_attempts`, `workers`, `leases`/`heartbeats`, `event_log`) and the frozen asset/job state machines.

### Scope hints (from M6-00 contract)
- Schema versioning via `PRAGMA user_version` (mirror M5 store pattern).
- Deterministic `job_id = sha256(platform|platform_content_id|stage|input_fingerprint|policy_version)[:16]`.
- Durable queue semantics; single-writer scheduler assumption.
- Crash recovery: leases survive restart; jobs requeue on expiry.
- No change to M5 `knowledge_store.sqlite3`.

### Delivered
- `src/operations/models.py` — `operations-store-v1` frozen contract: `AssetLifecycleState` (`DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE`), `JobState` (`QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL` + additive `CANCELLED` per Decision 21), `JobStage` (`DISCOVER | ARCHIVE | MEDIA_PROCESS | EVIDENCE_READY | KNOWLEDGE_EXTRACT | KNOWLEDGE_FINALIZE | STORE_INGEST`), `PipelineRunStatus`, `TriggerType`, frozen legal-transition tables, deterministic `compute_job_id` (`job_<sha256(platform|platform_content_id|stage|input_fingerprint|policy_version)[:16]>`, excludes time/attempt/worker/lease/run), `normalize_input_fingerprint` (must be a stable 64-hex SHA-256; timestamp substitution rejected), UTC-ISO timestamp helpers, Python-side backoff (`next_retry_at`, never computed in DB triggers).
- `src/operations/store.py` — the durable store: `PRAGMA user_version=1`, `operations_meta`, 7 tables (`assets`, `pipeline_runs`, `jobs`, `job_attempts`, `workers`, `event_log`), `foreign_keys=ON`, `busy_timeout`, `BEGIN IMMEDIATE` transactions. Enqueue idempotency (SUCCEEDED→SKIP existing-success; QUEUED/LEASED/RUNNING→exists-active; FAILED_RETRYABLE→retry path, no duplicate; FAILED_TERMINAL/CANCELLED→not silently resurrected; changed `input_fingerprint`/`policy_version` → new deterministic job generation with old history preserved). Central `transition_job_state`/`transition_asset_lifecycle` validation (no ad-hoc `UPDATE ... SET state` bypass), `cancel_job` (QUEUED/FAILED_RETRYABLE→CANCELLED; LEASED/RUNNING/terminal→deterministic no-op), `requeue_retryable_job` (terminal/exhausted → explicit reject), `set/clear_job_lease` (persistence only), `begin_attempt`/`finish_attempt` (monotonic `attempt_number` from 1, atomic attempt+state+event; exhaustion → `FAILED_TERMINAL`), `register_worker`/`worker_heartbeat`, append-only `event_log`, and the full read API (`get_asset`, `get_asset_by_canonical_id`, `get_job`, `list_jobs`, `list_pending_jobs`, `list_failed_jobs`, `list_job_attempts`, `list_events`, `get_pipeline_run`, `create/complete_pipeline_run`). No hard-delete API; no secret payloads; no production DB created (tests use temp paths).
- `src/operations/__init__.py` — full public export surface (constants, enums, errors `OperationsError`/`OperationsSchemaError`/`OperationsStateError`/`OperationsIntegrityError`, all store functions, `compute_job_id`, `validate_operations_store`, `EnqueueResult`, `OperationsValidationResult`).
- `tests/test_operations_store.py` — 76 tests covering: init/version/tables/incompatible-version, asset registration/duplicates/lifecycle legal+illegal+admin override, deterministic job ID, enqueue idempotency (incl. SUCCEEDED SKIP), changed-input new generation, pipeline runs + job↔run relation, legal/illegal job transitions, terminal immutability, CANCELLED (queued-cancel, cancel-twice, succeeded-cancel rejection, cancelled-cannot-requeue), retryable failure + requeue + backoff `next_retry_at`, retry exhaustion → `FAILED_TERMINAL`, attempt numbering/persistence, event log append-only + enqueue/transition/attempt events + stable ordering, worker persistence/heartbeat/capabilities JSON, lease field persist/clear/terminal-rejection, transaction rollback (job+event atomicity), FK integrity, `validate_operations_store` clean + corrupted detection, list pending/failed, Unicode metadata, SQL-injection safety, UTC timestamps, and the full synthetic flow (DISCOVER succeed → ARCHIVE fail/requeue/succeed → asset `ARCHIVED`, attempts/events consistent), duplicate discovery (1 asset/1 logical job), changed-input generations.
- Real verification: targeted `pytest tests/test_operations_store.py` = 76 passed. No production `data/operations/operations.sqlite3` created (spec: none this milestone). M2–M5 code untouched.

---

## M6-02: Local Worker Runtime + Capability/Lease Protocol (`DONE`)

### Objective
Worker process that heartbeats capability set, claims jobs via leases, refreshes lease, releases/requeues on completion/crash.

### Scope hints
- Capabilities: `collector`, `downloader`, `cpu_media`, `gpu_asr`, `gpu_vlm`, `llm_extraction`, `store_ingest`.
- PC offline = worker simply stops heartbeating; GPU jobs wait (never FAILED).
- GPU single-heavy-job rule enforced here (capability semaphore).
- Machine name is metadata.

### Delivered
- `src/operations/models.py` extensions: `VALID_CAPABILITIES` (frozen 7-capability vocabulary), `normalize_capability`/`normalize_capabilities` (exact, order-preserving dedup), `new_lease_token` (`lease_<secrets.token_hex(16)>`), frozen `ClaimedJob` dataclass (job identity + stage + asset identity + `input_fingerprint` + required capabilities + lease fields) whose `lease_token` is a private attribute exposed only via property — hidden from `repr()` and `to_public_dict()`.
- `src/operations/store.py` protocol layer (M6-01 primitives reused; no sealed semantics rewritten):
  - **Atomic claim** `claim_next_job(db, worker_id, capabilities, *, lease_duration_seconds=120, now)`: `BEGIN IMMEDIATE`, single transaction = select eligible `QUEUED` job (retry time respected, `next_retry_at IS NULL OR <= now`), capability all-of subset check, fresh lease token, `QUEUED→LEASED` + lease fields, `job_claimed` event, commit. Concurrency-safe (single winner).
  - **Claim ordering** (frozen deterministic v1): `priority DESC, enqueued_at ASC, job_id ASC`.
  - **Renewal** `renew_job_lease`: `LEASED`/`RUNNING` + owner/token match only, extends `lease_expires_at`, no per-renew event.
  - **Start** `start_claimed_job`: atomic `LEASED→RUNNING` + attempt creation (monotonic `attempt_number`) + `attempt_count` update in the same transaction; attempt is counted at start, so a crash after start consumes an attempt (see Decision 26).
  - **Completions** `complete_job_success` / `complete_job_retryable_failure` / `complete_job_terminal_failure` via shared `_complete_with_attempt_outcome`: fence on token, finish active attempt (outcome/error_class/error_message/metadata persisted), state decided by the store (`SUCCEEDED` / `FAILED_RETRYABLE`+backoff / `FAILED_TERMINAL` on exhaustion), lease cleared for non-running outcomes. Stale/old tokens never commit success.
  - **Expired-lease recovery** `recover_expired_leases(now)`: `LEASED`→`QUEUED` (no attempt consumed, `lease_expired_requeued` event); `RUNNING`→close active attempt as `lease_expired`/`WorkerLeaseExpired` retryable (attempt counts), `FAILED_RETRYABLE`+backoff (never immediate reclaim) or `FAILED_TERMINAL` on exhaustion.
  - **Heartbeat/staleness** `worker_heartbeat` (M6-01) + `is_worker_stale`/`list_workers_with_status` (derived from `last_heartbeat_at` + `stale_threshold_seconds=120`; staleness never directly fails jobs).
  - `StaleLeaseError` for any ownership mutation whose token is not the current DB token (fencing).
- `src/operations/worker.py` — generic `WorkerRuntime`: `register()`/`heartbeat()`, `run_once()` (heartbeat → claim → start → injected stage handler → complete; `RetryableJobError`→retryable, `TerminalJobError`→terminal, unknown `Exception`→retryable until `max_attempts`), handler registry keyed by stage, lightweight daemon heartbeat thread for long-running handlers (worker heartbeat + active-job lease renewal), `lease_lost` detection (fenced completions never committed), graceful `stop()` (no complex process kill), `run_forever(max_cycles=None)`. No M2–M5 imports; handlers are injected callables (M6-03 provides real adapters).
- Additive schema correction: `jobs.required_capabilities_json` (Decision 24) — `operations-store-v1` retained, no migration engine, old dev DBs require rebuild, production DB not created.
- `validate_operations_store` extended: active-attempt invariant (RUNNING ⇒ exactly 1 active attempt; non-RUNNING ⇒ 0), lease completeness (LEASED/RUNNING require full lease; non-leased states require none), capability JSON validity for workers and jobs.
- `tests/test_operations_worker.py` — 60 tests (registration, capabilities, atomic claim, ordering, capability matching, nonmatching stays QUEUED, lease fields, unique token, renewal, wrong-owner/stale-token rejection, start valid/stale, attempt-on-start, single active attempt, success/retryable/terminal completion, stale success/failure rejection, LEASED-before-start expiry no attempt, RUNNING expiry closes attempt + retryable + backoff + exhaustion terminal, heartbeat, worker stale derived status, stale worker doesn't fail job, two-worker concurrent claim single winner, reclaim after expiry, stale fencing after reclaim, WorkerRuntime idle/success/retryable/terminal/unknown-exception, handler registry, long-handler renewal design, graceful shutdown, token hidden from repr/log, validation invariants, full synthetic crash/recovery flow).
- Real verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py` = 136 passed; full `pytest tests -q` = 1401 passed / 10 skipped. No production `data/operations/operations.sqlite3`; no LLM/GPU/network; M2–M5 untouched.

---

## M6-03: Pipeline Stage Adapters for M2→M5 (`TODO`)

### Objective
Wrap every audited M2–M5 entry point (architecture doc §2) behind a uniform stage-adapter interface with invariant-gated success.

### Scope hints
- DISCOVER → collector sync adapter (`src.collector.cli` semantics / `CollectorService`).
- ARCHIVE → downloader worker / `SafeDouyinDownloader`.
- MEDIA_PROCESS/EVIDENCE_READY → `process_canonical_asset` / `process_canonical_album` + `write_evidence_manifest`/`write_evidence_chunks`.
- KNOWLEDGE_EXTRACT/FINALIZE → `extract_knowledge_candidates` / `merge_knowledge_candidates` / `enrich_knowledge_candidates` / `finalize_knowledge_document`.
- STORE_INGEST → `ingest_knowledge_document` (+ `validate_store`).
- Success = artifact invariant (§17 of architecture doc), never exit code.

---

## M6-04: Scheduler + Automatic Downstream Orchestration (`TODO`)

### Objective
Scheduler (NAS) that polls collections, enqueues DISCOVER, and triggers downstream stages on success; dispatches by capability ∩ resources.

### Scope hints
- Trigger model A/B/C (scheduled polling, heartbeat, downstream trigger).
- Configurable polling interval (no frozen N).
- Duplicate suppression + cursor/checkpoint reuse of M2 watermark.
- No nightly full rerun.

---

## M6-05: Crash Recovery / Retry / Observability (`TODO`)

### Objective
Retry policy (RETRYABLE vs TERMINAL, attempt/max/backoff), lease expiry requeue, event_log, and admin/status surface.

### Scope hints
- Recovery test plan from architecture doc §27 (scheduler crash, worker crash, PC shutdown during ASR, network disconnect, duplicate discovery/enqueue, lease expiry, partial download, corrupted artifact, LLM runtime unavailable, store ingest failure, restarts after hours/days).
- Admin ops: list pending/failed, retry, cancel, requeue, worker status, asset pipeline status.

---

## M6-06: Windows PC Worker Autostart (`TODO`)

### Objective
Boot/login autostart for the PC worker (capability-based GPU/media/collector worker).

### Scope hints
- Deployment contract only (architecture doc §24); service registration happens here, not in M6-00.

---

## M6-07: NAS Docker Control Plane Deployment (`TODO`)

### Objective
Docker Compose / service autostart for the NAS control plane + storage mounts + Knowledge Store ownership.

### Scope hints
- Must respect the M2 browser-runtime portability constraint (Topology B blocked until resolved).

---

## M6-08: Real Douyin Favorite → Searchable Knowledge E2E (`TODO`)

### Objective
End-to-end: real Douyin favorite → collection detection → archive → media → evidence → extraction → finalize → store ingest → SEARCHABLE.

### Scope hints
- Uses real C10 corpus assets for validation of stages already covered by M4/M5.
- PC must be the authentic collector/downloader location in v1 (Topology A).

---

## M6-09: Final Acceptance (`TODO`)

### Objective
Acceptance run covering the recovery scenarios + E2E; produce final acceptance doc.

---

## Deferred (not in M6)
Knowledge truth verification, dense/hybrid retrieval, reranker, RAG answer generation, Obsidian publishing, web end-user UI, global entity graph.

---

## Frozen Commit Contract (from M6-00)
- M6-00 commit message: `docs(m6): design automated knowledge operations architecture`.