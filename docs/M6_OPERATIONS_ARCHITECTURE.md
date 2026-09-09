# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Operations Architecture

> **Status**: DESIGN / APPROVED
> **Scope**: M6-00 only — architecture & orchestration contract. No production code, no daemon, no Docker, no runtime start, no operations DB creation.
> **Prerequisite**: M2 / M3 / M4 / M5 all COMPLETE / SEALED.

---

## 1. Product Goal (frozen)

The user opens Douyin on their phone and taps "favorite". From that moment the system must, without any manual step:

```
Collection detection
  → Archive
  → Media processing
  → Evidence
  → Knowledge extraction
  → M5 Store ingest
Final state: SEARCHABLE
```

The user's only responsibility is **favoriting content**. Everything downstream — download, transcode, ASR, OCR, VLM, knowledge extraction, and Knowledge Store ingestion — is automated.

---

## 2. Current M2 → M5 Callable Pipeline Audit (real code, not reports)

This is the authoritative list of stage entry points that M6 stage adapters must wrap. No assumptions were made from old reports; each entry was read from current source.

### M2 Collector (collection detection) — `src/collector/`

- CLI entry: `python -m src.collector.cli douyin {probe|sync|backfill} [--config/-c] [--json]` (`src/collector/cli.py:main`).
- `sync` = incremental collection sync down to watermark; `backfill` = historical; `probe` = dependency/auth diagnostics.
- `CollectorService` (`src/collector/service.py`) wraps a `DouyinCollector` + `DouyinCollectorConfig` (`src/collector/douyin/config.py`: `aid=6383`, `device_platform='webapp'`, `channel='channel_pc_web'`).
- `CollectorConfig` (`src/collector/config.py`): `runtime_root='./runtime'`, `raw_archive_root='./data/raw'`, `canonical_root='./data/canonical'`, `database_path='./data/metadata.db'`, `headless=True`, `profile_path`, `lock_timeout_sec=3600`. Forbidden secret keys are rejected at config load.
- Browser runtime: `DouyinBrowserRuntimeProvider` (`src/collector/douyin/browser_runtime.py`) — Node.js sidecar (`src/collector/douyin/browser/runtime_server.js`) + puppeteer-core + persistent Chromium profile. Page-context SDK signing runs inside the browser page. Credential snapshot API (`douyin.credentials.snapshot`, D02) provides the authenticated session.

### M2 Downloader (archive) — `src/downloader/`

- Worker CLI: `python -m src.downloader.worker {--once|--drain|--serve}` (`src/downloader/worker.py:main`) with `--collector-db data/metadata.db`, `--job-db data/downloader_state.sqlite3`, `--archive-root archive`, `--sandbox-root`, `--poll-interval`, `--auth-recheck-interval`.
- `DownloaderWorkerService` (DY-D10): outbox consumer over Collector C09 → `DownloaderJobStore` → sequential F2 execution. Single active worker enforced by `DownloaderServiceLock`.
- `SafeDouyinDownloader` (DY-D01): 8-stage state machine `RECEIVED→PREFLIGHT→SANDBOX_READY→DOWNLOADING→NORMALIZING→VALIDATING→PROMOTING→SUCCESS`; sandbox isolation + atomic promotion to formal archive; secret scrubbing.
- Backend: `F2InProcessBackendAdapter` (Python F2 port). Requires authenticated Douyin session (cookies) from the collector browser profile.

### M3 Media Processing & Evidence — `src/pipeline.py` + `src/intake/` + `src/media_adapter/` + `src/visual/` + `src/backends/`

- CLI entry: `python main.py --canonical-id <cid> [--stop-after {source|audio|asr|visual|evidence|knowledge|publish_media|all}] [--force]` (`main.py` → `src/pipeline.py:run`).
- `process_canonical_asset` (`src/pipeline.py:421`): video path, default boundary `asr` (M3-02), then `write_evidence_manifest` + (optional) `write_evidence_chunks` (`src/provenance.py:455`, `src/chunking/service.py:120`).
- `process_canonical_album` (`src/pipeline.py:452`): image-album path (visual/OCR + optional VLM), writes evidence manifest/chunks.
- Intake: `prepare_assets` / `MediaAsset` / `probe_media(ffprobe, path)` (`src/intake/media_probe.py:38`); `CanonicalMediaAssetAdapter` (`src/media_adapter/adapter.py`) loads formal archives from `asset_manifest.json`.
- ASR: `transcribe(audio_path, asr_config, temporary_output)` (`src/backends/asr.py:72`), backend `faster_whisper` (CUDA) via `src/backends/faster_whisper_worker.py`; NO_AUDIO handled.
- Visual: `build_visual_evidence(...)` (`src/visual/service.py:263`), `build_album_visual_evidence(...)` (`src/visual/album.py:229`), OCR via PaddleOCR GPU worker (`scripts/ocr_gpu_worker.py`), VLM via LM Studio (`qwen3-vl-8b-instruct`).
- Legacy LLM knowledge path (pre-M4): `build_knowledge(...)` (`src/knowledge/service.py:189`) — M4 supersedes this for production knowledge units.

### M4 Knowledge Extraction / Merge / Enrichment / Finalize — `src/knowledge/`

All are pure Python functions taking a `processed_dir` and writing deterministic, cache-fingerprinted artifacts:

| Stage | Entry | Reads | Writes |
|---|---|---|---|
| Extract | `extract_knowledge_candidates(processed_dir, config, backend)` (`extractor.py:1097`) | `evidence_manifest.json`, `evidence_chunks.json` | `knowledge/raw_extractions/<chunk>.json`, `knowledge/knowledge_candidates.json` |
| Merge | `merge_knowledge_candidates(processed_dir, config)` (`merger.py:261`) | `knowledge_candidates.json` | `merged_knowledge_candidates.json` |
| Enrich | `enrich_knowledge_candidates(processed_dir, config, backend)` (`enrichment.py:846`) | `merged_knowledge_candidates.json` | `enriched_knowledge_candidates.json` |
| Finalize | `finalize_knowledge_document(processed_dir, config)` (`render.py:368`) | `enriched_knowledge_candidates.json`, `knowledge_candidates.json` | `knowledge_units.json`, `knowledge.md`, `knowledge_finalization.json` |

- LLM backends: `MockLLMBackend` / `OpenAICompatibleBackend` (LM Studio `qwen/qwen3-8b` on `127.0.0.1:12345/v1` per M4-02 smoke; config path `config/config.json` `llm.lm_studio` points at `qwen3.6-27b-knowledge` for legacy path).
- All four stages are idempotent via deterministic cache fingerprints (`cache_hit` flag) and never rewrite inputs.

### M5 Store / Retrieval — `src/knowledge/store.py`, `retrieval.py`, `fts.py`

| Operation | Entry |
|---|---|
| Ingest (incremental, production path) | `ingest_knowledge_document(db_path, knowledge_units_path)` (`store.py:622`) → `IngestResult(status='inserted'|'replaced'|'unchanged', ...)` |
| Full rebuild (recovery/admin) | `rebuild_store(db_path, processed_root)` (`store.py:1060`) — temp DB → validate → atomic replace |
| Validate | `validate_store(db_path)` |
| Discovery | `discover_final_artifacts(processed_root)` — only `data/processed/*/knowledge/knowledge_units.json` |
| Store creation | `create_store(db_path)` / `open_store(db_path)` |
| Retrieval | `retrieve(db_path, query)` (`retrieval.py`) — FTS5 trigram + short-query substring fallback; `RetrievalQuery/RetrievalHit/RetrievalResult` |
| Evaluate | `evaluate_suite(db_path, suite)` (`evaluation.py`) against `evaluation/m5/c10_golden_queries.json` |

### Portability summary

| Stage | NAS-ready? | Notes |
|---|---|---|
| M2 Collector | **PC-only-for-now** | `browser_runtime.py` hardcodes Windows Chrome path `C:\Program Files\Google\Chrome\Application\chrome.exe`, `G:\antigravity-cli\dy\runtime\chrome-profile`, G: NODE_PATH candidates, `ctypes.windll` + `taskkill` termination. Cannot prove the existing Windows Chrome profile works on Linux/Docker → **deployment constraint / unresolved portability issue** (see §32). |
| M2 Downloader | **portable-with-small-change** | Pure Python F2 backend; auth depends on the collector browser session (so it must run where the authenticated browser lives in v1). |
| M3 Media/Evidence | **PC-only-for-now / portable-with-small-change** | ffmpeg/ffprobe are cross-platform but path-configured; ASR=whisper CUDA GPU; OCR=PaddleOCR GPU worker; VLM=LM Studio; multiple Windows absolute paths in `config/config.json`. Runs where GPU + model runtimes live. |
| M4 Extraction | **portable-with-small-change** | Pure Python + OpenAI-compatible HTTP LLM endpoint; only the LLM endpoint needs reachability. |
| M5 Store/Retrieval | **NAS-ready** | stdlib `sqlite3` + FTS5, no GPU, no browser, no secrets. |

---

## 3. Deployment Topology (M6 v1)

Because M2 collector's browser runtime cannot be proven portable (§2 audit), the v1 topology keeps Douyin browser/collector and downloader on the Windows PC.

**Topology A (recommended, v1):**

```
NAS (always-on)
  ├── Control Plane: scheduler, job DB, state tracking, heartbeat registry
  ├── Storage owner: archive mirror target (logical), processed artifacts, knowledge artifacts, operations DB, Knowledge Store
  └── Knowledge Store ownership (SQLite) + retrieval/query surface

Windows RTX-4090 PC (worker)
  ├── Douyin browser/collector (authenticated Chrome profile + page-context SDK signing)
  ├── Downloader (auth-dependent F2)
  ├── GPU media: ffmpeg, ASR (whisper CUDA), OCR (Paddle GPU), VLM
  └── M4 LLM extraction (qwen3-8b / llama.cpp runtime)
```

**Topology B (second-best):** move collector/downloader to NAS by first proving/porting the browser runtime (configurable Chrome binary, env-resolved profile + NODE_PATH, Linux signal handling). Not the v1 freeze.

Only **Topology A** is frozen for M6 v1. A is selected because the M2 browser runtime portability cannot currently be demonstrated.

---

## 4. Control Plane vs Execution Workers

- **Control Plane** (NAS): scheduler, job/lease state, event log, retry policy, heartbeat/capability registry, storage ownership, Knowledge Store ownership. Never executes GPU/media/LLM work.
- **Execution Workers** (capability-based, PC-first): one or more processes that claim jobs by capability, report heartbeat + capabilities, and write results to storage. Machine name (`Sean-PC`) is metadata only — scheduling is capability-based.

---

## 5. Pipeline Stages (frozen names, adjusted to real M2→M5 APIs)

```
DISCOVER
  ├── input:  collection cursor/checkpoint + one new favorite (platform, platform_content_id)
  ├── output: asset record (DISCOVERED) + enqueue ARCHIVE job
  ├── success invariant: asset row exists with stable identity; job row exists with deterministic job_id
  ├── failure condition: collector probe/auth error → retryable; terminal only on config/platform error
  ├── idempotency key: (platform, platform_content_id, asset_policy_version)
  └── retry safety: re-poll is naturally idempotent (cursor/watermark); duplicate enqueue collapses to one job

ARCHIVE
  ├── input:  (platform, platform_content_id) + auth session
  ├── output: formal asset in archive/ + asset_manifest.json + media files (hash-verified)
  ├── success invariant: archive manifest present + expected media exist + hashes match (CanonicalMediaAssetAdapter.load_from_dir passes)
  ├── failure condition: download auth/session failure → retryable; corrupt/unsupported media → terminal
  ├── idempotency key: (platform, platform_content_id, archive_policy_version)
  └── retry safety: SafeDouyinDownloader is already idempotent (PROMOTED = bypass re-download); partial download stays in sandbox

MEDIA_PROCESS
  ├── input:  formal asset dir
  ├── output: data/processed/<canonical_id>/metadata.json, media.json, transcript/visual artifacts
  ├── success invariant: M3 stage completion contract (see §21) — evidence_manifest.json schema-valid
  ├── failure condition: ffmpeg/ASR/OCR/VLM error → retryable (GPU/offline); NO_AUDIO is success-not-failure
  ├── idempotency key: (canonical_id, media_policy_version, source hash)
  └── retry safety: M3 processing.json per-stage state machine already supports resume/skip

EVIDENCE_READY
  ├── input:  processed/<canonical_id>/ evidence manifest + chunks
  ├── output: evidence_manifest.json + evidence_chunks.json validated
  ├── success invariant: evidence_manifest schema-valid + evidence_chunks schema-valid
  └── idempotency key: (canonical_id, evidence_policy_version)

KNOWLEDGE_EXTRACT
  ├── input:  evidence_manifest.json + evidence_chunks.json
  ├── output: knowledge/knowledge_candidates.json
  ├── success invariant: M4 extract artifact schema-valid
  ├── failure condition: LLM endpoint unavailable → retryable
  ├── idempotency key: (canonical_id, extract_policy_version, extraction_config_fingerprint)
  └── retry safety: M4 cache fingerprint → identical re-run is cache hit

KNOWLEDGE_FINALIZE
  ├── input:  knowledge_candidates.json (chains merge → enrich → finalize)
  ├── output: knowledge/knowledge_units.json (+ .md)
  ├── success invariant: knowledge_units.json schema knowledge-units-v1 valid
  ├── failure condition: schema/config mismatch → retryable via policy bump; invalid unit data → terminal
  ├── idempotency key: (canonical_id, finalize_policy_version, enrichment fingerprint)
  └── retry safety: all four M4 sub-stages are cache-fingerprinted

STORE_INGEST
  ├── input:  knowledge/knowledge_units.json
  ├── output: M5 Knowledge Store row set updated
  ├── success invariant: ingest_knowledge_document returns inserted|replaced|unchanged AND validate_store valid
  ├── failure condition: store locked/IO → retryable; schema mismatch → terminal
  ├── idempotency key: (canonical_id, store_policy_version, source artifact fingerprint)
  └── retry safety: M5 ingest is idempotent by source artifact fingerprint

DONE (asset SEARCHABLE)
```

---

## 6. Asset Lifecycle vs Job Lifecycle (never one status field)

**Asset lifecycle** (what happened to the content):

```
DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE
```

**Job lifecycle** (what one execution is doing):

```
QUEUED → LEASED → RUNNING → SUCCEEDED
                          ├── FAILED_RETRYABLE
                          └── FAILED_TERMINAL
```

These are two distinct state machines. An asset's current stage is a *derived* summary of its stage jobs, not a single mutable status.

---

## 7. Identity & Idempotency (frozen)

- **Asset identity** = the frozen canonical `(platform, platform_content_id)`. M6 does not create a new asset identity.
- **Job identity** (deterministic, per orchestration job):

```
job_id = sha256(
  f"{platform}|{platform_content_id}|{stage_name}"
  f"|{input_artifact_fingerprint}|{policy_version}"
)[:16]
```

- `input_artifact_fingerprint` = the fingerprint of the actual input artifact for that stage (e.g. merged candidate artifact fingerprint for finalize). For DISCOVER, `input fingerprint` is a constant (detection has no artifact) — so discovery jobs for the same asset+policy collapse to one.
- **Idempotency rule**: if a job for the same `(asset, stage, input_fingerprint, policy_version)` already SUCCEEDED, a new scheduler tick must not create a duplicate job — it is SKIP/CACHE HIT. Only policy/version or input change invalidates downstream stages.
- **No "full nightly rerun of all knowledge"**: invalidation is strictly downstream-of-the-changed-input.

---

## 8. Operations Job Store

- **Separate DB**: `data/operations/operations.sqlite3` (logical path; see §19 for logical vs physical). Never merged into M5 `knowledge_store.sqlite3` — different lifecycle, owner, and retention.
- **Why separate**: Knowledge Store is a derived retrieval artifact owned by M5; Operations DB is control-plane state with its own crash/recovery/retention needs.
- M6-00 **does not create** this DB. Only designed here.

### Job tables (not over-normalized)

| Table | Purpose |
|---|---|
| `assets` | asset identity, platform, platform_content_id, current lifecycle stage, canonical_id, media type, first/last seen |
| `pipeline_runs` | one row per (asset, stage) execution lineage — input/output fingerprints, policy versions |
| `jobs` | one row per stage job: job_id, asset ref, stage, state, priority, enqueue time |
| `job_attempts` | attempt_count, max_attempts, next_attempt_at, last_error, worker_id (retry history) |
| `workers` | worker identity, capabilities, machine metadata |
| `leases` / `heartbeats` | lease_owner, leased_at, lease_expires_at, heartbeat_at |
| `event_log` | structured append-only events (see §26) |

Required properties: crash recovery (durable queue + lease expiry), retry history, current state, diagnostic visibility.

---

## 9. Lease Model (frozen)

Workers claim jobs via **leases** — never by "process still running".

```
lease_owner       = worker_id
leased_at         = UTC timestamp
lease_expires_at  = leased_at + lease_ttl
```

- Worker refreshes its lease via periodic heartbeat while the job is still validly progressing.
- On lease expiry (worker crash/PC power-off), the job is **safely requeued** (reset to QUEUED, attempt recorded).
- Scheduler and PC worker cannot both run the same job because only one worker can hold a valid unexpired lease.

---

## 10. Retry Policy (frozen)

| Class | Examples | Behavior |
|---|---|---|
| `FAILED_RETRYABLE` | PC offline, temporary file lock, LLM endpoint temporarily unavailable, network blip | `attempt_count` increments; `next_attempt_at = now + backoff(min(2^attempt, cap))`; requeue on lease expiry |
| `FAILED_TERMINAL` | corrupt source artifact, schema validation failure, unsupported media type, config/policy error | recorded; no automatic retry; surfaced for admin |

- `max_attempts` (configurable, e.g. 5) per job stage; **no infinite high-speed retry**.
- Backoff persists across restarts (stored in `job_attempts.next_attempt_at`).

---

## 11. PC Offline Is Normal, Not Failure (frozen)

- The RTX-4090 PC powering off is a **normal state**, not an error.
- GPU-required jobs that cannot run stay `QUEUED` (waiting for capability), **never** `FAILED`.
- When the PC boots and its worker heartbeats with GPU capabilities, the scheduler assigns waiting GPU jobs.
- This is the core of the unattended experience.

---

## 12. Worker Capabilities (frozen)

Heartbeat declares capabilities (not hardcoded machine names):

```
collector
downloader
cpu_media
gpu_asr
gpu_vlm
llm_extraction
store_ingest
```

- Scheduler dispatches by capability ∩ resource availability.
- Machine name (`Sean-PC`) is metadata only.
- One worker process may declare multiple capabilities.

---

## 13. LLM Runtime Policy (design only — nothing is started in M6-00)

- Current preference (from M5 operator note): local runtime first → `G:\llama.cpp`; models on `D:\LMmodel`; LM Studio is **not** the default production runtime.
- M6 LLM worker design detects a runtime (probe health endpoint / process), starts/stops it, health-checks before extracting.
- The concrete runtime contract (which runtime, how started) is finalized in the worker implementation milestone. M6-00 does not start any runtime.

---

## 14. GPU Resource Ownership (frozen v1 rule)

- RTX 4090 may be needed by ASR, VLM, and M4 LLM simultaneously.
- **v1 rule: one GPU-heavy job at a time** per GPU worker (capability semaphore / single-slot lease on the GPU capability).
- No complex GPU scheduler in v1. Prevents Whisper + VLM + 27B LLM from fighting over 24 GB VRAM.

---

## 15. Storage Ownership

- **Long-term owner**: NAS — archive/media, processed artifacts, knowledge artifacts, operations DB, Knowledge Store.
- **Development-time reality**: paths are on Windows `G:\local_pc_project\...` until NAS is deployed.
- **Rule**: business code must reference **logical paths** (config keys), not hardcoded Windows/NAS pairs. A deployment config maps logical → physical per environment. M6-00 defines the logical path contract; it does not ship a NAS config.

---

## 16. File Transfer Contract (NAS ↔ PC)

For M6 v1, evaluate in order and freeze the simplest reliable option:

- **A. SMB / shared filesystem** (recommended candidate): both sides mount the same logical tree; workers read/write via normal paths.
- **B. sync / local staging**: each worker stages inputs locally, syncs outputs back.
- **C. simple HTTP worker API**: worker serves a small fetch/put surface.

Hard requirements for any choice:
- **Partial file protection**: writers write to `.tmp` then atomic rename; readers only read final names.
- **Hash verification**: transfers verify SHA-256 before promotion (already the norm in M2/M3/M5).
- **No worker reads an in-flight download**: stage inputs are only offered after the producer stage reports success (invariant-gated, §21).

v1 freeze during implementation: **A (shared filesystem)** unless a hard blocker surfaces; B and C are fallbacks.

---

## 17. Stage Completion = Artifact Invariant, Never Exit Code

A stage is "successful" only when its artifact invariant holds:

| Stage | Success invariant |
|---|---|
| ARCHIVE | `asset_manifest.json` schema-valid + expected media exist + SHA-256 match |
| EVIDENCE_READY | `evidence_manifest.json` schema-valid (+ `evidence_chunks.json` schema-valid) |
| KNOWLEDGE_EXTRACT | `knowledge_candidates.json` schema-valid |
| KNOWLEDGE_FINALIZE | `knowledge_units.json` schema `knowledge-units-v1` valid |
| STORE_INGEST | M5 ingest returns `inserted|replaced|unchanged` **and** `validate_store` is valid |

---

## 18. M5 Incremental Ingest (frozen)

- **Normal production path**: new/updated asset → `ingest_knowledge_document(db_path, knowledge_units_path)`.
- **No per-favorite full rebuild**.
- M5 `rebuild_store` remains a **recovery/admin operation** only (schema changes, corruption recovery).

---

## 19. Trigger Model

- **A. Scheduled collection polling**: configurable interval (not frozen to a number; a config key) — run `collector douyin sync` style detection.
- **B. Worker heartbeat**: capability + availability refresh.
- **C. Downstream stage trigger**: when stage N succeeds, scheduler automatically enqueues stage N+1 for that asset.
- No human-run scripts in the production flow.

---

## 20. Collection Polling Design

- Cursor/checkpoint: last successful poll watermark (collector already maintains watermark/duplicate suppression).
- New-item detection + duplicate suppression: reuse M2 collection semantics (`DouyinCollector.sync` down to watermark). **Do not reimplement the list-collection parser.**
- Near-real-time polling is the design target; true realtime is not promised.

---

## 21. Delete / Uncollect Semantics (frozen)

- User removing a Douyin favorite is **not** a deletion request.
- PKP is a personal knowledge archive: uncollection must **not** auto-delete archive, evidence, knowledge, or Knowledge Store entries.
- A separate future deletion workflow will define explicit deletion. M6-00 only freezes the "uncollect ≠ delete" rule.

---

## 22. Observability (v1: SQLite event log + status surface)

Structured event log must answer, per asset:

- Which stage is it at now?
- Why is it stuck (last error)?
- How many retries?
- Which worker executed it?
- When is the next retry?

v1 = `event_log` table + a CLI/status endpoint (`list pending|failed`, `status <asset>`). No Grafana/Prometheus in v1.

---

## 23. Admin Operations Contract (frozen contract, no UI in M6-00)

- `list pending`
- `list failed`
- `retry job`
- `cancel job`
- `requeue`
- `worker status`
- `asset pipeline status`

---

## 24. Startup / Autostart (deployment contract only)

- **NAS**: Docker Compose / service auto-start for control plane (+ storage mounts).
- **Windows PC**: boot / login autostart of the worker process.
- M6-00 only documents this contract; no service is installed now.

---

## 25. Security / Secrets Boundary (frozen)

- **Never write** into Git, job DB plaintext payloads, or logs: Douyin cookies, browser profile, API secrets, LLM credentials.
- Config loading already forbids secret keys (`CollectorConfig.FORBIDDEN_SECRET_KEYS`).
- If NAS needs Douyin auth (Topology B later), credentials are handled by a dedicated credential store/secret boundary — never embedded in job payloads.
- Job DB stores references/hints (e.g. "session present", last error message class), never cookie material.

---

## 26. Network Failure (NAS ↔ PC)

- Disconnection is a normal condition.
- Requirements: no job loss, no duplicate submission, resume on reconnection.
- Mechanism: **durable queue + leases + idempotency** — no strong-consistency distributed transactions.

---

## 27. Failure Recovery Test Plan (documented for future milestones)

Scenarios to cover in the M6 implementation milestones (crash-injection style, mostly offline):

- scheduler crash
- worker crash
- PC shutdown during ASR
- network disconnect
- duplicate discovery
- duplicate enqueue
- lease expiry
- partial download
- corrupted artifact
- LLM runtime unavailable
- Store ingest failure
- restart after several hours
- restart after several days

**Acceptance property**: after any of these, the pipeline **continues** — it never requires manual restart from scratch.

---

## 28. M2 Portability Audit (this milestone's required analysis)

| Component | Classification | Evidence |
|---|---|---|
| Douyin collection (browser runtime) | **PC-only-for-now** | `src/collector/douyin/browser_runtime.py` hardcodes `C:\Program Files\Google\Chrome\Application\chrome.exe`, `G:\antigravity-cli\dy\runtime\chrome-profile`, G:-drive NODE_PATH candidates, `ctypes.windll` + `taskkill /F`. Existing Windows Chrome profile portability to Linux/Docker is **unproven** → marked deployment constraint / unresolved portability issue. |
| Douyin downloader | **portable-with-small-change** | Pure Python F2 backend; but requires the authenticated session, which lives in the PC browser profile → v1 stays on PC. |
| M3 media processing | **PC-only-for-now (GPU + Windows paths)** | ffmpeg/whisper-cuda/Paddle-GPU/LM-Studio VLM; absolute Windows paths in `config/config.json`; runs where GPU lives. |
| M4 extraction | **portable-with-small-change** | Pure Python; only needs the OpenAI-compatible LLM endpoint reachable. |
| M5 store/retrieval | **NAS-ready** | stdlib sqlite3 + FTS5, no GPU/browser/secrets. |

**Do not assume "containerizing will just work"** for the browser runtime — the profile bootstrapping, Chrome binary path, NODE_PATH, and Windows process control must be reworked before any NAS deployment of collection.

---

## 29. M6 Task Tree (frozen after audit)

```
M6-00  Operations Architecture & Contract            ← this milestone
M6-01  Durable Operations Store + Job State Machine  (operations.sqlite3, tables, states)
M6-02  Local Worker Runtime + Capability/Lease Protocol
M6-03  Pipeline Stage Adapters for M2→M5 (wrap §2 entry points, invariant gates)
M6-04  Scheduler + Automatic Downstream Orchestration (polling, triggers, dispatch)
M6-05  Crash Recovery / Retry / Observability
M6-06  Windows PC Worker Autostart
M6-07  NAS Docker Control Plane Deployment
M6-08  Real Douyin Favorite → Searchable Knowledge E2E
M6-09  Final Acceptance
```

This skeleton is grounded in the real dependency order: store first (M6-01), then worker protocol (M6-02), then adapters (M6-03), then scheduler (M6-04), then resilience (M6-05), then deployment/autostart (M6-06/07), then E2E + acceptance (M6-08/09). Adjustments are allowed if an implementation milestone surfaces a dependency not visible here.

---

## 30. Deferred (out of M6 scope)

Not part of automated Operations:

- knowledge truth verification
- dense retrieval
- hybrid retrieval
- reranker
- RAG answer generation
- Obsidian publishing
- web end-user UI
- global entity graph

---

## 31. Logical vs Physical Paths (contract)

| Logical key | Development (Windows) | Target (NAS) |
|---|---|---|
| `ops.db_path` | `data/operations/operations.sqlite3` | `/data/operations/operations.sqlite3` |
| `archive_root` | `archive` | `/data/archive` |
| `processed_root` | `data/processed` | `/data/processed` |
| `store.db_path` | `data/knowledge/knowledge_store.sqlite3` | `/data/knowledge/knowledge_store.sqlite3` |
| `collected/processed/knowledge` artifacts | under `data/processed/<cid>/knowledge` | same relative layout |

M6 workers read these from configuration; no dual hardcoded path sets in business code.