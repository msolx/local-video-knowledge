# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Handoff

> **Milestone Status**: `M6-01 = DONE`; `M6-02 = NEXT`; `M6-03..M6-09 = TODO`
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

## 6. NEXT_AGENT_START_HERE

**M6-02 · Worker Runtime + Capability / Lease Protocol**

- Objective: implement the worker runtime + the M6-02 capability/lease protocol on top of the M6-01 store primitives.
- Entry points to reuse: `src/operations/store.py` — `register_worker`, `worker_heartbeat`, `set_job_lease`/`clear_job_lease`, `transition_job_state` (`QUEUED→LEASED` claims, `LEASED→QUEUED` lease-expiry recovery), `begin_attempt`/`finish_attempt`; `src/operations/models.py` enums and `compute_job_id`.
- Deliverable (subject to M6-02 spec): worker runtime + lease/heartbeat protocol (`src/operations/` additions) + tests.
- Constraints: do not modify M5 `knowledge_store.sqlite3`; do not modify any M2–M5 sealed module; do not modify M6-01 sealed store semantics without an explicit contract-gap STOP; no runtime/model start; SQLite only; no production DB unless the spec explicitly requires it.
- Commit message: per M6-02 spec.
- The M6-01 store already persists `lease_owner/leased_at/lease_expires_at/lease_token` and rejects residual lease fields on non-leased states; the concurrent atomic claim algorithm is M6-02's job.

Do not start M6-03 until M6-02 is sealed.

---

## 7. Hard Constraints (carried forward)

- M2/M3/M4/M5 sealed logic: never rewrite.
- Topology A frozen for v1; Topology B blocked until browser-runtime portability is resolved.
- No secrets in Git / job DB / logs.
- PC offline is normal; GPU jobs wait, never FAIL.
- One GPU-heavy job at a time.
- Stage success is artifact-invariant, not exit-code.
- No nightly full knowledge rerun.