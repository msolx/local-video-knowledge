# M6 Recovery Runbook

> Milestone: M6-05 — Crash Recovery / Retry / Observability
> Scope: how the automated knowledge operations control plane recovers from crashes, retries transient failures, and surfaces health for an operator.
> Applies to: `src/operations/` (store, worker, stages, scheduler, admin, observability, cli).

---

## 1. Principles

- **At-least-once, never exactly-once.** A stage handler may run more than once; every handler must be idempotent and its success must be proven by a durable artifact (fingerprint), never by an exit code.
- **Job state is the durable source of truth.** `jobs`, `job_attempts`, `assets`, `pipeline_runs`, `workers`, `event_log` in `data/operations/operations.sqlite3`.
- **PC offline is a normal wait, not a failure.** Capability-missing jobs stay `QUEUED`; no attempt is consumed while the PC is offline.
- **Stage success = artifact invariant.** A retried stage that already produced the exact output fingerprint returns `CACHE_HIT` and does not re-execute.
- **No secrets in jobs / events / logs / backups.** Lease tokens, cookies, API keys never leave the store.

---

## 2. Recovery Matrix

Each scenario lists the frozen behavior and who handles it.

| # | Scenario | Behavior | Auto / Manual |
|---|---|---|---|
| 1 | Scheduler crash between a `SUCCEEDED` stage and its downstream enqueue | Reconciliation (scheduler phase 4) re-derives the next stage from the milestone and re-enqueues exactly once (job id is deterministic). No duplicate downstream. | AUTO_RECOVER |
| 2 | Worker crash before start (`LEASED`, never `RUNNING`, no attempt) | `recover_expired_leases`: `LEASED → QUEUED`, no attempt consumed. Next claim re-leases. | AUTO_RECOVER |
| 3 | Worker crash during run (`RUNNING` + attempt) | `recover_expired_leases`: closes the attempt (retryable, backoff) and re-queues. Attempt count is incremented at start, so the crash consumes one attempt. Replay re-runs the handler idempotently (at-least-once). | AUTO_RECOVER |
| 4 | PC shutdown / offline (capability gap) | Job stays `QUEUED`; heartbeat goes stale; lease expires → safe requeue. When a matching worker rejoins, the job is claimed and run. | AUTO_RECOVER |
| 5 | Network / lease loss | Lease expiry paths in #2/#3; heartbeat staleness flags the worker; `WorkerRuntime` fences completions with a stale token (rejected, never committed). | AUTO_RECOVER |
| 6 | Duplicate discovery | Deterministic job id + enqueue idempotency → single `ARCHIVE` job. | AUTO_RECOVER |
| 7 | Duplicate enqueue | Same-key `SUCCEEDED` → SKIP; same-key active → exists; retryable → retry path; terminal → no silent resurrection. | AUTO_RECOVER |
| 8 | Lease expiry | `LEASED → QUEUED` (no attempt) or `RUNNING` attempt closed + retryable backoff. | AUTO_RECOVER |
| 9 | Partial artifact | Adapter validates required files exist + schema-valid before returning a result; partial → retryable (re-produce), never `CACHE_HIT`. | AUTO_RECOVER |
| 10 | Corrupt artifact | Schema / hash / fingerprint invalid → `TerminalJobError` (`FAILED_TERMINAL`). Requires operator attention. | MANUAL_ATTENTION |
| 11 | LLM unavailable | Retryable failure with exponential backoff (`next_retry_at`); job re-queued when due; scheduler phase-2 requeues. | AUTO_RECOVER |
| 12 | Store temporary failure (DB lock / busy) | Retryable failure with backoff, distinct from terminal. | AUTO_RECOVER |
| 13 | Restart after hours / days | `startup_recovery` (see §3): validate store → recover leases → requeue due retryables → reconcile runs. Everything is time-based and idempotent. | AUTO_RECOVER |
| 14 | Handler side-effect before completion crash | Retry re-runs the handler; the produced artifact is fingerprint-identical → `CACHE_HIT`; no duplicate side effect is committed. | AUTO_RECOVER |
| 15 | Orchestration invariant failure | Missing durable result / missing output fingerprint / unroutable capability → run `FAILED`, health `INVARIANT_ERROR`. Requires operator attention. | MANUAL_ATTENTION |
| 16 | Retry exhaustion | `attempt_count >= max_attempts` on a retryable → `FAILED_TERMINAL` (no silent resurrection). Operator may override. | MANUAL_ATTENTION |

---

## 3. Startup Recovery

`startup_recovery(db_path)` (admin) runs on control-plane boot:

1. `validate_operations_store` (schema + integrity).
2. `recover_expired_leases` — expire stale leases; close abandoned attempts.
3. Requeue due retryables (scheduler phase 2).
4. Scheduler reconciliation (phase 4) — advance lifecycles, enqueue downstream stages.
5. Never schedules a poll; polling resumes via the normal scheduler loop.

`run_recovery_pass(db_path, run_poll=False, poll_sources=...)` is the same pass with an optional poll step. Both are idempotent and safe to run repeatedly.

### CLI

```
python -m src.operations.cli recover [--db PATH] [--poll douyin:default:3600]
```

---

## 4. Observability

Health vocabulary (frozen):

| Health | Meaning | Attention |
|---|---|---|
| `HEALTHY` | Pipeline complete / idle | auto |
| `RUNNING` | Stage LEASED/RUNNING or QUEUED with matching workers | auto |
| `WAITING_FOR_WORKER` | Current job QUEUED, no live matching worker | auto |
| `WAITING_RETRY` | FAILED_RETRYABLE, backoff in progress | auto |
| `SUCCEEDED` | Latest run completed | auto |
| `FAILED_TERMINAL` | Terminal failure | **manual** |
| `CANCELLED` | Admin cancelled (not a failure) | auto |
| `STALLED` | Lease expired / scheduler crashed mid-advance | **manual** |
| `INVARIANT_ERROR` | Orchestration invariant violated | **manual** |

`MANUAL_ATTENTION_HEALTH = {STALLED, INVARIANT_ERROR, FAILED_TERMINAL}`.

### CLI

```
python -m src.operations.cli status [--json] [--validate]
python -m src.operations.cli asset <canonical_id>
python -m src.operations.cli jobs [--state ...] [--stage ...] [--json]
python -m src.operations.cli failed [--json]
python -m src.operations.cli workers [--json]
python -m src.operations.cli timeline <canonical_id> [--limit N]
```

Every row is secret-scrubbed before output (`lease_token`, cookies, API keys never printed).

---

## 5. Admin Operations

```
python -m src.operations.cli retry <job_id> [--force] [--reason ...] [--input-fingerprint ...]
python -m src.operations.cli cancel <job_id> [--reason ...]
```

- `retry` respects the frozen retry policy: requeues due retryables; `--force` ignores backoff; terminal / exhausted requires `--force` + a valid new input fingerprint to start a new generation.
- `cancel` is additive terminal `CANCELLED`; cancelling an active or already-terminal job is a no-op.

---

## 6. Incident Class: Destructive Tests Against Real Data

> **MANDATORY (M4 incident contract).** Never run a destructive stage adapter (`KnowledgeExtractAdapter`, `KnowledgeFinalizeAdapter`, `MediaProcessAdapter`, …) or a recovery test against the repository's real `data/processed` tree.

- All stage/recovery tests copy required artifacts to a disposable `tmp_path` processed root first (see `_disposable_processed_root` in `tests/test_operations_recovery.py`).
- The generic guard `_guard_rejects_real_processed_root(target)` returns `True` when a target resolves to the real `data/processed` tree — a test/helper that would target it must be rejected.
- Production M5 store (`data/knowledge/knowledge_store.sqlite3`) and the M4 C10 historical artifacts are read-only / never overwritten.
- The 69-KU forensic rerun is **not** production input.

---

## 7. Secrets & Event Spam

- Secrets: lease tokens are never written into event payloads or attempt metadata; CLI output scrubs them; admin functions never accept/echo credentials.
- Event spam: `event_log` is append-only with mutation-delimited event types; the scheduler suppresses duplicate poll/discover events per cycle.