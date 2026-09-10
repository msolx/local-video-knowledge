# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Architectural Decision Log

> **Milestone Status**: `M6-00 = DONE`, `M6-01 = DONE`, `M6-02 = DONE`, `M6-03 = DONE`, `M6-04 .. M6-09 = TODO`
> **Status**: APPROVED / ACTIVE
> **Context**: M2/M3/M4/M5 are COMPLETE/SEALED. M6 automates the full path "Douyin favorite → SEARCHABLE Knowledge Store" with a NAS control plane + capability-based workers.

---

## Decision 1: M6 Automates Orchestration Only — Never Content or Semantics
- **Context**: M2–M5 sealed production logic must not be re-written.
- **Decision**: M6 adds an operations/control layer. It never re-implements collection parsing, media probing, extraction prompts, KU identity, or store semantics. All stage work calls the sealed M2–M5 entry points (audited in M6_OPERATIONS_ARCHITECTURE.md §2). Any required behavior change inside a sealed stage is out of scope for M6 (surface as a contract gap, do not patch silently).

---

## Decision 2: Separate Operations DB from Knowledge Store
- **Context**: Control-plane state and retrieval state have different lifecycle, owner, retention, and failure semantics.
- **Decision**: `data/operations/operations.sqlite3` holds operations state (assets, jobs, attempts, workers, leases, heartbeats, event_log). The M5 `data/knowledge/knowledge_store.sqlite3` stays a derived retrieval artifact. Job state is never written into the Knowledge Store. M6-00 designs the schema; creation happens in M6-01.

---

## Decision 3: SQLite Remains the Operations Storage Technology
- **Context**: The repo is already SQLite-first (collector repo, downloader job store, M5 store); personal scale does not need a server DB.
- **Decision**: Freeze SQLite for the operations DB in v1. No PostgreSQL/Redis/Kafka in v1. Keep it single-writer (one scheduler) + durable queue semantics.

---

## Decision 4: Two Separate State Machines (Asset ≠ Job)
- **Context**: Mixing "where the content is" with "what this execution is doing" corrupts diagnostics and recovery.
- **Decision**: Asset lifecycle (`DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE`) and job lifecycle (`QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL`) are distinct modeled states. An asset's stage is derived from its stage jobs, not a single mutable status.

---

## Decision 5: Canonical Asset Identity Is Frozen (platform, platform_content_id)
- **Context**: M6 must not invent a second identity.
- **Decision**: Continue using the frozen canonical asset identity `(platform, platform_content_id)`. Every orchestration job additionally gets a deterministic `job_id` (§7 of the architecture doc) so repeated scheduler ticks cannot create unbounded duplicate jobs.

---

## Decision 6: Idempotency by Stage + Input Fingerprint + Policy Version
- **Context**: Duplicate discovery/download/extraction must not happen.
- **Decision**: A stage job is a cache hit (SKIP) when a job for the same `(asset, stage, input_artifact_fingerprint, policy_version)` already SUCCEEDED. Upstream input change invalidates only downstream stages. No nightly full knowledge rerun.

---

## Decision 7: Topology A — NAS control+storage+store, PC browser+GPU
- **Context**: M2 collector browser runtime cannot be proven portable (Windows Chrome path, G: profile, NODE_PATH, win32 process control).
- **Decision**: Freeze **Topology A** for M6 v1: NAS owns control plane, storage, operations DB, Knowledge Store; Windows RTX-4090 PC owns Douyin browser/collector, downloader (auth-bound), GPU media/ASR/OCR/VLM, and M4 LLM extraction. Topology B (collector on NAS) is deferred until the browser runtime portability issue is resolved.

---

## Decision 8: Capability-Based Workers, Not Machine Names
- **Context**: Hardcoding "Sean-PC" makes scheduling brittle.
- **Decision**: Workers declare capabilities (`collector`, `downloader`, `cpu_media`, `gpu_asr`, `gpu_vlm`, `llm_extraction`, `store_ingest`). Scheduler dispatches by capability ∩ resources. Machine name is metadata.

---

## Decision 9: Lease-Based Job Claiming + Heartbeat
- **Context**: "Process still alive" is not a crash detector.
- **Decision**: Jobs are claimed via leases (`lease_owner`, `leased_at`, `lease_expires_at`); workers heartbeat. Lease expiry → safe requeue. Scheduler and workers cannot double-run a job because only the lease holder runs it.

---

## Decision 10: Retry Classes — RETRYABLE vs TERMINAL
- **Context**: Infinite fast retry is forbidden.
- **Decision**: `FAILED_RETRYABLE` (PC offline, file lock, LLM endpoint down, network blip) with `attempt_count/max_attempts/backoff`; `FAILED_TERMINAL` (corrupt source, schema failure, unsupported media) without auto-retry. Backoff persists across restarts.

---

## Decision 11: PC Offline Is a Normal Wait, Not Failure
- **Context**: The 4090 PC powering off is expected.
- **Decision**: GPU-required jobs remain `QUEUED` (waiting for capability) when no GPU-capable worker is online — never `FAILED`. On heartbeat, the scheduler assigns them.

---

## Decision 12: Single GPU-Heavy Job at a Time (v1)
- **Context**: ASR/VLM/LLM could fight over 24 GB VRAM.
- **Decision**: v1 GPU resource rule = one GPU-heavy job per GPU worker (capability semaphore). No complex GPU scheduler in v1.

---

## Decision 13: Stage Success = Artifact Invariant
- **Context**: Exit code 0 is not proof of a usable artifact.
- **Decision**: Each stage reports success only when its artifact invariant holds (manifest schema-valid, hashes match, knowledge_units.json schema-valid, M5 ingest returned inserted|replaced|unchanged + validate_store valid). Invariants are listed in the architecture doc §17.

---

## Decision 14: M5 Ingest Is Incremental; Rebuild Is Recovery-Only
- **Context**: Per-favorite full rebuild is wasteful and risks store churn.
- **Decision**: Production path = `ingest_knowledge_document` per new/updated asset. `rebuild_store` is reserved for recovery/admin.

---

## Decision 15: Uncollection ≠ Deletion
- **Context**: PKP is a personal knowledge archive.
- **Decision**: Removing a Douyin favorite must not delete archive/evidence/knowledge/Store entries. A separate explicit deletion workflow is designed later.

---

## Decision 16: Durable Queue + Leases + Idempotency for NAS↔PC Network Failure
- **Context**: NAS↔PC disconnection is normal; no strong-consistency distributed transactions.
- **Decision**: Network resilience = durable queue + lease expiry + per-stage idempotency. Jobs are not lost, not duplicated, and resume on reconnection.

---

## Decision 17: Secrets Never Enter Git / Job DB / Logs
- **Context**: Douyin cookies, browser profile, API secrets, LLM credentials are sensitive.
- **Decision**: Secrets boundary enforced end-to-end. Config loading already rejects secret keys. Job payloads/logs store references, not material. NAS-side Douyin auth (if ever needed in Topology B) requires a dedicated credential store.

---

## Decision 18: M6-00 Is Docs-Only
- **Context**: Freeze contract before any execution milestone.
- **Decision**: M6-00 ships four docs (architecture, decisions, tasks, handoff) and touches no `src/`, `tests/`, `scripts/`, `config/`. No Docker, no operations DB creation, no service registration, no NAS changes, no runtime start. M6-01 is the first code milestone.

---

## Decision 19: Storage = Logical Paths in Config, Physical Per Environment
- **Context**: Business code must not hardcode both Windows and NAS paths.
- **Decision**: Workers read logical path keys from configuration (e.g. `ops.db_path`, `archive_root`, `processed_root`, `store.db_path`); deployment maps logical → physical. The logical↔physical table is in the architecture doc §31.

---

## Decision 20: File Transfer v1 = Shared Filesystem (A), with Atomic-Rename + Hash Verification
- **Context**: NAS and PC need to exchange stage inputs/outputs reliably.
- **Decision**: Prefer option A (SMB/shared filesystem) as the v1 transfer strategy; writers use `.tmp` + atomic rename and SHA-256 verification; readers only see final names after producer success. B (sync/staging) and C (HTTP worker API) are documented fallbacks, not v1.

---

## Decision 21: Additive Job State Correction — `CANCELLED` (M6-01)
- **Context**: M6-00 frozen the admin operation `cancel job`, but the frozen job lifecycle (`QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_TERMINAL`) had no way to distinguish "admin cancelled" from "terminal failure". Without `CANCELLED`, cancelled jobs are indistinguishable from `FAILED_TERMINAL`.
- **Decision**: Add the single additive job state `CANCELLED` to the frozen job lifecycle. It is **terminal**, never auto-retryable, and cannot be requeued/claimed. Legal entries: `QUEUED → CANCELLED` and `FAILED_RETRYABLE → CANCELLED`. Cancelling a `LEASED`/`RUNNING`/already-terminal job is a deterministic no-op (frozen and tested). No other lifecycle state is added or renamed; in particular M6 does **not** add `FAILED_PC_OFFLINE` or `WAITING_FOR_PC` (per Decision 11, a job waiting for a matching capability worker simply stays `QUEUED`).
- **Status**: FROZEN (implemented + tested in M6-01).

---

## Decision 22: At-Least-Once Execution with Lease Fencing (M6-02)
- **Context**: M6 must execute jobs exactly once if possible, but a worker can complete external side effects and then crash before committing `SUCCEEDED`.
- **Decision**: M6 v1 provides **at-least-once** execution semantics via durable queue + atomic claim + lease + fencing token + idempotent stage execution. Exactly-once is **not** claimed; the residual risk (external side effect done, `SUCCEEDED` not committed) is resolved in M6-03 by per-stage **artifact idempotency** (stage success = artifact invariant, never exit code). Duplicate execution is minimized but not theoretically impossible.

---

## Decision 23: Lease Token Is a Fencing Token (M6-02)
- **Context**: Two workers can hold overlapping ownership of one job across crash/reclaim cycles. Checking only `lease_owner == worker_id` is unsafe because one worker process may legitimately re-claim the same job later.
- **Decision**: Every successful claim generates a fresh `lease_token` (`lease_<random>`). Every ownership mutation (renew, start, complete success/retryable/terminal) must submit `(job_id, worker_id, lease_token)`. If the token is not the DB's current token, the mutation is rejected with `StaleLeaseError`. Tokens do not participate in logical job identity; they are execution credentials. Tokens live in the jobs row only and are never copied into event messages or human logs; `ClaimedJob.to_public_dict()`/`repr()` hide them.

---

## Decision 24: Capability Dispatch Is Exact All-Of Matching (M6-02)
- **Context**: M6-00 froze the capability vocabulary but the M6-01 `jobs` schema had no persistent capability field, so capability-aware claim was unimplementable.
- **Decision**: Additive schema correction: `jobs.required_capabilities_json` (`[]` default) while keeping `operations-store-v1` (no migration engine; no production DB existed, so create-schema + validation + tests updated; old dev DBs require rebuild). Claim matches iff `required_capabilities ⊆ worker.capabilities`; empty requirement = generic/control-plane executable. Matching is exact capability name only — no fuzzy/prefix/machine-name routing (hostname is metadata, never routing).

---

## Decision 25: Lease Semantics and Stale Worker Behavior (M6-02)
- **Context**: Lease expiry recovery must distinguish "claimed but never started" from "executing when worker vanished", and a stale worker must not directly fail jobs.
- **Decision**: Lease duration defaults to 120 s (configurable, injected clock). `LEASED`-before-start expiry requeues to `QUEUED` without consuming an attempt. `RUNNING` expiry closes the active attempt as `lease_expired`/`WorkerLeaseExpired` (retryable), applies backoff via `next_retry_at` (never immediate reclaim), and transitions to `FAILED_RETRYABLE` (or `FAILED_TERMINAL` on exhaustion). Worker staleness is derived from `last_heartbeat_at` + threshold and never directly fails jobs — job ownership is decided only by lease expiry. Worker heartbeat and job lease renewal are distinct concepts: heartbeat keeps the worker row alive; an active job lease must be explicitly renewed.

---

## Decision 26: Stage Adapters Wrap Sealed Entry Points; Orchestration Never Reimplements Pipeline Logic (M6-03)
- **Context**: M6 must execute the M2→M5 stage chain from the durable job store, but M2–M5 production logic is sealed and must not be rewritten.
- **Decision**: M6-03 adds `src/operations/stages.py` adapters that only resolve inputs, preflight, call the sealed stage APIs, detect artifact cache, validate outputs, and classify retryable/terminal errors. Adapters never reimplement download/media/knowledge logic. A typed frozen `StageExecutionResult` (`stage`, `canonical_id`, `status` ∈ `EXECUTED`/`CACHE_HIT`, `input_fingerprint`, `output_fingerprint`, `artifacts`, `metadata`) is the only handler return and is JSON-safe (no sqlite Rows, `Path` objects, Enum repr, secrets).

---

## Decision 27: Output Fingerprint = Deterministic Hash of Ordered Canonical Artifacts (M6-03)
- **Context**: The scheduler needs `stage N output_fingerprint` as the durable `input_fingerprint` for stage N+1; a fingerprint that depends on time/worker/attempt/lease/job-id would break downstream identity.
- **Decision**: `stage_output_fingerprint(...)` hashes the stage's canonical output artifacts in a canonical order (`(role, path, sha256)` sorted) plus a stage policy version. Where a stage already has a frozen fingerprint (M4 `fingerprint`/`finalization_fingerprint`, M5 `source_artifact_fingerprint`), it is reused rather than inventing a second incompatible scheme. `mtime`/creation time never participate.

---

## Decision 28: Artifact-Based Idempotency — Cache Hit Means Valid, Not Merely Present (M6-03)
- **Context**: Worker execution is at-least-once; duplicate execution must resolve safely. A truncated/corrupt existing artifact must never be mistaken for a completed stage.
- **Decision**: Every adapter follows INPUT → expected-output policy → if a schema-valid + fingerprint/policy-matching artifact already exists then `CACHE_HIT`, else execute the sealed stage, then validate the artifact invariant, then `EXECUTED`. Adapters validate real artifact content (e.g. `verify_evidence_manifest`/`verify_evidence_chunks`, `CanonicalKnowledgeUnitsDocument.from_dict()`, `CanonicalMediaAssetAdapter.load_from_dir`) — existence alone is never proof. Invalid existing artifacts are never silently overwritten: per-stage they either regenerate deterministically (safe) or fail `Terminal`/`Retryable`.

---

## Decision 29: Capability Mapping for Stage Adapters (M6-03)
- **Context**: M6-04 scheduler needs a deterministic stage→capability map to route jobs to workers.
- **Decision**: `required_capabilities_for_stage(stage, media_type=None)` freezes: `DISCOVER → (collector,)`, `ARCHIVE → (downloader,)`, `MEDIA_PROCESS(video) → (gpu_asr,)`, `MEDIA_PROCESS(album) → (gpu_vlm,)`, `KNOWLEDGE_EXTRACT → (llm_extraction,)`, `KNOWLEDGE_FINALIZE → ()` (deterministic render, control-plane executable), `STORE_INGEST → (store_ingest,)`. The MEDIA_PROCESS split reflects the real M3 routing (video ASR vs album OCR/VLM), not the stage name alone.

---

## Decision 30: Stage Result Persistence + Durable Downstream Fingerprint Handoff (M6-03)
- **Context**: M6-02 `WorkerRuntime` coerced handler returns to `HandlerResult(metadata)`; a stage adapter returning `StageExecutionResult` was previously discarded/unsupported, so M6-04 could not reliably read `output_fingerprint`.
- **Decision**: `WorkerRuntime` now accepts a `StageExecutionResult` return, validates identity (`stage`, `canonical_id`, `input_fingerprint` vs the claimed job; `output_fingerprint` must be a valid SHA-256; JSON-safe), and persists the full JSON payload into the successful attempt's `metadata_json`. New `get_job_result(job_id)` returns the latest successful attempt's `stage_result` — the durable handoff the scheduler reads for `output_fingerprint` without re-scanning artifacts. Job payloads stay identity/reference-only (no transcripts, media, cookies, LLM prompts).

---

## Decision 31: Error Classification Is Per-Adapter and Never Message-Substring Based (M6-03)
- **Context**: Retryable vs terminal must be deterministic and actionable.
- **Decision**: Adapters raise the frozen `RetryableJobError`/`TerminalJobError` with explicit classification: retryable = transient precondition (browser/auth runtime unavailable, network/temporary transfer, LLM endpoint down, store locked); terminal = schema-invalid upstream, unsupported media, corrupt source (hash mismatch), identity mismatch. Exceptions are wrapped at the adapter boundary with explicit stage semantics; no `IOError` blanket mapping.

---

## Decision 32: SQLite-Over-SMB Is Forbidden for the Operations DB (M6-03)
- **Context**: Shared filesystem transfer is frozen for artifact/media exchange only; the NAS-owned `operations.sqlite3` must not be written by Windows workers over SMB.
- **Decision**: The Operations DB is owned by the NAS control plane. Cross-machine worker protocol must go through a thin RPC/HTTP control-plane API or equivalent owner-mediated transport (not implemented in M6-03). Windows GPU workers write M5 to a disposable/local knowledge store or to NAS only via owner-mediated ingest. Store-owner is always a NAS-local process.

---

## Decision 33: At-Least-Once Replay + Input-Change Semantics (M6-03)
- **Context**: Crash-after-side-effect-before-commit must resolve to one canonical artifact; a changed upstream input must not falsely cache-hit against an old output.
- **Decision**: Replay is safe: first run `EXECUTED` writes the artifact, a re-claimed run sees the valid artifact and returns `CACHE_HIT` with the identical output fingerprint, leaving exactly one canonical artifact (P0 acceptance, tested). Cache semantics are input/policy provenance-driven: a changed input fingerprint forms a new logical job and never cache-hits against an old output. Both are covered by tests (`test_at_least_once_replay`, `test_changed_input_invalidates_old_output`).