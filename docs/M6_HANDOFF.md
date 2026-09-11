# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Handoff

> **Milestone Status**: `M6-00..M6-09 = DONE / COMPLETE / SEALED`. Next Milestone: `M7 · Knowledge Verification`.
> **Branch**: `feat/m6-automated-knowledge-operations`
> **M2/M3/M4/M5/M6**: COMPLETE / SEALED (do not modify).

---

## 1. Current State

- M6-00 (design), M6-01 (durable operations store + job state machine), M6-02 (worker + lease protocol), M6-03 (stage adapters), M6-04 (scheduler), M6-05 (recovery/observability), M6-06 (Windows worker host + autostart), and M6-07 (NAS control plane + remote worker transport) are complete on `feat/m6-automated-knowledge-operations`, branched from `main` at `e6476be57564d950b7eb75016eed54b1cbf32fec` (M5 final SHA).
- M6-06 delivered `src/operations/windows_worker.py` + `tests/test_operations_windows_worker.py` (54 tests), PowerShell autostart artifacts under `scripts/windows/`, `config/examples/m6_windows_worker.example.json`, and `docs/M6_WINDOWS_WORKER_RUNBOOK.md`. No production operations DB created; no production scheduled task registered; no M2–M5 code modified; no runtime started.
- M6-07 delivered `src/operations/{transport,http_transport,control_plane}.py` + `tests/test_operations_{http_transport,control_plane}.py` (21 + 38 tests), additive `allowed_stages`/idempotency primitives in `store.py` and transport injection in `worker.py`/`windows_worker.py`, Docker packaging under `docker/control-plane/`, config examples, and `docs/M6_NAS_CONTROL_PLANE_RUNBOOK.md`. Real localhost HTTP distributed E2E (DISCOVER→…→STORE_INGEST→SEARCHABLE) passed with execution-host assertions; PC-offline, NAS-restart, network-partition, and fencing tests green. No production NAS deployment; no Windows scheduled task; no live Douyin/network/GPU/LLM; no production Ops DB.
- Docs:
  - `docs/M6_OPERATIONS_ARCHITECTURE.md`
  - `docs/M6_DECISIONS.md` (now 65 decisions; M6-07 added Decisions 56–65)
  - `docs/M6_TASKS.md` (M6-07 DONE)
  - `docs/M6_HANDOFF.md`
  - `docs/M6_WINDOWS_WORKER_RUNBOOK.md`
  - `docs/M6_NAS_CONTROL_PLANE_RUNBOOK.md` (new)

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

## 5e. M6-05 Deliverables Completed

- `src/operations/observability.py` — pure read-only health projection. Frozen health vocabulary (`HEALTHY/RUNNING/WAITING_FOR_WORKER/WAITING_RETRY/SUCCEEDED/FAILED_TERMINAL/CANCELLED/STALLED/INVARIANT_ERROR`) + `MANUAL_ATTENTION_HEALTH = {STALLED, INVARIANT_ERROR, FAILED_TERMINAL}`. Frozen `AssetPipelineStatus`, `WorkerStatus`, `OperationsSummary`. Functions: `get_asset_pipeline_status`, `list_asset_pipeline_statuses`, `get_worker_status`, `list_worker_statuses`, `compute_operations_summary` (incl. optional `validate_operations_store`), `get_asset_timeline`. Explicit field allowlist — can never emit a `lease_token`. Never writes.
- `src/operations/admin.py` — explicit recovery + admin mutations (never read-only). `RecoveryResult` (`m6-recovery-result-v1`), `AdminRetryResult`, `AdminCancelResult`. `run_recovery_pass`/`startup_recovery` compose frozen primitives (validate → `recover_expired_leases` → scheduler phase-2 requeue → phase-4 reconcile; optional poll pass). `admin_retry_job` (backoff respected; terminal/exhausted requires `force` + valid new input fingerprint → new generation), `admin_cancel_job` (additive `CANCELLED`), `admin_requeue_asset`. Secrets never accepted/echoed.
- `src/operations/cli.py` — thin secret-scrubbed presentation layer. Commands: `status`, `asset`, `jobs`, `failed`, `workers`, `timeline`, `retry`, `cancel`, `recover`. Every subcommand accepts `--db` (env `OPERATIONS_DB_PATH`) + `--json`. `_secret_free` strips `lease_token`/cookies/API keys before output. No network/GPU/LLM started.
- `docs/M6_RECOVERY_RUNBOOK.md` — recovery matrix (16 scenarios with explicit AUTO_RECOVER/MANUAL_ATTENTION), startup recovery, observability vocabulary, admin ops, M4 incident destructive-root guard rule, secrets & event-spam policy.
- Tests:
  - `tests/test_operations_recovery.py` (32 tests) — fault matrix: scheduler crash windows A/B, worker crash before start / during run (recovery + exhaustion), network disconnect lease expiry, handler side-effect replay (at-least-once CACHE_HIT), admin retry/cancel semantics, duplicate discovery/enqueue suppression, PC offline→online, LLM unavailable backoff, DB lock retryable, hours/days-late requeue, partial artifact never cache-hit, corrupt artifact terminal, real C10 video+album offline recovery → SEARCHABLE into **disposable** M5 stores (copied processed roots), and M4 incident destructive-root guard regression tests (real `data/processed` rejected; disposable tmp roots allowed).
  - `tests/test_operations_observability.py` — health classification, timeline, worker staleness, summary, real C10 status fixtures (read-only, disposable DBs).
  - `tests/test_operations_cli.py` — all commands, `--json`, secret redaction, error paths.
- Verification: targeted M6 suite (`store/worker/stages/scheduler/recovery/observability/cli`) = **302 passed**; incident acceptance (`test_m4_acceptance` + `test_m4_incident_recovery`) = **54 passed**; full `pytest tests -q` = **1588 passed / 10 skipped / 0 failed**. Live C10 unchanged (video 62 KU, album 6 KU); production M5 store unchanged (2 assets / 68 KU, revision `7b604b33…`). No production ops DB created; no network/GPU/LLM/live Douyin; M2–M5 production code unmodified.

---

## 5f. M6-06 Deliverables Completed

- `src/operations/windows_worker.py` — Windows PC Worker Host wrapping the frozen `WorkerRuntime`. `m6-windows-worker-config-v1` config contract; `m6-windows-worker-preflight-v1` preflight result; frozen exit codes (0 normal / 2 config / 3 preflight / 4 already-running / 5 fatal); `WINDOWS_PC_TARGET_CAPABILITIES` (collector, downloader, cpu_media, gpu_asr, gpu_vlm, llm_extraction — no store_ingest); transport placeholder `local_sqlite_test` (runnable) vs `http` (fails closed until M6-07/08).
  - `WorkerHostConfig` — worker_id, capabilities, roots, `operations_db_path` (local-dev only), knowledge_store_path, intervals, rotating log settings, `single_instance_lock_path`, transport, `startup_delay_seconds`, `runtime_references` (browser exe/profile, node, ASR/VLM/LLM runtimes + model roots). Secrets never in config.
  - `run_capability_preflight` — availability/config checks only (never launches browser/Douyin/Whisper/llama.cpp); missing required prerequisite → start fails (exit 3), no silent capability drop; `llm_runtime`/`llm_model_root` existence checks only (llama.cpp policy).
  - `SingleInstanceLock` — OS file lock (`msvcrt`/`fcntl`), stale file re-locked (never fatal), second instance exit 4.
  - `configure_worker_logging` — `RotatingFileHandler` (10 MB × 5, default `logs/operations/windows-worker.log`); `redact_config`/`redact_worker_registration`/`redact_message` strip secret-shaped keys.
  - `WindowsWorkerHost.start()` — SMB/UNC + transport guards → fail-closed missing `operations_db_path` → preflight → lock → build `WorkerRuntime` → register (redacted) → heartbeat → SIGINT/SIGTERM graceful stop → `run_forever` → exit code; host never calls `startup_recovery`.
  - CLI: `python -m src.operations.windows_worker {run|preflight|print-config} --config <path> [--json]`.
- `src/operations/__init__.py` — lazy PEP 562 `__getattr__` exports (keeps `python -m` stderr clean).
- `config/examples/m6_windows_worker.example.json` (tracked, no secrets) + gitignored `config/local/`.
- `scripts/windows/{run_m6_worker,install_m6_worker_task,uninstall_m6_worker_task,status_m6_worker_task}.ps1` — run wrapper (exact venv Python + exit-code propagation), dry-run-by-default Task Scheduler installer (`-Apply` deferred to M6-08), idempotent uninstall, read-only status.
- `docs/M6_WINDOWS_WORKER_RUNBOOK.md` — full runbook.
- `tests/test_operations_windows_worker.py` — 54 tests (config/preflight/lock/logging/host lifecycle/exit codes/PowerShell artifacts/real install dry-run 0-mutation).
- Verification: targeted M6 suite = **356 passed**; incident acceptance = **54 passed**; full regression = **1642 passed / 10 skipped / 0 failed**. PowerShell syntax validated (0 errors). Live C10 unchanged (video 62 / album 6); production M5 store unchanged (2 assets / 68 KU, revision `7b604b33…`). No production ops DB created; **no production scheduled task registered**; no network/GPU/LLM/live Douyin; M2–M5 production code unmodified.

---

## 6. NEXT_AGENT_START_HERE

**Milestone M7 — Knowledge Verification**

- Objective: External truth verification, claim grounding against trusted knowledge sources, confidence calibration, hallucination detection, and automated verification question resolution.
- Baseline: Milestone M6 sealed under tag `m6-automated-knowledge-operations-complete` on branch `feat/m6-automated-knowledge-operations`.
- Production Architecture: Topology A (NAS Control Plane + Windows GPU Worker) fully operational in production.
- Do not start M7 until it is explicitly requested; M6 is sealed above.

---

## 7. Hard Constraints (carried forward)

- M2/M3/M4/M5 sealed logic: never rewrite.
- Topology A frozen for v1; Topology B blocked until browser-runtime portability is resolved.
- No secrets in Git / job DB / logs.
- PC offline is normal; GPU jobs wait, never FAIL.
- One GPU-heavy job at a time.
- Stage success is artifact-invariant, not exit-code.
- No nightly full knowledge rerun.