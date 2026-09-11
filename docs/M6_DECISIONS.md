# Milestone M6: Automated Knowledge Operations & NAS/PC Orchestration · Architectural Decision Log

> **Milestone Status**: `M6-00 = DONE`, `M6-01 = DONE`, `M6-02 = DONE`, `M6-03 = DONE`, `M6-04 = DONE`, `M6-05 = DONE`, `M6-06 = DONE`, `M6-07..M6-09 = TODO`
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

---

## Decision 34: Reconciliation Scheduler, Not Event-Stream Scheduler (M6-04)
- **Context**: A scheduler that only reacts to an in-memory event chain loses state on restart; "the job SUCCEEDED but the downstream was never enqueued" must self-heal.
- **Decision**: `src/operations/scheduler.py` is **level-triggered / reconciliation-oriented**. `run_once(now)` re-derives desired state purely from the durable DB + artifacts + `StageExecutionResult` state: recover expired leases → requeue due `FAILED_RETRYABLE` → process completed `DISCOVER` results → reconcile asset pipelines (advance lifecycles, enqueue missing downstream) → schedule `DISCOVER` polls. No memory-only event chain; a restarted scheduler reconstructs all downstream decisions from durable state.

---

## Decision 35: DISCOVER Is a Batch Producer, Not a Per-Asset Parent (M6-04)
- **Context**: `DiscoverAdapter` returns many `(platform, platform_content_id)` identities per poll; treating DISCOVER as one pipeline's parent would emit a single ARCHIVE job for many assets.
- **Decision**: A successful DISCOVER `StageExecutionResult` is processed once (tracked by `scheduler_state.processed_discover_jobs`), and **each** discovered identity is independently registered (idempotent `register_asset`) and enqueued an `ARCHIVE` job under its own pipeline run. DISCOVER is not part of the single-asset downstream graph (`ASSET_PIPELINE_GRAPH = ARCHIVE → MEDIA_PROCESS → KNOWLEDGE_EXTRACT → KNOWLEDGE_FINALIZE → STORE_INGEST`).

---

## Decision 36: DISCOVER Control Identity + Deterministic Poll Generation (M6-04)
- **Context**: DISCOVER polling has no natural content asset; a real content id would pollute asset identity, and a timestamp-based fingerprint would break recovery.
- **Decision**: A reserved **control asset** is used: `(platform, "__discover__")` → `canonical_id = control_{platform}_{source_key}_discover`, flagged `control_plane=True`, never conflicting with real Douyin assets. Poll jobs use `poll_slot_fingerprint(platform, poll_slot, source_key, policy_version)` where `poll_slot = epoch_seconds(now) // interval_seconds` — a deterministic **scheduler trigger identity** (explicitly documented as NOT a knowledge/artifact identity). Overlap suppression: while any DISCOVER job is `QUEUED/LEASED/RUNNING` for the control asset, no new poll is enqueued; the scheduler still advances `last_scheduled_at/next_due_at` so it never spins. When a prior ARCHIVE generation for an asset ended `FAILED_TERMINAL/CANCELLED`, `discovery_control_fingerprint(platform, content_id, generation=N, ...)` bumps `N` so re-discovery forms a new ARCHIVE generation. Watermark/checkpoint semantics stay entirely with M2's collector.

---

## Decision 37: Downstream Enqueue Reuses the Durable Fingerprint Handoff (M6-04)
- **Context**: The scheduler must enqueue stage N+1 with the exact output of stage N.
- **Decision**: For every SUCCEEDED stage the scheduler reads `get_job_result(job_id)`, takes `output_fingerprint`, and enqueues the next stage with `input_fingerprint = output_fingerprint` and `required_capabilities_for_stage(next_stage, media_type=...)`. `CACHE_HIT` and `EXECUTED` are equally successful outputs and both advance the pipeline. If a SUCCEEDED job has no valid stage result, the scheduler records an `orchestration_invariant_failure` event, fails the pipeline run, and never guesses a fingerprint. The `media_type` needed to route `MEDIA_PROCESS` (`gpu_asr` vs `gpu_vlm`) comes from the ARCHIVE result `metadata` (a minimal M6-04 gap-fill to `ArchiveAdapter` — the M2 media adapter is untouched).

---

## Decision 38: Asset Lifecycle = Highest Milestone; Freshness = Run Generation (M6-04)
- **Context**: An asset may be `SEARCHABLE` while a newer refresh pipeline is still `RUNNING`; lifecycle must never regress (`SEARCHABLE → ARCHIVED` is illegal in M6-01).
- **Decision**: Asset lifecycle is monotonic "highest completed capability milestone" (`DISCOVERED → ARCHIVED → EVIDENCE_READY → KNOWLEDGE_READY → SEARCHABLE`). `KNOWLEDGE_EXTRACT` maps to no asset milestone (no new lifecycle state invented). Freshness is represented by the pipeline-run/job generation: a changed upstream fingerprint creates a new logical job generation under the same asset, advancing independently without touching lifecycle. `transition_asset_lifecycle` is only called when the milestone rank strictly increases.

---

## Decision 39: Pipeline Run Completion Is Derived, Not "Last Job Enqueued" (M6-04)
- **Context**: A run must not be marked complete merely because `STORE_INGEST` was enqueued.
- **Decision**: A pipeline run is `SUCCEEDED` only when `STORE_INGEST` is `SUCCEEDED` AND the asset is `SEARCHABLE` (run completion is derived in the reconciliation loop). A `FAILED_TERMINAL`/`CANCELLED` current-generation job fails/cancels the run (`FAILED`/`CANCELLED`). Runs are reused while `RUNNING` (`list_pipeline_runs(status=RUNNING)`), preventing duplicate run creation across scheduler cycles.

---

## Decision 40: Retry Requeue and Lease Recovery Are Scheduler Phase-0 Duties (M6-04)
- **Context**: Retry timing and lease expiry were built in M6-02 as store primitives but nobody was driving them.
- **Decision**: `run_once` calls `recover_expired_leases(now)` (M6-02 reuse, never reimplemented) and requeues `FAILED_RETRYABLE` jobs whose `next_retry_at <= now` and `attempt_count < max_attempts` via `requeue_retryable_job` — same logical job identity, no new job. Not-yet-due jobs stay `FAILED_RETRYABLE`. PC-offline semantics are unchanged: capability-missing jobs stay `QUEUED` and never fail the pipeline; `KNOWLEDGE_FINALIZE` has empty capabilities (control-plane executable), all other stages carry their frozen capability set.

---

## Decision 41: Scheduler State Lives in Operations DB, Not a Second M2 Watermark (M6-04)
- **Context**: Poll timing must survive scheduler restart but must not duplicate the M2 collector's cursor/watermark.
- **Decision**: A minimal additive `scheduler_state` table (`scheduler_key PK`, `last_scheduled_at`, `last_completed_at`, `next_due_at`, `metadata_json`) records only scheduler/control timing (`discover:{platform}:{source_key}` keys). M2's collector remains the sole source of truth for the Douyin cursor/watermark. `operations-store-v1` is retained (no production Ops DB yet; an old dev DB requires rebuild). Duplicate suppression relies on M6-01 enqueue idempotency (deterministic `job_id`) — no separate in-memory dedup cache.

---

## Decision 42: Scheduler Events Are Mutation-Delimited (M6-04)
- **Context**: A poll scheduler running every cycle must not flood `event_log` with idle ticks.
- **Decision**: Scheduler writes append-only events only on actual mutations: poll scheduled (only when a poll job is newly created), assets registered, pipeline run created/completed/failed/cancelled, downstream job enqueued (only when created), lifecycle advanced, retry requeued, orchestration invariant failure. Idle cycles append nothing.

---

## Decision 43: Read-Only Observability Is a Pure Projection (M6-05)
- **Context**: The control plane needs health/diagnostics, but nothing in observability may become a second copy of truth.
- **Decision**: `src/operations/observability.py` is a pure read projection over `assets`, `pipeline_runs`, `jobs`, `job_attempts`, `workers`, `event_log`. It never writes; it derives `AssetPipelineStatus`, `WorkerStatus`, `OperationsSummary` and asset timelines from durable rows only. It can never emit a `lease_token` (explicit field allowlist). Health vocabulary is frozen: `HEALTHY, RUNNING, WAITING_FOR_WORKER, WAITING_RETRY, SUCCEEDED, FAILED_TERMINAL, CANCELLED, STALLED, INVARIANT_ERROR`.

---

## Decision 44: Manual-Attention Health Is Explicit (M6-05)
- **Context**: Operators must know which states require a human, not just the scheduler.
- **Decision**: `MANUAL_ATTENTION_HEALTH = {STALLED, INVARIANT_ERROR, FAILED_TERMINAL}`. Everything else (`WAITING_RETRY`, `WAITING_FOR_WORKER`, `RUNNING`, lease recovery, requeue, worker rejoin) is `AUTO_RECOVER`. `AssetPipelineStatus.needs_attention`/`attention_reason` expose the boundary; health is never derived from the human `workers.status` column alone.

---

## Decision 45: Admin Recovery Reuses Sealed Primitives (M6-05)
- **Context**: `startup_recovery` and repair passes must not re-implement recovery rules.
- **Decision**: `src/operations/admin.py` composes the frozen store primitives (`validate_operations_store`, `recover_expired_leases`, `requeue_retryable_job`, scheduler phase-2/phase-4 reconciliation) into `run_recovery_pass` / `startup_recovery`. `admin_retry_job` respects the frozen retry policy (backoff, exhaustion); terminal/exhausted retry requires `force=True` plus a valid new input fingerprint (new generation). `admin_cancel_job` wraps the additive `CANCELLED` terminal state. `RecoveryResult` is JSON-safe and excludes lease tokens.

---

## Decision 46: At-Least-Once Replay Proves Idempotency via Artifact Fingerprint (M6-05)
- **Context**: A worker may crash after a handler side-effect but before completion commit; the retried run must not duplicate or corrupt.
- **Decision**: Recovery treats retry as at-least-once replay. A retried stage adapter re-runs the handler; if the produced artifacts are fingerprint-identical to the previous attempt, the adapter returns `CACHE_HIT` (same `output_fingerprint`), and the completion commits exactly once. Replay correctness is proven by artifact identity, never by exit codes. Partial artifacts are never `CACHE_HIT`; corrupt artifacts are terminal (manual attention).

---

## Decision 47: Real-Data Incident Guard Is Mandatory for Destructive Tests (M6-05)
- **Context**: The M4 C10 incident proved that a test-mode adapter running against the repository's real `data/processed` can overwrite sealed historical intermediates.
- **Decision**: All M6-05 stage/recovery tests copy required artifacts to disposable `tmp_path` processed roots before running destructive adapters (`KnowledgeExtractAdapter`, `KnowledgeFinalizeAdapter`, `MediaProcessAdapter`, …). The generic guard `_guard_rejects_real_processed_root(target)` must reject the real `data/processed` tree; tests assert it. Production M5 store and M4 C10 historical artifacts are read-only in tests. The 69-KU forensic rerun is never production input. This guard is a regression test, not a bypass.

---

## Decision 48: CLI Is a Thin Secret-Scrubbed Presentation Layer (M6-05)
- **Context**: Operators need status/admin commands without exposing secrets.
- **Decision**: `src/operations/cli.py` is a thin presentation layer over observability + admin. Every subcommand accepts `--db` (env `OPERATIONS_DB_PATH` fallback) and `--json`. Every emitted job row passes through `_secret_free` (strips `lease_token`, cookies, API keys) before output. Commands: `status`, `asset`, `jobs`, `failed`, `workers`, `timeline`, `retry`, `cancel`, `recover`. No network / GPU / LLM runtime is ever started by the CLI.

---

## Decision 49: Windows Worker Host Wraps WorkerRuntime; Lifecycle Stays Platform-Neutral (M6-06)
- **Context**: The PC worker needs a long-running host (config → preflight → single-instance → logging → register → run_forever → graceful stop → exit code), but Windows-specific process lifecycle must not leak into `worker.py`.
- **Decision**: New `src/operations/windows_worker.py` owns all Windows/process concerns (`WorkerHostConfig`, `WorkerPreflightResult`, `SingleInstanceLock`, rotating logging, signal handling, deterministic exit codes, CLI). `WorkerRuntime` remains platform-neutral and is wrapped unchanged. Frozen exit codes: `0` normal stop, `2` config error, `3` preflight failure, `4` already running, `5` fatal runtime error.

---

## Decision 50: Capability Preflight Fails Start Rather Than Silently Dropping (M6-06)
- **Context**: A worker that claims a capability it cannot actually execute would cause the control plane to dispatch a doomed job.
- **Decision**: `run_capability_preflight` checks availability/configuration only (path existence for browser/profile/ASR/VLM/LLM runtimes) and never launches any runtime. Missing prerequisites → `start()` returns `EXIT_PREFLIGHT_FAILURE` with explicit diagnostics (never a silent capability removal). `store_ingest` is deliberately absent from `WINDOWS_PC_TARGET_CAPABILITIES` (NAS owns the M5 store) and produces a warning if claimed.

---

## Decision 51: Local LLM Runtime Policy — llama.cpp Preferred, Path Checks Only in M6-06 (M6-06)
- **Context**: M6-00 frozen `G:\llama.cpp` as the preferred local LLM runtime and `D:\LMmodel` as the model root; LM Studio is not the M6 production default.
- **Decision**: M6-06 preflight performs existence/configuration checks for `llm_runtime`/`llm_model_root` only — it never starts llama.cpp and never scans the model directory. Loading a model is out of scope until a stage executes.

---

## Decision 52: Single-Instance via OS File Lock; Stale Files Are Not Fatal (M6-06)
- **Context**: Two worker hosts must never run concurrently, but a crash leaves a stale lock file that must not permanently block restart.
- **Decision**: `SingleInstanceLock` uses an OS-level byte-range lock (`msvcrt` on Windows, `fcntl` elsewhere) whose identity is the lock path (derived from `worker_id` config, never PID/time). The OS lock is authoritative: a stale file is simply re-locked. A second instance exits `4` (already running). PID written to the lock file is diagnostics-only.

---

## Decision 53: Task Scheduler "At Log On" + Restart-On-Failure, Production Registration Deferred (M6-06)
- **Context**: The collector needs the interactive Windows user session (Chrome + profile), so boot-before-login is wrong for v1. The NAS control-plane endpoint does not exist yet, so registering a production scheduled task now would run a host with no transport.
- **Decision**: Task Scheduler trigger = **At Log On** with a configurable startup delay (default 45 s) and `RunLevel Limited` interactive principal; settings allow running on battery and never stop on battery; restart policy = every 1 minute with high count (no infinite crash-loop). `scripts/windows/install_m6_worker_task.ps1` is **dry-run by default** (`-Apply` registers; intended only after M6-08). This milestone ships ready-to-install artifacts only; no production task is registered.

---

## Decision 54: No SQLite-Over-SMB for the Operations DB; Transport Is Placeholder (M6-06)
- **Context**: Decision 32 forbade workers opening the NAS operations DB over SMB; M6-06 must make that enforceable at the host.
- **Decision**: `WindowsWorkerHost._smb_guard_errors()` rejects a UNC (`\\...`) or URL (`://`) `operations_db_path`; mapped SMB drive letters are forbidden by documented policy (undetectable programmatically). The host requires an explicit local `operations_db_path` and fails closed (exit `2`) rather than auto-creating the production `data/operations/operations.sqlite3`. `control_plane_transport` is `local_sqlite_test` (the only runnable M6-06 transport); `http` is a placeholder that fails closed until M6-07/08.

---

## Decision 55: Worker Host Does Not Own Startup Recovery (M6-06)
- **Context**: M6-05 `startup_recovery` belongs to the NAS control plane, not an execution worker — especially in the future remote topology.
- **Decision**: The Windows worker host only registers, heartbeats, claims, and executes. Recovery/scheduler remain control-plane duties. Windows sleep/shutdown are normal states: lease expiry → NAS recovery; on resume the host re-registers and re-heartbeats. No distributed resume protocol in M6-06.