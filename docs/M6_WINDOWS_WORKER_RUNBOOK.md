# M6-06 · Windows PC Worker Host & Autostart Runbook

> Milestone: `M6-06 = DONE` (sealed). Frozen commit contract: `feat(m6): add Windows worker host and autostart`.
> Scope: Windows PC worker host lifecycle + ready-to-install Task Scheduler autostart artifacts. **No production scheduled task is registered this milestone** (deferred to M6-08, when the NAS control-plane endpoint exists).

---

## 1. Role of the Windows Worker

The Windows RTX-4090 PC is the execution worker in the frozen M6 **Topology A** (NAS = control plane + storage + operations DB + Knowledge Store; PC = browser/collector + downloader + GPU media/ASR/OCR/VLM + M4 LLM extraction).

`src/operations/windows_worker.py` wraps the platform-neutral `WorkerRuntime` (M6-02) into a reliable long-running host:

```
load config → preflight → single-instance lock → logging
→ build WorkerRuntime → register worker → heartbeat thread
→ run_forever → graceful stop → deterministic exit code
```

The host is **not** the operations admin CLI (M6-05 `cli.py`) and **not** the scheduler. It only registers, heartbeats, claims and executes jobs.

---

## 2. Topology Boundary (frozen)

- The Windows worker must **never** open a NAS-hosted `operations.sqlite3` over SMB/UNC. The operations DB is owned by the NAS control-plane process.
- The shared filesystem is used **only** for media / archive / processed artifacts.
- The future Windows-worker ↔ NAS-control-plane link is a thin RPC/HTTP control-plane API. In M6-06 the transport is a placeholder:

| `control_plane_transport` | Meaning | Runnable in M6-06? |
|---|---|---|
| `local_sqlite_test` | local-dev / tests against a temp/local SQLite operations store | yes |
| `http` | placeholder for M6-07/08 remote transport | **no — fails closed** |

A `local_sqlite_test` config is never acceptable as a NAS production topology.

---

## 3. Local-Dev vs Production

- **Local-dev**: a `WorkerHostConfig` with `control_plane_transport = "local_sqlite_test"` and an explicit local `operations_db_path` (temp/test path). Used by tests and for host-lifecycle smoke runs. Marked as local-dev only.
- **Production**: requires the M6-07/08 `http` transport and a control-plane endpoint. **Not wired in M6-06.** No production config is shipped; the example config (`config/examples/m6_windows_worker.example.json`) is example-only.
- Real local/private configs live at `config/local/m6_windows_worker.json` (gitignored via `config/local/`).

---

## 4. Config Schema (`m6-windows-worker-config-v1`)

Required:
- `worker_id` (non-empty; identity of the single-instance lock)
- `capabilities` (non-empty list; must be frozen vocabulary)
- `control_plane_transport` (`local_sqlite_test` | `http`)

Optional but used by the host:
- `operations_db_path` — local-dev/test store. If absent, `run` fails closed (exit 2); the production `data/operations/operations.sqlite3` is **never** auto-created.
- `workspace_root`, `archive_root`, `processed_root`, `knowledge_store_path` — logical paths.
- `poll_interval_seconds`, `heartbeat_interval_seconds`, `lease_duration_seconds`, `worker_stale_threshold_seconds`.
- `log_path` (default `logs/operations/windows-worker.log`), `log_max_bytes` (10 MB), `log_backup_count` (5).
- `single_instance_lock_path` (default `data/operations/windows-worker.lock`).
- `startup_delay_seconds` (Task Scheduler delay, default 45).
- `runtime_references` — path **references only** (browser executable/profile, node, ASR/VLM/LLM runtimes, model roots). Used by preflight existence checks.

**Secrets never live in config** (no cookies, tokens, API keys, passwords, credentials).

---

## 5. Capability Profile (Windows 4090 PC)

Target profile per the frozen topology:

```
collector
downloader
cpu_media
gpu_asr
gpu_vlm
llm_extraction
```

`store_ingest` is **not** claimed: the M5 Knowledge Store is owned by the NAS control-plane side (M6-08). Capabilities are **explicitly configured**, never guessed from hostname.

---

## 6. Preflight

`run_capability_preflight(config)` performs **availability/configuration checks only** — it never launches a browser, touches Douyin, runs Whisper, loads a model, or starts llama.cpp. Per declared capability:

- `collector` → browser executable exists (required), browser profile exists (required), node runtime optional.
- `downloader` / `cpu_media` → configuration present (no runtime launch).
- `gpu_asr` → ASR runtime exists (required), model root optional.
- `gpu_vlm` → VLM runtime + model reference exist (required).
- `llm_extraction` → LLM runtime exists (required, expected `G:\llama.cpp`), model root optional (expected `D:\LMmodel`).
- `store_ingest` (if claimed) → warning (NAS-owned).

A capability whose prerequisite is missing ⇒ preflight **FAILED** (exit 3) with explicit diagnostics. The host never silently drops a capability and continues.

`WorkerPreflightResult` is JSON-safe and never contains cookies/tokens/lease tokens/secret values. `preflight --config <path> [--json]` is machine-checkable without starting the worker.

---

## 7. Local LLM Runtime Policy (llama.cpp)

- Preferred local runtime: `G:\llama.cpp`; model root: `D:\LMmodel`. LM Studio is not the M6 production default.
- M6-06 does **not** start llama.cpp and does **not** scan the model directory — only path existence/configuration checks.

---

## 8. Single Instance

`SingleInstanceLock` uses an OS-level byte-range file lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere):

- Identity = the lock path (derived from `worker_id` config), never PID/time.
- The OS lock is authoritative. A **stale lock file** left by a crash is simply re-locked — it never blocks restart.
- A second instance of the same worker config exits **4** (`already running`).

---

## 9. Logging

Stdlib `logging` + `RotatingFileHandler`:

- Default file: `logs/operations/windows-worker.log`, 10 MB × 5 backups.
- Records startup, preflight, worker registration, host lifecycle, idle/retry-level diagnostics, shutdown, fatal exceptions.
- **No** high-frequency output per heartbeat / per lease renewal (avoids log thrash).

**Redaction**: `lease_token`, cookies, `Authorization`, API keys, passwords, browser session contents are never logged. `redact_config` / `redact_worker_registration` / `redact_message` strip secret-shaped keys; the full config dict is never dumped.

---

## 10. Graceful Shutdown

- Ctrl+C / SIGINT (and SIGTERM where available) set the stop event and call `WorkerRuntime.stop()`, letting `run_forever` end naturally.
- Shutdown never marks a running job SUCCEEDED/FAILED itself; a handler that cannot stop immediately is left to lease semantics + subsequent NAS recovery.

---

## 11. Exit Codes (frozen, documented)

| Code | Meaning |
|---|---|
| 0 | normal stop |
| 2 | config error (incl. fail-closed: no `operations_db_path`, UNC/SMB path, `http` transport not implemented) |
| 3 | preflight failure |
| 4 | already running (single-instance) |
| 5 | fatal runtime error |

Task Scheduler's restart policy reacts to non-zero exits.

---

## 12. Fatal Runtime Error

An uncaught host-level exception is logged (redacted) and exits **5**. The host does **not** swallow fatal bugs in an internal `while True` restart loop; the restart policy belongs to Windows Task Scheduler.

---

## 13. Task Scheduler Design

- **Trigger**: **At Log On** (`RunLevel Limited`, interactive user). Rationale: the collector needs the interactive Windows session (Chrome + profile). If a project audit later proves the host can run without a user session, a boot trigger may be reconsidered.
- **Startup delay**: 45 s default (configurable `-StartupDelaySeconds`), letting G:, the user profile and runtime environment settle. The delay is an install parameter, not scattered.
- **Restart policy**: restart on failure every 1 minute (`-RestartIntervalMinutes`) with a high count (`-RestartCount 999999`); settings allow starting on battery and never stop on battery; no execution-time limit (long-running host). This avoids a per-second crash loop while keeping the host alive across days.
- **Exact execution command**: the repository venv Python (e.g. `G:\local_pc_project\personal-knowledge-pipeline\.venv\Scripts\python.exe -m src.operations.windows_worker run --config …`) with `WorkingDirectory` = repository root — never the system PATH / default python / shell profile.
- **Power settings**: the host does not block Windows sleep.

---

## 14. PowerShell Scripts (`scripts/windows/`)

| Script | Purpose | Safety |
|---|---|---|
| `run_m6_worker.ps1` | resolve repo + exact venv Python, invoke worker host, propagate exit code | no worker logic |
| `install_m6_worker_task.ps1` | validate config, build + **dry-run** the task definition | `-Apply` registers (deferred to M6-08); default = 0 mutation |
| `uninstall_m6_worker_task.ps1` | idempotent uninstall (missing task = no-op exit 0) | never touches other tasks |
| `status_m6_worker_task.ps1` | read-only status (exists / state / last run / last result / next run) | never registers/unregisters; secret-free |

**No production scheduled task is registered this milestone.** `install_m6_worker_task.ps1` prints the exact task definition (task name, trigger, executable, arguments, working dir, restart policy) in dry-run mode. Production enablement happens in M6-08 after the NAS control-plane/API is verified.

---

## 15. Sleep / Shutdown / Offline Semantics

- Windows sleep/shutdown/offline is a **normal** state. If the worker stops heartbeating, NAS lease expiry → recovery.
- On resume, the host re-registers and re-heartbeats, then continues claiming.
- A normal Windows shutdown gives the host a best-effort graceful stop; a power cut has no extra local recovery DB. NAS lease semantics handle recovery. The host never copies the operations DB to Windows.

---

## 16. No SQLite-Over-SMB Rule

- `WindowsWorkerHost._smb_guard_errors()` rejects an `operations_db_path` that is a UNC path (`\\...`) or a URL (`://`).
- Mapped SMB drive letters are equally forbidden by policy even though the program cannot reliably detect them (documented, not the only safety mechanism).
- The host requires an explicit local `operations_db_path` and fails closed otherwise.

---

## 17. Host Diagnostics

`preflight --json` outputs the startup configuration summary, capabilities, preflight result, worker id and host state — never lease tokens, secrets, cookies, or model contents. `print-config` prints the parsed redacted configuration.

---

## 18. Startup Recovery Ownership

`startup_recovery` (M6-05) is a **control-plane / NAS** duty. The Windows execution worker does **not** call it, especially under the future remote topology. Worker host duties: register → heartbeat → claim → execute. Control plane duties: recovery, scheduler, pipeline reconciliation.

---

## 19. Why Production Registration Is Deferred

The NAS remote control-plane transport does not exist yet (M6-07/08). Registering a production scheduled task now would launch a host that can only run `local_sqlite_test` against a local/temp DB — not a NAS topology. M6-06 therefore ships **ready-to-install** artifacts and explicit `-DryRun` tooling; actual `Register-ScheduledTask` is deferred to M6-08.

---

## 20. Quick Reference

```powershell
# Validate config without starting anything (JSON output)
.\.venv\Scripts\python.exe -m src.operations.windows_worker preflight --config config\examples\m6_windows_worker.example.json --json

# Print parsed redacted config
.\.venv\Scripts\python.exe -m src.operations.windows_worker print-config --config config\examples\m6_windows_worker.example.json

# Run the host (local-dev: requires an explicit local operations_db_path in the config)
& .\scripts\windows\run_m6_worker.ps1 -Config .\config\local\m6_windows_worker.json -Command run

# Dry-run Task Scheduler install (0 mutation)
.\scripts\windows\install_m6_worker_task.ps1 -Config .\config\local\m6_windows_worker.json

# Status / uninstall (read-only / idempotent)
.\scripts\windows\status_m6_worker_task.ps1
.\scripts\windows\uninstall_m6_worker_task.ps1
```