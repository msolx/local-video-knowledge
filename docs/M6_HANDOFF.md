# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Handoff

> **Milestone Status**: `M6-01 = DONE`, `M6-02 = DONE`; `M6-03 = NEXT`; `M6-04..M6-09 = TODO`
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

## 6. NEXT_AGENT_START_HERE

**M6-03 — Pipeline Stage Adapters for M2 → M5**

- Objective: wrap the real M2–M5 stage entry points (table in §4) as stage adapters that plug into the M6-02 `WorkerRuntime` handler registry. Each adapter executes the stage, verifies its artifact invariant (Decision 13 / architecture §17), and raises `RetryableJobError` / `TerminalJobError` accordingly.
- Reuse: `src/operations/worker.py` (`WorkerRuntime`, `RetryableJobError`, `TerminalJobError`, `HandlerResult`), `src/operations/store.py` (claim/start/complete protocol), the M6-01 store primitives, and the M6-02 lease/fencing contract.
- Deliverable (subject to M6-03 spec): stage adapters (`src/operations/` additions) + tests; idempotent artifact semantics are the key contract (resolves the at-least-once residual gap of Decision 22).
- Constraints: never modify M2–M5 sealed modules; never modify M6-01/M6-02 sealed semantics without an explicit contract-gap STOP; no runtime/model start; SQLite only; no production DB unless the spec explicitly requires it; PC-offline stays `QUEUED` (never FAILED).
- Commit message: per M6-03 spec.
- M6-02 sealed the capability-worker and lease protocol: atomic claim, fencing token (`StaleLeaseError`), renewal, start-with-attempt, completion protocols, expired-lease recovery (`LEASED`→QUEUED no attempt; `RUNNING`→closed attempt + backoff), worker heartbeat/staleness (derived, never directly fails jobs), and the generic `WorkerRuntime` with injected handlers + long-job lease-renewal thread.

Do not start M6-04 until M6-03 is sealed.

---

## 7. Hard Constraints (carried forward)

- M2/M3/M4/M5 sealed logic: never rewrite.
- Topology A frozen for v1; Topology B blocked until browser-runtime portability is resolved.
- No secrets in Git / job DB / logs.
- PC offline is normal; GPU jobs wait, never FAIL.
- One GPU-heavy job at a time.
- Stage success is artifact-invariant, not exit-code.
- No nightly full knowledge rerun.