# M6-07 NAS Control Plane & Remote Worker Transport — Runbook

> Scope: M6-07 delivers the distributed Operations topology: a NAS Control Plane
> (HTTP API + scheduler + startup recovery + NAS local worker + M5 store ownership)
> and a remote Windows worker that talks only through HTTP/RPC. This runbook is
> the operator guide. It does **not** cover the real NAS deployment — that is M6-08.

---

## 1. Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                 NAS Control Plane            │
                    │  Operations SQLite (NAS-local fs only)       │
                    │  M5 Knowledge Store (NAS-local fs only)      │
                    │  Scheduler  ·  startup_recovery  · retry     │
                    │  Observability  ·  event log                 │
                    │  NAS local worker (KNOWLEDGE_FINALIZE,       │
                    │                              STORE_INGEST)   │
                    │  HTTP API (m6-control-plane-api-v1)          │
                    └───────────────▲──────────────────────────────┘
                                    │ HTTP/RPC (Bearer auth)
                    ┌───────────────┴──────────────────────────────┐
                    │        Remote Windows Worker                 │
                    │  DISCOVER · ARCHIVE · MEDIA_PROCESS ·        │
                    │  KNOWLEDGE_EXTRACT (GPU/LLM/browser)         │
                    │  NEVER opens Ops/M5 SQLite directly          │
                    └──────────────────────────────────────────────┘
```

- The NAS Control Plane is the **sole authority** for all Operations state
  mutation (claim/start/renew/complete, recovery, retry, scheduler, asset
  lifecycle). Remote workers never simulate state transitions.
- Stage placement is an **explicit policy**, not a timing race:
  - Windows: `DISCOVER`, `ARCHIVE`, `MEDIA_PROCESS`, `KNOWLEDGE_EXTRACT`
  - NAS:     `KNOWLEDGE_FINALIZE`, `STORE_INGEST`
  - The server derives the claim filter from each worker's registered stage
    allowlist (persisted at registration), never from the request body.

## 2. HTTP API (`m6-control-plane-api-v1`)

| Method | Path                          | Auth   | Purpose                                   |
|--------|-------------------------------|--------|-------------------------------------------|
| GET    | `/health/live`                | none   | process liveness (cheap, no DB validation)|
| GET    | `/health/ready`               | bearer | readiness: Ops DB valid, scheduler + local worker initialized |
| POST   | `/api/v1/workers/register`    | bearer | register a worker (capabilities + allowed_stages) |
| POST   | `/api/v1/workers/heartbeat`   | bearer | heartbeat / renew worker record           |
| POST   | `/api/v1/jobs/claim`          | bearer | claim next eligible job (idempotent replay) |
| POST   | `/api/v1/jobs/{job_id}/start` | bearer | LEASED→RUNNING + attempt begin (idempotent) |
| POST   | `/api/v1/jobs/{job_id}/renew` | bearer | renew lease                              |
| POST   | `/api/v1/jobs/{job_id}/complete` | bearer | complete (succeeded / retryable_failure / terminal_failure), idempotent |
| GET    | `/api/v1/jobs/{job_id}`       | bearer | job state (secrets redacted)              |
| GET    | `/api/v1/jobs/{job_id}/result`| bearer | latest successful stage result            |
| GET    | `/api/v1/status`              | bearer | OperationsSummary projection (M6-05)      |

Structured errors (never tracebacks/SQL/tokens):

```json
{ "error": { "code": "STALE_LEASE", "message": "..." } }
```

Codes: `AUTH_FAILED`, `VALIDATION_ERROR`, `NO_JOB`, `STALE_LEASE`, `CONFLICT`,
`NOT_FOUND`, `SERVER_UNAVAILABLE`, `INVARIANT_ERROR`.

## 3. Authentication

- Bearer token read from environment variable `PKP_CONTROL_PLANE_TOKEN`
  (server and client). Config files only reference the env var name via
  `auth_token_env` — the actual token is never committed, logged, or echoed.
- Constant-time comparison via `hmac.compare_digest`.
- `/health/live` is unauthenticated. Everything else (including
  `/health/ready`) requires auth.
- Production mode **fails closed** if the token env is unset. Only explicit
  local-test mode may bypass auth.

## 4. Ports

- Default HTTP listen: `0.0.0.0:8765` (configurable via `host` / `port` /
  `--host` / `--port`). Intended to be reachable only inside the NAS network;
  no TLS in M6 v1 (assume private trusted network; add a reverse proxy for TLS
  in production).

## 5. Database Ownership

- **Operations DB** and **M5 Knowledge Store** are NAS-local filesystem only.
- The control plane rejects UNC (`\\...`) and URL (`://`) paths for both at
  config parse and at startup (fail-closed).
- The NAS volume itself must be a local filesystem (ext4/btrfs/ZFS/etc.);
  SQLite-over-SMB/NFS is forbidden.
- Remote Windows workers never open either DB — only via the HTTP control plane.

## 6. Artifact Storage

- `archive` and `processed` datasets are shared storage that both hosts can
  reach (e.g. an SMB/CIFS share, NFS, or NAS export).
- Windows and NAS may see the same physical storage under different roots
  (e.g. `Z:\PKP\processed` vs `/srv/pkp/processed`).
- Stage output identity is content-based: the M6-03 frozen fingerprint
  contract excludes `resolved_path` from `ArtifactDescriptor.fingerprint_dict`,
  so path differences across machines never alter output identity.
- Stage execution resolves files from the **local** `processed_root` /
  `archive_root` in each host's config. The NAS never opens Windows-style
  absolute paths; Windows never opens NAS paths.

## 7. Stage Placement & Worker Allowlists

| Stage             | Host    | Capabilities (frozen)                     |
|-------------------|---------|-------------------------------------------|
| DISCOVER          | Windows | collector                                 |
| ARCHIVE           | Windows | downloader                                |
| MEDIA_PROCESS     | Windows | gpu_asr (video) / gpu_vlm (album)         |
| KNOWLEDGE_EXTRACT | Windows | llm_extraction                            |
| KNOWLEDGE_FINALIZE| NAS     | (none — CPU deterministic)                |
| STORE_INGEST      | NAS     | store_ingest                              |

- Allowlists are declared in each host's config (`allowed_stages`) and
  registered with the control plane. The server enforces them at claim time;
  the M6-02 all-of capability rule is unchanged (allowlist is an additive
  placement filter only).

## 8. Windows Worker Config (http mode)

`config/examples/m6_windows_worker_http.example.json`:

```json
{
  "schema_version": "m6-windows-worker-config-v1",
  "worker_id": "windows-pc-4090",
  "capabilities": ["collector", "downloader", "cpu_media", "gpu_asr", "gpu_vlm", "llm_extraction"],
  "operations_db_path": null,
  "knowledge_store_path": null,
  "control_plane_transport": "http",
  "control_plane_url": "http://192.168.1.10:8765",
  "auth_token_env": "PKP_CONTROL_PLANE_TOKEN",
  "http_timeout_seconds": 30.0,
  "reconnect_backoff_seconds": 1.0,
  "allowed_stages": ["DISCOVER", "ARCHIVE", "MEDIA_PROCESS", "KNOWLEDGE_EXTRACT"]
}
```

- When `control_plane_transport = "http"`, `operations_db_path` must be `null`
  (no local Ops DB). `local_sqlite_test` is the only mode that allows a local
  Ops DB (tests / local dev only).
- Preflight in http mode validates URL shape, token env presence, shared roots
  and runtime prerequisites; a temporarily-offline server is classified as a
  warning/retryable connectivity state, not a capability failure.

## 9. Token Setup

1. Generate a strong random token, e.g. `openssl rand -hex 32`.
2. Set it on the NAS server env and on the Windows client env:
   `PKP_CONTROL_PLANE_TOKEN=<token>`.
3. Never write it into config files, compose files, logs, or shell history.
   In Docker, inject via `.env` / Docker secrets / NAS UI environment.

## 10. Docker

- Image: `docker/control-plane/Dockerfile` (`python:3.12-slim`, non-root `pkp`
  user uid/gid 1000, stdlib urllib healthcheck on `/health/live`).
- Compose: `docker/control-plane/docker-compose.example.yml`
  - volumes: `ops_data:/var/lib/pkp/operations` (Ops DB),
    `knowledge_data:/var/lib/pkp/knowledge` (M5 store),
    `archive_data:/mnt/pkp/archive`, `processed_data:/mnt/pkp/processed`,
    `logs_data:/var/log/pkp`.
  - env: `PKP_CONTROL_PLANE_TOKEN=CHANGE_ME` (must be replaced).
  - `restart: unless-stopped`; `read_only: true`; `tmpfs /tmp`;
    `no-new-privileges`.
- Build & disposable smoke (already performed in M6-07, no production bind):
  ```
  docker build -f docker/control-plane/Dockerfile -t pkp-control-plane:test .
  # disposable container + temp volumes + CHANGE_ME token → /health/live,
  # /health/ready, /api/v1/status verified; image removed afterwards.
  ```

## 11. Startup Sequence

The control plane starts in this exact order and only then exposes HTTP ready:

1. load config (reject remote Ops/M5 paths)
2. configure logging (rotating, secret-redacting)
3. open/create local Ops DB
4. `validate_operations_store`
5. verify M5 store path (NAS-local)
6. `startup_recovery` (recover expired leases / close dead attempts)
7. create scheduler
8. create NAS local worker
9. start scheduler loop
10. start local worker loop
11. expose HTTP ready

`/health/ready` reports `ready` only after all steps succeed; init errors
surface as structured `{error:{code,message}}` responses.

## 12. Recovery

- Control-plane-owned: `startup_recovery` on boot, lease expiry recovery,
  retry requeue with backoff, pipeline reconciliation. All use the sealed
  M6-05 primitives.
- Remote worker crash mid-run: lease expires → NAS recovery closes the
  attempt as `WorkerLeaseExpired` (retryable) and requeues with backoff; on
  retry the sealed adapter `CACHE_HIT` path completes at-least-once.
- Fencing stays authoritative: a stale lease completion returns `STALE_LEASE`
  even if the worker already produced an artifact.

## 13. NAS Restart

- The Ops DB is durable on the NAS local volume. On restart the control plane
  runs `startup_recovery` then reconciles runs; remote workers reconnect via
  heartbeat and the pipeline continues automatically. A temporary server
  outage is a `RemoteUnavailableError` for workers — bounded retry/backoff,
  workers keep running, no manual restart needed.

## 14. PC Offline

- Windows-required jobs remain `QUEUED` (lease never held); the NAS local
  worker never claims Windows-stage jobs (server-authoritative allowlist).
  When the PC returns, it re-registers/re-heartbeats and claims the waiting
  jobs automatically.

## 15. Network Partition

- If the worker finishes side effects but the completion response is lost, the
  completion RPC is idempotent (state-based replay): a job already in the
  terminal target state with a matching attempt is replayed, never
  double-written. A stale (expired/re-claimed) completion returns
  `STALE_LEASE`; the job retries later and completes via `CACHE_HIT`.

## 16. SQLite-over-SMB Protection

- Ops DB + M5 store: NAS-local filesystem only; UNC/URL paths rejected at
  config and startup.
- Windows worker: `operations_db_path` must be `null` in http mode; a local
  Ops DB is only permitted under `local_sqlite_test` (test/local dev).

## 17. Deployment Checklist (M6-08)

- [ ] Create production volumes (Ops DB, M5 store, archive, processed, logs).
- [ ] Inject a real `PKP_CONTROL_PLANE_TOKEN` (env / secret, never committed).
- [ ] Configure the NAS control plane JSON with NAS-local paths.
- [ ] Build/pull the control-plane image; `docker compose up -d`.
- [ ] Verify `/health/live` and `/health/ready` (authenticated).
- [ ] Set `PKP_CONTROL_PLANE_TOKEN` on the Windows PC.
- [ ] Copy `m6_windows_worker_http.example.json` → `config/local/m6_windows_worker.json`
      with the real `control_plane_url`; run `run_m6_worker.ps1 -Command preflight`.
- [ ] Register the scheduled task via `install_m6_worker_task.ps1 -Apply`
      (M6-08; intentionally never registered in M6-07).
- [ ] Confirm workers appear in `/api/v1/status` and run the real E2E.

## 18. Rollback

- Stop the control plane: `docker compose down` (data volumes persist).
- Unregister the Windows task: `scripts/windows/uninstall_m6_worker_task.ps1`.
- Previous store revisions remain intact; M5 store is only ever written by
  `STORE_INGEST` (NAS local) and is unchanged by this milestone.
- No migration is needed between M6-07 and M6-08: `operations-store-v1` schema
  retained (idempotency is an additive control-plane-owned table).