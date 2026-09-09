# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Handoff

> **Milestone Status**: `M6-00 = DONE`; `M6-01 = NEXT`; `M6-02..M6-09 = TODO`
> **Branch**: `feat/m6-automated-knowledge-operations`
> **M2/M3/M4/M5**: COMPLETE / SEALED (do not modify).

---

## 1. Current State

- M6-00 (design only) is complete on `feat/m6-automated-knowledge-operations`, branched from `main` at `e6476be57564d950b7eb75016eed54b1cbf32fec` (M5 final SHA).
- No production code changed. No Docker, no operations DB, no service, no runtime started.
- Docs created:
  - `docs/M6_OPERATIONS_ARCHITECTURE.md`
  - `docs/M6_DECISIONS.md`
  - `docs/M6_TASKS.md`
  - `docs/M6_HANDOFF.md`

---

## 2. Key Facts Carried Forward

- Canonical asset identity remains `(platform, platform_content_id)` — frozen, not re-created.
- Stage graph (frozen): `DISCOVER → ARCHIVE → MEDIA_PROCESS → EVIDENCE_READY → KNOWLEDGE_EXTRACT → KNOWLEDGE_FINALIZE → STORE_INGEST → DONE`.
- Asset lifecycle: `DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE`.
- Job lifecycle: `QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL`.
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

## 5. NEXT_AGENT_START_HERE

**M6-01 · Durable Operations Store + Job State Machine**

- Objective: implement `data/operations/operations.sqlite3` (logical `ops.db_path`) with tables `assets`, `pipeline_runs`, `jobs`, `job_attempts`, `workers`, `leases`/`heartbeats`, `event_log`; freeze asset/job state machines; deterministic `job_id`; durable queue semantics; crash recovery via lease expiry.
- Entry points to reuse: `src/knowledge/store.py` patterns (`PRAGMA user_version`, `_sha256_json`, `compute_store_revision`, atomic replace), `src/storage.py` (`atomic_write_json`, `load_json`, `utc_now`, `sha256_file`).
- Deliverable (subject to M6-01 spec): `src/operations/store.py` (or `src/operations/` package) + `tests/test_operations_store.py`.
- Constraints: do not modify M5 `knowledge_store.sqlite3`; do not modify any M2–M5 sealed module; no runtime/model start; SQLite only.
- Commit message: `feat(m6): add durable operations store and job state machine` (adjust to actual deliverable).

Do not start M6-02 until M6-01 is sealed.

---

## 6. Hard Constraints (carried forward)

- M2/M3/M4/M5 sealed logic: never rewrite.
- Topology A frozen for v1; Topology B blocked until browser-runtime portability is resolved.
- No secrets in Git / job DB / logs.
- PC offline is normal; GPU jobs wait, never FAIL.
- One GPU-heavy job at a time.
- Stage success is artifact-invariant, not exit-code.
- No nightly full knowledge rerun.