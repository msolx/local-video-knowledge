# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Task Board

> **Status**: M6-00 DONE; M6-01 DONE; M6-02 DONE; M6-03 DONE; M6-04 DONE; M6-05 DONE; M6-06 DONE; M6-07..M6-09 TODO.

---

## Status Matrix

| Milestone | Status | Deliverable |
|---|---|---|
| M6-00 | DONE | `docs/M6_OPERATIONS_ARCHITECTURE.md`, `docs/M6_DECISIONS.md`, `docs/M6_TASKS.md`, `docs/M6_HANDOFF.md` |
| M6-01 | DONE | `src/operations/` durable store + job state machine, `tests/test_operations_store.py` |
| M6-02 | DONE | Local Worker Runtime + Capability/Lease Protocol |
| M6-03 | DONE | Pipeline Stage Adapters for M2→M5 |
| M6-04 | DONE | Scheduler + Automatic Downstream Orchestration |
| M6-05 | DONE | Crash Recovery / Retry / Observability |
| M6-06 | DONE | Windows PC Worker Host & Autostart |
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

## M6-03: Pipeline Stage Adapters for M2→M5 (`DONE`)

### Objective
Wrap every audited M2–M5 entry point (architecture doc §2) behind a uniform stage-adapter interface with invariant-gated success.

### Delivered
- `src/operations/stages.py` — the stage adapter layer. Frozen constants `STAGES_POLICY_VERSION = m6-stages-policy-v1`, `STAGE_EXECUTION_RESULT_SCHEMA_VERSION = m6-stage-execution-result-v1`, `STATUS_EXECUTED`/`STATUS_CACHE_HIT`; typed frozen `StageExecutionResult` (`stage`, `canonical_id`, `status`, `input_fingerprint`, `output_fingerprint`, `artifacts` descriptors `{role,path,sha256}`, `metadata`) with `validate()` + JSON-safe guarantee (no sqlite Row / `Path` / Enum repr / secrets). `validate_stage_execution_result(result, claimed)` enforces identity (`stage`/`canonical_id`/`input_fingerprint` vs the claimed job, SHA-256 output fingerprint).
- `fingerprint_artifacts(...)`/`stage_output_fingerprint(...)` — deterministic SHA-256 over canonical-ordered `(role,path,sha256)` descriptors + stage policy version; reuses frozen fingerprints where a stage already has one (M4 `fingerprint`/`finalization_fingerprint`, M5 `source_artifact_fingerprint`).
- `required_capabilities_for_stage(stage, media_type=None)` — frozen map (Decision 29): DISCOVER→collector, ARCHIVE→downloader, MEDIA_PROCESS(video)→gpu_asr, MEDIA_PROCESS(album)→gpu_vlm, KNOWLEDGE_EXTRACT→llm_extraction, KNOWLEDGE_FINALIZE→() , STORE_INGEST→store_ingest.
- Adapters (each: preflight → sealed-call → invariant postcondition → `StageExecutionResult`):
  - `DiscoverAdapter` — wraps `CollectorService.execute(mode)`; returns discovered identities (batch semantics, dedup); maps `CollectorErrorCode` → Retryable (DEPENDENCY_NOT_READY/AUTH_NOT_READY/RUNTIME_ERROR/LOCKED) vs Terminal (CONFIG_ERROR/NOT_IMPLEMENTED/UNKNOWN); no polling loop (M6-04).
  - `ArchiveAdapter` — wraps `SafeDouyinDownloader`/downloader service; cache-hit iff `CanonicalMediaAssetAdapter.load_from_content_id` yields a formal archive (`asset_manifest.json`); missing `source_url` in job metadata → Retryable; auth/runtime preconditions → Retryable; downloader success must produce a loadable archive else Terminal.
  - `MediaProcessAdapter` — wraps `process_canonical_asset`/`process_canonical_album`; cache-hit iff `verify_evidence_manifest` + `verify_evidence_chunks` pass (schema + fingerprint + `not_checked`); partial/corrupt evidence files present → Terminal (never silently overwrite); media_type routing (video ASR vs album OCR/VLM) drives capability + processor selection; postcondition re-verifies evidence.
  - `KnowledgeExtractAdapter` — chains `extract_knowledge_candidates` → `merge_knowledge_candidates` → `enrich_knowledge_candidates`; respects M4 cache/fingerprint semantics (never "file exists → skip"); `FileNotFoundError` upstream → Retryable; postcondition: enriched artifact schema-valid.
  - `KnowledgeFinalizeAdapter` — wraps `finalize_knowledge_document`; cache-hit iff `knowledge_units.json` parses via `CanonicalKnowledgeUnitsDocument.from_dict()` AND `canonical_id` matches the asset; upstream missing → Retryable; success requires re-parse; never mutates KU fields/verification status.
  - `StoreIngestAdapter` — wraps `ingest_knowledge_document` against the configured knowledge store; `inserted`/`replaced`/`unchanged` all succeed (CACHE_HIT on unchanged); verifies `get_ingested_asset` + unit-count; `rebuild_store` never used for normal ingest; DB lock → Retryable.
- `build_stage_handler_registry(...)` — dependency-injected adapter registry (workspace/processed/archive roots, knowledge store path, collector, downloader, media adapter, app config, LLM backend, M4 configs) returning the `{stage: handler}` mapping directly consumable by `WorkerRuntime`; all deps faked in tests.
- `src/operations/worker.py` — accepts a `StageExecutionResult` handler return, validates it, and persists the JSON payload into the successful attempt `metadata_json` (`stage_result`); `store.py` gains `get_job_result(job_id)` for the durable downstream fingerprint handoff (Decision 30); `ClaimedJob` carries the job `metadata` reference.
- `tests/test_operations_stages.py` — 55 tests covering: result serialization/JSON-safety, deterministic + path-ordered fingerprinting, invalid fingerprint/stage/canonical/input mismatch rejection, capability mapping for all stages, discover success/duplicate/retryable/missing-runtime, archive fake-success/valid-cache-hit/invalid-existing-not-cache/retryable-auth/missing-runtime, media video+album cache-hit/invalid-evidence/fingerprint, knowledge extract cached-chain + missing-evidence, finalize cache-hit/canonical-mismatch-not-cache/schema-invalid-not-cache, store ingest inserted/unchanged(CACHE_HIT)/replaced/disposable-db-only, handler registry, WorkerRuntime integration + result persistence + durable fingerprint read, stale-token-cannot-complete, at-least-once replay (EXECUTED → crash → CACHE_HIT, identical fingerprint, one artifact), changed-input-invalidates, partial-artifact-rejected, secrets absent from results, and real C10 video+album offline cache audits (read-only artifacts, disposable knowledge DBs).
- Real verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py tests/test_operations_stages.py` = 191 passed; full `pytest tests -q` = 1456 passed / 10 skipped. No production `data/operations/operations.sqlite3` created, no production `data/knowledge/knowledge_store.sqlite3` modified (disposable temp DBs only), no network/GPU/LLM/live Douyin; M2–M5 production code untouched (only M6 `operations` modules extended additively).

---

## M6-04: Scheduler + Automatic Downstream Orchestration (`DONE`)

### Objective
Scheduler (NAS) that polls collections, enqueues DISCOVER, and triggers downstream stages on success; dispatches by capability ∩ resources.

### Scope hints
- Trigger model A/B/C (scheduled polling, heartbeat, downstream trigger).
- Configurable polling interval (no frozen N).
- Duplicate suppression + cursor/checkpoint reuse of M2 watermark.
- No nightly full rerun.

### Delivered
- `src/operations/scheduler.py` — the reconciliation/level-triggered control-plane scheduler (Decision 34). Frozen `SCHEDULER_POLICY_VERSION = m6-scheduler-policy-v1`, `SCHEDULER_SCHEMA_VERSION = m6-scheduler-v1`; frozen `SchedulerCycleResult` (10 counters: `expired_leases_recovered`, `retry_jobs_requeued`, `assets_registered`, `pipeline_runs_created`, `jobs_enqueued`, `lifecycles_advanced`, `runs_completed`, `runs_failed`, `polls_scheduled`, `invariant_failures`; JSON-safe). Deterministic `run_once(now)` in five frozen phases: (1) recover expired leases → (2) requeue due `FAILED_RETRYABLE` → (3) process completed DISCOVER results → (4) reconcile asset pipelines (lifecycle advance + missing downstream enqueue + run finalize) → (5) schedule DISCOVER polls. `run_forever(poll_interval_seconds, stop_event)` is stdlib-only (threading.Event), no external queue framework.
- DISCOVER is a batch producer (Decision 35): each discovered identity is independently registered + enqueued an `ARCHIVE` job under its own pipeline run; processed DISCOVER jobs are tracked in `scheduler_state.processed_discover_jobs` to prevent event spam and duplicate processing across cycles.
- DISCOVER control identity (Decision 36): reserved control asset `(platform, "__discover__")` → `control_{platform}_{source_key}_discover`; `poll_slot_fingerprint` uses a deterministic `poll_slot = epoch_seconds(now) // interval_seconds` (documented scheduler trigger identity, not a knowledge/artifact identity); overlap suppression while any DISCOVER job is active; `discovery_control_fingerprint(..., generation=N, ...)` bumps `N` when a prior ARCHIVE generation ended terminal/cancelled so re-discovery forms a new ARCHIVE generation; M2 collector stays the sole watermark/cursor owner.
- `ASSET_PIPELINE_GRAPH = (ARCHIVE, MEDIA_PROCESS, KNOWLEDGE_EXTRACT, KNOWLEDGE_FINALIZE, STORE_INGEST)`; `STAGE_MILESTONES = {ARCHIVE: ARCHIVED, MEDIA_PROCESS: EVIDENCE_READY, KNOWLEDGE_FINALIZE: KNOWLEDGE_READY, STORE_INGEST: SEARCHABLE}` (KNOWLEDGE_EXTRACT has no milestone). Downstream enqueue reads `get_job_result(job_id).output_fingerprint` → next `input_fingerprint`; `CACHE_HIT` and `EXECUTED` both advance (Decision 37). `required_capabilities_for_stage(next_stage, media_type=...)` reused verbatim; `media_type` for `MEDIA_PROCESS` routing comes from the ARCHIVE result `metadata` (minimal `ArchiveAdapter` gap-fill, M2 untouched).
- Lifecycle is "highest completed milestone" (monotonic; never regress — `transition_asset_lifecycle` only on strict rank increase); freshness is the run/job generation; a `SEARCHABLE` asset can host a new RUNNING refresh run without lifecycle regression (Decision 38). Pipeline run completion is derived: `SUCCEEDED` only when `STORE_INGEST` SUCCEEDED + asset `SEARCHABLE`; `FAILED_TERMINAL`/`CANCELLED` current-generation job fails/cancels the run; RUNNING runs are reused to avoid duplicates (Decision 39).
- Retry requeue + lease recovery are scheduler phase-0 duties (Decision 40): `recover_expired_leases(now)` reused from M6-02 (never reimplemented); due retryable jobs requeued with the SAME `job_id`. PC-offline: capability-missing jobs stay `QUEUED`, never FAILED; no attempt consumed while offline.
- Scheduler state persistence (Decision 41): additive `scheduler_state` table (`scheduler_key PK`, `last_scheduled_at`, `last_completed_at`, `next_due_at`, `metadata_json`) via `get/set_scheduler_state`; `list_pipeline_runs(canonical_id, status)` added for active-run lookup; `operations-store-v1` retained (old dev Ops DB requires rebuild; no migration engine). Events are mutation-delimited (Decision 42) — idle cycles append nothing.
- `tests/test_operations_scheduler.py` — 39 tests covering: `SchedulerCycleResult` serialization, empty cycle no-op, lease recovery integration, retry before/at due (same job identity), poll due/not-due/overlap-suppression/generation determinism, DISCOVER batch processing (multiple + duplicate + dedup), pipeline-run create/suppression, per-stage enqueue + lifecycle progression (ARCHIVE→ARCHIVED, MEDIA→EVIDENCE_READY, FINALIZE→KNOWLEDGE_READY, STORE_INGEST→SEARCHABLE), run success, cache-hit continues, required capabilities reused (incl. media_type routing), downstream input=upstream output fingerprint, missing/invalid stage-result invariant failure, terminal failure stops downstream + run FAILED + asset stays ARCHIVED, cancelled run no downstream, PC-offline stays QUEUED with no attempt, duplicate cycle produces no duplicate jobs/events, restart recovery (new Scheduler instance on same DB self-heals downstream), changed fingerprint → new generation without lifecycle regress, refresh run on SEARCHABLE asset, scheduler-state persistence, `run_forever` stop, synthetic unattended full E2E (DISCOVER→…→SEARCHABLE, zero manual downstream enqueues), and real C10 video+album offline chains through real adapters into disposable M5 stores.
- Real verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py tests/test_operations_stages.py tests/test_operations_scheduler.py` = 230 passed; full `pytest tests -q` = 1495 passed / 10 skipped. No production `data/operations/operations.sqlite3` created, no production `data/knowledge/knowledge_store.sqlite3` modified (disposable temp DBs only), no network/GPU/LLM/live Douyin; M2–M5 production code untouched.

---

## M6-05: Crash Recovery / Retry / Observability (`DONE`)

### Objective
Retry policy (RETRYABLE vs TERMINAL, attempt/max/backoff), lease expiry requeue, event_log, and admin/status surface.

### Scope hints
- Recovery test plan from architecture doc ·27 (scheduler crash, worker crash, PC shutdown during ASR, network disconnect, duplicate discovery/enqueue, lease expiry, partial download, corrupted artifact, LLM runtime unavailable, store ingest failure, restarts after hours/days).
- Admin ops: list pending/failed, retry, cancel, requeue, worker status, asset pipeline status.

### Deliverables
- `src/operations/observability.py` — pure read-only health projection: frozen health vocabulary (`HEALTHY/RUNNING/WAITING_FOR_WORKER/WAITING_RETRY/SUCCEEDED/FAILED_TERMINAL/CANCELLED/STALLED/INVARIANT_ERROR`), `MANUAL_ATTENTION_HEALTH`, `AssetPipelineStatus`, `WorkerStatus`, `OperationsSummary`, `get_asset_pipeline_status`, `list_asset_pipeline_statuses`, `get_worker_status`, `list_worker_statuses`, `compute_operations_summary`, `get_asset_timeline`. Never writes; never emits `lease_token`.
- `src/operations/admin.py` — `run_recovery_pass` / `startup_recovery` (compose frozen store primitives: validate → recover leases → requeue due retryables → reconcile runs), `admin_retry_job` (respects backoff/exhaustion; terminal requires `force` + new input fingerprint), `admin_cancel_job`, `admin_requeue_asset`; `RecoveryResult`/`AdminRetryResult`/`AdminCancelResult` JSON-safe.
- `src/operations/cli.py` — thin secret-scrubbed CLI: `status`, `asset`, `jobs`, `failed`, `workers`, `timeline`, `retry`, `cancel`, `recover`; `--db` (env `OPERATIONS_DB_PATH`) + `--json`; `_secret_free` strips tokens.
- `docs/M6_RECOVERY_RUNBOOK.md` — recovery matrix (16 scenarios, AUTO/MANUAL), startup recovery, observability vocabulary, admin ops, M4 incident guard rule.
- Tests: `tests/test_operations_recovery.py` (32 tests incl. fault matrix + real C10 offline recovery to searchable on disposable roots + M4 incident destructive-root guard regression), `tests/test_operations_observability.py`, `tests/test_operations_cli.py`.

### Verification
- Targeted M6 suite (`store/worker/stages/scheduler/recovery/observability/cli`): 302 passed.
- Incident acceptance (`test_m4_acceptance` + `test_m4_incident_recovery`): 54 passed.
- Full regression `pytest tests -q`: 1588 passed / 10 skipped / 0 failed.
- Live C10 unchanged (video 62 KU, album 6 KU); production M5 store unchanged (2 assets / 68 KU, revision `7b604b33…`).
- No production ops DB created; no network / GPU / LLM / live Douyin used.

---

## M6-06: Windows PC Worker Host & Autostart (`DONE`)

### Objective
Wrap the frozen `WorkerRuntime` into a reliable Windows long-running worker host with explicit configuration, capability preflight, single-instance enforcement, rotating redacted logging, graceful shutdown, deterministic exit codes, and Task Scheduler autostart artifacts.

### Scope hints (from M6-00 contract)
- Deployment contract only (architecture doc §24); Task Scheduler artifacts ship ready-to-install but production registration is deferred to M6-08.
- The Windows worker must NOT open a NAS-hosted `operations.sqlite3` over SMB/UNC; the operations DB is NAS-control-plane-owned (Decision 32/54).

### Delivered
- `src/operations/windows_worker.py` — the Windows PC Worker Host. Frozen contracts: `WORKER_HOST_CONFIG_VERSION = m6-windows-worker-config-v1`, `WORKER_PREFLIGHT_RESULT_VERSION = m6-windows-worker-preflight-v1`, `WORKER_HOST_POLICY_VERSION = m6-windows-worker-host-v1`; frozen exit codes (`EXIT_OK=0`, `EXIT_CONFIG_ERROR=2`, `EXIT_PREFLIGHT_FAILURE=3`, `EXIT_ALREADY_RUNNING=4`, `EXIT_FATAL_RUNTIME_ERROR=5`); `WINDOWS_PC_TARGET_CAPABILITIES = (collector, downloader, cpu_media, gpu_asr, gpu_vlm, llm_extraction)` (no `store_ingest`); transport placeholders `local_sqlite_test` (only runnable) / `http` (fails closed until M6-07/08).
  - `WorkerHostConfig` (frozen dataclass): worker_id, capabilities, workspace/archive/processed roots, `operations_db_path` (local-dev/test only), knowledge_store_path, poll/heartbeat/lease/stale intervals, log path + rotation, `single_instance_lock_path`, transport, `startup_delay_seconds`, `runtime_references` (browser executable/profile, node, ASR/VLM/LLM runtimes + model roots). `load/from_dict/to_dict/export_json`; rejects unknown schema, empty worker_id, invalid/empty capabilities, non-`{local,http}` transport. Secrets never live in config.
  - `WorkerPreflightResult`/`PreflightCheck` — JSON-safe; checks are existence/configuration only (never launch browser/Douyin/Whisper/llama.cpp). Missing required prerequisite → failed preflight (exit 3), never a silent capability drop.
  - `SingleInstanceLock` — OS file lock (`msvcrt` win32 / `fcntl` else); stale lock file re-locked (never fatal); second instance → exit 4.
  - `configure_worker_logging` — stdlib `RotatingFileHandler` (default `logs/operations/windows-worker.log`, 10 MB × 5 backups); `redact_config`/`redact_worker_registration`/`redact_message` strip secret-shaped keys (`lease_token`, cookies, tokens, API keys, password, auth…).
  - `WindowsWorkerHost.start()` — lifecycle: hard SMB/UNC + transport guards → fail-closed on missing `operations_db_path` → preflight → single-instance lock → build `WorkerRuntime` → register (redacted) → heartbeat thread → SIGINT/SIGTERM graceful stop → `run_forever` → `EXIT_OK`; `KeyboardInterrupt` → `EXIT_OK`; fatal → `EXIT_FATAL_RUNTIME_ERROR` (redacted). Host never calls `startup_recovery` (control-plane duty).
  - CLI `python -m src.operations.windows_worker {run|preflight|print-config} --config <path> [--json]`. `run` requires an explicit local `operations_db_path` (fails closed otherwise — never auto-creates production `data/operations/operations.sqlite3`).
- `src/operations/__init__.py` — lazy PEP 562 `__getattr__` exports for windows_worker symbols (avoids the runpy `-m` stderr warning; `python -m src.operations.windows_worker` is stderr-clean for Task Scheduler).
- `config/examples/m6_windows_worker.example.json` — tracked example (all 6 PC capabilities, `local_sqlite_test`, browser/Chrome profile + `G:\llama.cpp` / `D:\LMmodel` references, `operations_db_path: null`). No secrets. Real local config lives at gitignored `config/local/` (`config/local/` added to `.gitignore`).
- `scripts/windows/run_m6_worker.ps1` — resolves repo root + exact venv Python (`.venv\Scripts\python.exe`), invokes `-m src.operations.windows_worker`, propagates exit code. No worker logic.
- `scripts/windows/install_m6_worker_task.ps1` — validates config via `print-config`, builds the ScheduledTask definition (At LogOn + configurable delay, exact venv Python via the run script, `WorkingDirectory` = repo root, restart every 1 min / high count, no execution-time limit, limited interactive principal). **Dry-run by default**; `-Apply` is explicitly not intended until M6-08.
- `scripts/windows/uninstall_m6_worker_task.ps1` — idempotent (missing task = no-op exit 0).
- `scripts/windows/status_m6_worker_task.ps1` — read-only status (exists/state/last run/result/next run; secret-free actions).
- `docs/M6_WINDOWS_WORKER_RUNBOOK.md` — full runbook (role, topology boundary, local-dev vs production, capability profile, preflight, logging, single instance, exit codes, Task Scheduler design, install/dry-run/uninstall/status, sleep/shutdown, no-SQLite-over-SMB rule, why production registration is deferred).
- `tests/test_operations_windows_worker.py` — 54 tests covering: config parse/round-trip/invalid-schema/missing-worker_id/invalid+duplicate capability normalization/local-dev transport/production-rejects-local-topology/UNC rejection/example-no-secrets; preflight success + per-prerequisite failures + no-runtime-start + JSON-safe + no-secret-exposure; single-instance acquire/reject/release/stale-file; logging init/rotation/redaction; host lifecycle (start/WorkerRuntime constructed/heartbeat-registration delegation/graceful stop/KeyboardInterrupt/fatal exit/config exit/preflight exit/already-running exit); safety (no production Ops DB auto-created via host AND via CLI, no production M5 modified, no network, no LLM/GPU); PowerShell artifacts (run-script path, install dry-run, deterministic task name, logon trigger, startup delay, exact venv Python, working dir, restart policy, uninstall idempotency, status read-only, no Task Scheduler mutation); real install dry-run = 0 mutation.

### Verification
- Targeted M6 suite (`store/worker/stages/scheduler/recovery/observability/cli/windows_worker`): **356 passed**.
- Incident acceptance (`test_m4_acceptance` + `test_m4_incident_recovery`): **54 passed**.
- Full regression `pytest tests -q`: **1642 passed / 10 skipped / 0 failed**.
- Live C10 unchanged (video 62 KU, album 6 KU); production M5 store unchanged (2 assets / 68 KU, revision `7b604b33…`).
- PowerShell syntax validation: all 4 `.ps1` scripts parse clean (PowerShell `Parser::ParseFile`, 0 errors). Install dry-run confirmed 0 Task Scheduler mutation.
- No production operations DB created; **no production scheduled task registered**; no network / GPU / LLM / live Douyin used.

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