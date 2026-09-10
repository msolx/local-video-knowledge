# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Handoff

> **Milestone Status**: `M6-01 = DONE`, `M6-02 = DONE`, `M6-03 = DONE`, `M6-04 = DONE`; `M6-05 = NEXT`; `M6-06..M6-09 = TODO`
> **Branch**: `feat/m6-automated-knowledge-operations`
> **M2/M3/M4/M5**: COMPLETE / SEALED (do not modify).

---

## 1. Current State

- M6-00 (design) and M6-01 (durable operations store + job state machine) are complete on `feat/m6-automated-knowledge-operations`, branched from `main` at `e6476be57564d950b7eb75016eed54b1cbf32fec` (M5 final SHA).
- M6-01 delivered `src/operations/` (`models.py`, `store.py`, `__init__.py`) + `tests/test_operations_store.py` (76 tests, all passing). No production operations DB created; no M2–M5 code modified; no runtime started.
- Docs:
  - `docs/M6_OPERATIONS_ARCHITECTURE.md`
  - `docs/M6_DECISIONS.md` (now 21 decisions; Decision 21 = additive `CANCELLED` job state)
  - `docs/M6_TASKS.md` (M6-01 DONE)
  - `docs/M6_HANDOFF.md`

---

## 2. Key Facts Carried Forward

- Canonical asset identity remains `(platform, platform_content_id)` — frozen, not re-created.
- Stage graph (frozen): `DISCOVER → ARCHIVE → MEDIA_PROCESS → EVIDENCE_READY → KNOWLEDGE_EXTRACT → KNOWLEDGE_FINALIZE → STORE_INGEST → DONE`.
- Asset lifecycle: `DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE`.
- Job lifecycle: `QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL` (+ additive terminal `CANCELLED` from admin cancel, Decision 21; no `FAILED_PC_OFFLINE`/`WAITING_FOR_PC` — capability wait stays `QUEUED`).
- Job idempotency: `job_id = sha256(platform|platform_content_id|stage|input_artifact_fingerprint|policy_version)[:16]`; SUCCEEDED same-key job = SKIP.
- Lease model: `lease_owner/leased_at/lease_expires_at` + heartbeat; expiry → safe requeue.
- Retry: RETRYABLE (PC offline, file lock, LLM down, network) with attempt/max/backoff; TERMINAL (corrupt source, schema failure, unsupported media) without auto-retry.
- PC offline = normal wait (`QUEUED`), never FAILED.
- Capabilities: `collector`, `downloader`, `cpu_media`, `gpu_asr`, `gpu_vlm`, `llm_extraction`, `store_ingest`.
- GPU v1 rule: one GPU-heavy job per worker.
- Stage success = artifact invariant (never exit code) — see architecture doc §17.
- M5 ingest is incremental; `rebuild_store` is recovery/admin only.
- Uncollection ≠ deletion.
- Secrets never enter Git / job DB payloads / logs.
- Topology A (NAS control+storage+store; PC browser+GPU) is the frozen v1 topology.
- Operations DB: `data/operations/operations.sqlite3` (logical `ops.db_path`), separate from M5 store.

---

## 3. M2 Portability Constraint (resolved classification, blocking Topology B)

The Douyin collector browser runtime (`src/collector/douyin/browser_runtime.py`) is classified **PC-only-for-now**:
- Hardcoded `C:\Program Files\Google\Chrome\Application\chrome.exe`.
- Hardcoded `G:\antigravity-cli\dy\runtime\chrome-profile`.
- G:-drive `NODE_PATH` candidates.
- win32 process control (`ctypes.windll`, `taskkill`).
- The existing Windows Chrome profile's Linux/Docker portability is **unproven** → marked `deployment constraint / unresolved portability issue`.

Consequence: collection/downloader remain on the PC in v1 (Topology A). Before any NAS-side collection, this must be reworked (configurable Chrome path, env-resolved profile + NODE_PATH, cross-platform process control) and re-audited.

---

## 4. Stage Entry Points M6-03 Must Wrap (authoritative, audited from source)

| Stage | Entry (real code) |
|---|---|
| DISCOVER | `src.collector.cli` (`collector douyin {probe\|sync\|backfill}`), `CollectorService` + `DouyinCollectorConfig` (aid=6383, webapp, channel_pc_web) |
| ARCHIVE | `src.downloader.worker` (`--once\|--drain\|--serve`), `DownloaderWorkerService`, `SafeDouyinDownloader` (F2 backend, sandbox+atomic promotion) |
| MEDIA_PROCESS / EVIDENCE_READY | `src.pipeline.py:process_canonical_asset` (video, default boundary `asr`) / `process_canonical_album` (visual/OCR), `write_evidence_manifest` (`provenance.py:455`), `write_evidence_chunks` (`chunking/service.py:120`), `probe_media` (`intake/media_probe.py:38`), `CanonicalMediaAssetAdapter` |
| KNOWLEDGE_EXTRACT | `extract_knowledge_candidates(processed_dir, config, backend)` (`knowledge/extractor.py:1097`) → `knowledge_candidates.json` |
| KNOWLEDGE_FINALIZE | `merge_knowledge_candidates` (`merger.py:261`) → `enrich_knowledge_candidates` (`enrichment.py:846`) → `finalize_knowledge_document` (`render.py:368`) → `knowledge_units.json` |
| STORE_INGEST | `ingest_knowledge_document(db, knowledge_units_path)` (`store.py:622`) + `validate_store`; recovery = `rebuild_store` (`store.py:1060`) |
| Retrieval surface | `retrieve(db, query)` (`retrieval.py`); `evaluate_suite` (`evaluation.py`) + `evaluation/m5/c10_golden_queries.json` |

Legacy pre-M4 LLM path (`build_knowledge`, LM Studio `qwen3.6-27b-knowledge`) is superseded by M4 for production units; M6 uses the M4 path.

---

## 5. M6-01 Deliverables Completed

- `src/operations/models.py` — frozen `operations-store-v1` contract: `AssetLifecycleState`, `JobState` (incl. additive `CANCELLED`), `JobStage` (`DISCOVER | ARCHIVE | MEDIA_PROCESS | EVIDENCE_READY | KNOWLEDGE_EXTRACT | KNOWLEDGE_FINALIZE | STORE_INGEST`), `PipelineRunStatus`, `TriggerType` (`discovery|manual|recovery`), frozen legal-transition tables, `compute_job_id` (`job_<sha256(platform|platform_content_id|stage|input_fingerprint|policy_version)[:16]>`), `normalize_input_fingerprint` (stable 64-hex SHA-256 required; timestamp substitution rejected), UTC-ISO timestamps, Python-side backoff → `next_retry_at`.
- `src/operations/store.py` — `PRAGMA user_version=1`, `operations_meta`, 7 tables (`assets`, `pipeline_runs`, `jobs`, `job_attempts`, `workers`, `event_log`), `foreign_keys=ON`, `BEGIN IMMEDIATE` mutations, enqueue idempotency (SUCCEEDED→SKIP; active→exists; retryable→retry path; terminal/cancelled→no silent resurrection; changed input/policy→new generation), central transition engine (`transition_job_state`, `transition_asset_lifecycle`), `cancel_job` / `requeue_retryable_job` (terminal/exhausted → explicit reject), lease-field persistence (`set_job_lease`/`clear_job_lease`, no worker protocol yet), `begin_attempt`/`finish_attempt` (monotonic numbering, exhaustion→`FAILED_TERMINAL`), `register_worker`/`worker_heartbeat`, append-only `event_log`, full read API, `validate_operations_store`. No hard-delete API; no secret payloads; no production DB.
- `src/operations/__init__.py` — full export surface.
- `tests/test_operations_store.py` — 76 tests (init/version/tables, asset lifecycle legal+illegal+admin override, job id determinism, enqueue idempotency + changed-input generations, pipeline runs, transitions, terminal immutability, CANCELLED semantics, retry/backoff/exhaustion, attempts, event log append-only, workers, lease fields, transaction rollback, FK integrity, validation clean+corrupted, list APIs, Unicode, SQL-injection safety, UTC, synthetic complete flow, duplicate discovery). Target run: 76 passed.

---

## 5b. M6-02 Deliverables Completed

- `src/operations/models.py` extensions — `VALID_CAPABILITIES` (frozen: `collector`, `downloader`, `cpu_media`, `gpu_asr`, `gpu_vlm`, `llm_extraction`, `store_ingest`), `normalize_capability`/`normalize_capabilities`, `new_lease_token`, frozen `ClaimedJob` (token hidden from `repr`/`to_public_dict`).
- `src/operations/store.py` protocol layer — atomic `claim_next_job` (BEGIN IMMEDIATE; ordering `priority DESC, enqueued_at ASC, job_id ASC`; capability all-of subset; fresh token; `QUEUED→LEASED`), `renew_job_lease`, `start_claimed_job` (LEASED→RUNNING + attempt created + counted in same txn), `complete_job_success`/`complete_job_retryable_failure`/`complete_job_terminal_failure` (token-fenced shared completion; store decides final state incl. exhaustion), `recover_expired_leases` (LEASED→QUEUED no attempt; RUNNING→closed attempt retryable + backoff or terminal), `is_worker_stale`/`list_workers_with_status` (derived from heartbeat + threshold), `StaleLeaseError` fencing. Additive `jobs.required_capabilities_json` (Decision 24; `operations-store-v1` retained). `validate_operations_store` extended (active-attempt invariant, lease completeness, capability JSON validity). Tokens never written to event/log payloads.
- `src/operations/worker.py` — generic `WorkerRuntime` (register/heartbeat/run_once/run_forever/stop; injected stage-handler registry; `RetryableJobError`/`TerminalJobError`/unknown-Exception policy; daemon heartbeat thread renewing active-job lease for long handlers; `lease_lost` fencing; graceful shutdown). No M2–M5 imports.
- `src/operations/__init__.py` — new exports (protocol fns, `StaleLeaseError`, `ClaimedJob`, capability helpers, worker runtime types).
- `tests/test_operations_worker.py` — 60 tests incl. the P0 stale-fencing flow (A claims token A1 → expiry → B claims B1 → all A-side renew/start/success/retryable/terminal rejected; B succeeds), two-worker concurrent claim single-winner, LEASED/RUNNING crash accounting, retry-exhaustion-through-crash, heartbeat/staleness, capability matching, WorkerRuntime behaviors, token-hiding, validation invariants.
- Verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py` = 136 passed; full `pytest tests -q` = 1401 passed / 10 skipped. No production ops DB; no LLM/GPU/network; M2–M5 untouched.

---

## 5c. M6-03 Deliverables Completed

- `src/operations/stages.py` — stage adapter layer (`STAGES_POLICY_VERSION = m6-stages-policy-v1`, `STAGE_EXECUTION_RESULT_SCHEMA_VERSION = m6-stage-execution-result-v1`). Frozen `StageExecutionResult` (`stage`, `canonical_id`, `status` ∈ `EXECUTED|CACHE_HIT`, `input_fingerprint`, `output_fingerprint`, `artifacts`, `metadata`) + `validate_stage_execution_result` identity audit. `fingerprint_artifacts`/`stage_output_fingerprint` (canonical-ordered artifact descriptors + policy version; reuses frozen M4/M5 fingerprints). `required_capabilities_for_stage(stage, media_type=None)`. Adapters: `DiscoverAdapter`, `ArchiveAdapter`, `MediaProcessAdapter`, `KnowledgeExtractAdapter`, `KnowledgeFinalizeAdapter`, `StoreIngestAdapter`. `build_stage_handler_registry(...)` (DI registry feeding `WorkerRuntime`).
- `src/operations/worker.py` — accepts a `StageExecutionResult` handler return, validates identity (stage/canonical/input match, SHA-256 output, JSON-safe), persists full JSON into the successful attempt `metadata_json` (`stage_result`). `src/operations/store.py` — `get_job_result(job_id)` (latest successful attempt's `stage_result` = durable downstream fingerprint handoff). `ClaimedJob` carries the job `metadata` reference.
- `tests/test_operations_stages.py` — 55 tests (result/JSON-safety, deterministic + path-ordered fingerprinting, identity-mismatch rejection, capability map, all six adapters incl. fake success + valid cache-hit + invalid-existing-not-cache + retryable errors, worker runtime integration + persistence + durable fingerprint read, stale-token fencing, at-least-once replay EXECUTED→CACHE_HIT identical fingerprint one artifact, changed-input invalidation, partial-artifact rejection, secrets absent, real C10 video+album offline cache audits).
- Verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py tests/test_operations_stages.py` = 191 passed; full `pytest tests -q` = 1456 passed / 10 skipped. No production ops DB created; production M5 knowledge store untouched (disposable temp DBs only); no network/GPU/LLM/live Douyin; M2–M5 production code unmodified.

---

## 5d. M6-04 Deliverables Completed

- `src/operations/scheduler.py` — reconciliation/level-triggered scheduler (Decision 34). `SCHEDULER_POLICY_VERSION = m6-scheduler-policy-v1`, `SCHEDULER_SCHEMA_VERSION = m6-scheduler-v1`. Frozen `SchedulerCycleResult` (10 counters; JSON-safe). `run_once(now)` in five frozen phases (recover leases → requeue due retryable → process DISCOVER results → reconcile runs → schedule polls); `run_forever(poll_interval_seconds, stop_event)` stdlib-only.
- DISCOVER batch semantics (Decision 35): control asset `(platform, "__discover__")` → `control_{platform}_{source_key}_discover`; each discovered identity independently registered + `ARCHIVE` enqueued under its own RUNNING pipeline run; processed-DISCOVER tracking in `scheduler_state.processed_discover_jobs`. Deterministic poll generation (Decision 36): `poll_slot = epoch_seconds(now)//interval_seconds` in `poll_slot_fingerprint`; overlap suppression; `discovery_control_fingerprint(..., generation=N, ...)` bumps ARCHIVE generation after terminal/cancelled; M2 owns the watermark.
- `ASSET_PIPELINE_GRAPH = (ARCHIVE → MEDIA_PROCESS → KNOWLEDGE_EXTRACT → KNOWLEDGE_FINALIZE → STORE_INGEST)`; `STAGE_MILESTONES` (KNOWLEDGE_EXTRACT has none). Downstream `input_fingerprint` = upstream `get_job_result().output_fingerprint`; `CACHE_HIT` and `EXECUTED` both advance; missing/invalid stage result → `orchestration_invariant_failure` + run FAILED (Decision 37). `required_capabilities_for_stage` reused verbatim; `media_type` for MEDIA_PROCESS routing from ARCHIVE result `metadata` (additive `ArchiveAdapter` gap-fill; M2 untouched).
- Lifecycle = highest milestone, monotonic, never regress; freshness = run/job generation (Decision 38). Run completion derived from `STORE_INGEST` SUCCEEDED + `SEARCHABLE` (Decision 39). Retry requeue + `recover_expired_leases` are phase-0 duties (Decision 40). PC-offline stays `QUEUED`.
- Additive persistence: `scheduler_state` table + `get/set_scheduler_state` + `list_pipeline_runs(canonical_id, status)` (Decision 41); `operations-store-v1` retained (old dev Ops DB requires rebuild). Events are mutation-delimited (Decision 42).
- `tests/test_operations_scheduler.py` — 39 tests incl. synthetic unattended full E2E (DISCOVER→…→SEARCHABLE, zero manual enqueues), restart recovery (new Scheduler instance self-heals), duplicate-cycle no-op, retry before/at due, PC-offline queued, terminal stops downstream, refresh generation without lifecycle regress, real C10 video+album offline chains through real adapters into disposable M5 stores.
- Verification: targeted `pytest tests/test_operations_store.py tests/test_operations_worker.py tests/test_operations_stages.py tests/test_operations_scheduler.py` = 230 passed; full `pytest tests -q` = 1495 passed / 10 skipped. No production ops DB created; production M5 knowledge store untouched (disposable temp DBs only); no network/GPU/LLM/live Douyin; M2–M5 production code unmodified.

---

## 6. NEXT_AGENT_START_HERE

**M6-05 — Crash Recovery / Retry / Observability**

- Objective: harden the scheduler + worker runtime against real crash/interruption scenarios and surface observability for the NAS control plane (the M6-04 scheduler is the durable driver; M6-05 adds the recovery/retry/observability surface the architecture doc's §27 recovery test plan calls for).
- Reuse (all frozen): `src/operations/scheduler.py` (`Scheduler.run_once`/`run_forever`, phase-0 lease recovery + retry requeue, `SchedulerCycleResult`), `src/operations/store.py` (`recover_expired_leases`, `requeue_retryable_job`, `get_job_result`, `list_failed_jobs`, `list_pending_jobs`, `list_events`, `validate_operations_store`), `src/operations/worker.py` (`WorkerRuntime`), `src/operations/stages.py` (adapters).
- Key contracts to exercise/prove: scheduler crash between job SUCCEEDED and lifecycle advance; worker crash after side-effect before commit (at-least-once replay); PC shutdown mid-ASR (lease expiry → retryable attempt accounting); network disconnect; duplicate discovery/enqueue; partial download; corrupted artifact (never cache-hit); LLM runtime unavailable; store ingest failure; restarts after hours/days. Admin surface: list pending/failed, retry, cancel, requeue, worker status, asset pipeline status.
- Constraints: never modify M2–M5 sealed modules; never modify M6-01..M6-04 sealed semantics without an explicit contract-gap STOP; no runtime/model start; SQLite only; no production DB unless the spec explicitly requires it.
- Commit message: per M6-05 spec.

Do not start M6-05 until it is explicitly requested; M6-04 is sealed above.

---

## 7. Hard Constraints (carried forward)

- M2/M3/M4/M5 sealed logic: never rewrite.
- Topology A frozen for v1; Topology B blocked until browser-runtime portability is resolved.
- No secrets in Git / job DB / logs.
- PC offline is normal; GPU jobs wait, never FAIL.
- One GPU-heavy job at a time.
- Stage success is artifact-invariant, not exit-code.
- No nightly full knowledge rerun.