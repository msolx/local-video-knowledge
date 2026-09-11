"""M6-05 admin/debug CLI (thin presentation layer over observability + admin).

This is an operations console, not an end-user UI. It is deliberately
dependency-free (argparse + stdlib only) so it can run on the NAS control plane
without installing Rich/Textual.

Security (frozen M6-05 §32/§36):

  - default DB path: ``--db`` > env ``OPERATIONS_DB_PATH`` > the project default
  - mutation commands print the affected job/stage/state only
  - no lease_token, cookie, Authorization, API key or credential is ever printed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .models import DEFAULT_OPERATIONS_PATH
from .store import get_job

CLI_PROMPT_VERSION = "m6-cli-v1"


def _db_path_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "db", None):
        return Path(args.db)
    env_path = os.environ.get("OPERATIONS_DB_PATH")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_OPERATIONS_PATH)


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    elif isinstance(value, dict):
        for key in sorted(value.keys()):
            print(f"{key}: {value[key]}")
    else:
        print(value)


# ----------------------------------------------------------------------
# Human-readable rendering (table-like plain text, no framework)
# ----------------------------------------------------------------------


def _render_status_row(status: dict[str, Any]) -> str:
    worker = status.get("assigned_worker") or "-"
    if status.get("worker_stale"):
        worker += " (stale)"
    attempts = f"{status.get('attempt_count', 0)}/{status.get('max_attempts', 0)}"
    next_retry = status.get("next_retry_at") or "-"
    if status.get("needs_attention"):
        flag = "*ATTN*"
    else:
        flag = ""
    return (
        f"{status.get('canonical_id'):<34} | "
        f"{status.get('asset_lifecycle_state'):<15} | "
        f"{status.get('current_pipeline_status') or '-':<9} | "
        f"{status.get('current_stage') or '-':<17} | "
        f"{status.get('current_job_state') or '-':<16} | "
        f"{worker:<22} | "
        f"{attempts:<7} | "
        f"{next_retry:<25} | "
        f"{status.get('health'):<18} | {flag}"
    )


def _print_status_table(statuses: list[dict[str, Any]]) -> None:
    header = (
        "CANONICAL ID                       | LIFECYCLE        | PIPELINE   | "
        "STAGE              | JOB STATE        | WORKER                 | "
        "ATTEMPTS | NEXT RETRY                 | HEALTH             | FLAG"
    )
    print(header)
    print("-" * len(header))
    for status in statuses:
        print(_render_status_row(status))


# ----------------------------------------------------------------------
# Subcommand implementations
# ----------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    from .observability import compute_operations_summary, list_asset_pipeline_statuses

    db_path = _db_path_from_args(args)
    summary = compute_operations_summary(
        db_path,
        include_validation=args.validate,
    )
    statuses = [s.to_dict() for s in list_asset_pipeline_statuses(db_path)]
    if args.json:
        _emit(
            {
                "summary": summary.to_dict(),
                "assets": statuses,
            },
            as_json=True,
        )
        return 0
    print(f"operations store      : {db_path}")
    print(f"assets_total          : {summary.assets_total}")
    print(f"searchable_assets     : {summary.searchable_assets}")
    print(f"active_pipeline_runs  : {summary.active_pipeline_runs}")
    print(f"jobs_by_state         : {summary.jobs_by_state}")
    print(f"workers               : {summary.workers_online} online / "
          f"{summary.workers_stale} stale / {summary.workers_total} total")
    print(f"waiting_for_worker    : {summary.waiting_for_worker}")
    print(f"waiting_retry         : {summary.waiting_retry}")
    print(f"terminal_failures     : {summary.terminal_failures}")
    print(f"stalled_pipelines     : {summary.stalled_pipelines}")
    print(f"invariant_errors      : {summary.invariant_errors}")
    if summary.next_retry_at:
        print(f"earliest_next_retry_at: {summary.next_retry_at}")
    if summary.store_valid is not None:
        print(f"store_valid           : {summary.store_valid}")
    print()
    _print_status_table(statuses)
    return 0


def _cmd_asset(args: argparse.Namespace) -> int:
    from .observability import get_asset_pipeline_status

    db_path = _db_path_from_args(args)
    status = get_asset_pipeline_status(db_path, args.canonical_id)
    if status is None:
        print(f"asset {args.canonical_id} not found")
        return 1
    _emit(status.to_dict(), as_json=args.json)
    return 0


def _secret_free(job: dict[str, Any]) -> dict[str, Any]:
    """Strip execution secrets from a job row before any output."""
    row = dict(job)
    row.pop("lease_token", None)
    return row


def _cmd_jobs(args: argparse.Namespace) -> int:
    from .store import list_jobs

    db_path = _db_path_from_args(args)
    jobs = list_jobs(db_path, state=args.state, stage=args.stage)
    rows = [_secret_free(j) for j in jobs]
    if args.json:
        _emit(rows, as_json=True)
        return 0
    header = (
        "JOB ID                          | STAGE             | STATE            | "
        "CANONICAL ID                    | ATTEMPTS | NEXT RETRY"
    )
    print(header)
    print("-" * len(header))
    for j in rows:
        next_retry = j.get("next_retry_at") or "-"
        print(
            f"{j.get('job_id'):<34} | "
            f"{j.get('stage'):<17} | "
            f"{j.get('state'):<16} | "
            f"{j.get('canonical_id'):<30} | "
            f"{j.get('attempt_count', 0):>3}/{j.get('max_attempts', 0):<3} | "
            f"{next_retry}"
        )
    return 0


def _cmd_failed(args: argparse.Namespace) -> int:
    from .store import list_failed_jobs, list_job_attempts

    db_path = _db_path_from_args(args)
    jobs = list_failed_jobs(db_path)
    rows: list[dict[str, Any]] = []
    for j in jobs:
        row = _secret_free(j)
        last_error = None
        for attempt in list_job_attempts(db_path, j["job_id"]):
            if attempt.get("outcome") == "failed":
                last_error = attempt.get("error_message") or attempt.get("error_class")
        row["last_error"] = last_error
        rows.append(row)
    if args.json:
        _emit(rows, as_json=True)
        return 0
    header = (
        "JOB ID                          | STAGE             | STATE            | "
        "CANONICAL ID                    | ATTEMPTS | LAST ERROR"
    )
    print(header)
    print("-" * len(header))
    for j in rows:
        last_error = (j.get("last_error") or "")[:40]
        print(
            f"{j.get('job_id'):<34} | "
            f"{j.get('stage'):<17} | "
            f"{j.get('state'):<16} | "
            f"{j.get('canonical_id'):<30} | "
            f"{j.get('attempt_count', 0):>3}/{j.get('max_attempts', 0):<3} | "
            f"{last_error}"
        )
    return 0


def _cmd_workers(args: argparse.Namespace) -> int:
    from .observability import list_worker_statuses

    db_path = _db_path_from_args(args)
    workers = [w.to_dict() for w in list_worker_statuses(db_path)]
    if args.json:
        _emit(workers, as_json=True)
        return 0
    header = "WORKER ID | DISPLAY NAME | HOSTNAME | CAPABILITIES | STATUS | OWNED JOBS | RUNNING"
    print(header)
    print("-" * len(header))
    for w in workers:
        print(
            f"{w.get('worker_id'):<10} | "
            f"{str(w.get('display_name') or '-'):<12} | "
            f"{str(w.get('hostname') or '-'):<9} | "
            f"{','.join(w.get('capabilities', [])) or '-':<13} | "
            f"{w.get('derived_status'):<6} | "
            f"{','.join(w.get('currently_owned_jobs', [])) or '-':<11} | "
            f"{','.join(w.get('running_jobs', [])) or '-'}"
        )
    return 0


def _cmd_timeline(args: argparse.Namespace) -> int:
    from .observability import get_asset_timeline

    db_path = _db_path_from_args(args)
    timeline = get_asset_timeline(db_path, args.canonical_id, limit=args.limit)
    if args.json:
        _emit(timeline, as_json=True)
        return 0
    for event in timeline:
        print(
            f"{event.get('timestamp')}  {event.get('event_type'):<28} "
            f"{event.get('from_state') or '-':<12} -> {event.get('to_state') or '-':<12} "
            f"job={event.get('job_id') or '-'} worker={event.get('worker_id') or '-'} "
            f"{event.get('message') or ''}"
        )
    return 0


def _cmd_retry(args: argparse.Namespace) -> int:
    from .admin import admin_retry_job

    db_path = _db_path_from_args(args)
    result = admin_retry_job(
        db_path,
        args.job_id,
        force=args.force,
        reason=args.reason,
        new_input_fingerprint=args.input_fingerprint,
    )
    _emit(result.to_dict(), as_json=args.json)
    if result.outcome in ("requeued", "new_generation_enqueued"):
        return 0
    return 1


def _cmd_cancel(args: argparse.Namespace) -> int:
    from .admin import admin_cancel_job

    db_path = _db_path_from_args(args)
    result = admin_cancel_job(db_path, args.job_id, reason=args.reason)
    _emit(result.to_dict(), as_json=args.json)
    if result.outcome == "cancelled":
        return 0
    return 1


def _cmd_recover(args: argparse.Namespace) -> int:
    from .admin import run_recovery_pass
    from .scheduler import PollSource

    db_path = _db_path_from_args(args)
    poll_sources = None
    if args.poll:
        sources = []
        for raw in args.poll:
            parts = raw.split(":", 2)
            if len(parts) == 3:
                sources.append(
                    PollSource(
                        platform=parts[0],
                        source_key=parts[1],
                        interval_seconds=int(parts[2]),
                    )
                )
            elif len(parts) == 2:
                sources.append(
                    PollSource(platform=parts[0], source_key=parts[1])
                )
            else:
                sources.append(PollSource(source_key=parts[0]))
        poll_sources = sources
    result = run_recovery_pass(
        db_path, run_poll=bool(poll_sources), poll_sources=poll_sources
    )
    _emit(result.to_dict(), as_json=args.json)
    return 0


# ----------------------------------------------------------------------
# argparse wiring
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="src.operations.cli",
        description="M6 operations admin/debug console (read-only + explicit recovery).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="operations sqlite path (default: $OPERATIONS_DB_PATH or project default)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON output (stable schema)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Every subcommand accepts --db and --json in any position. SUPPRESS keeps
    # the global parser's value when the option is not re-supplied on the
    # subcommand, so `--db X status` and `status --db X` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="operations sqlite path (default: $OPERATIONS_DB_PATH or project default)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable JSON output (stable schema)",
    )

    p_status = sub.add_parser("status", parents=[common], help="global operations summary")
    p_status.add_argument(
        "--validate",
        action="store_true",
        help="also run full validate_operations_store (slower)",
    )
    p_status.set_defaults(func=_cmd_status)

    p_asset = sub.add_parser("asset", parents=[common], help="asset pipeline status")
    p_asset.add_argument("canonical_id")
    p_asset.set_defaults(func=_cmd_asset)

    p_jobs = sub.add_parser("jobs", parents=[common], help="list jobs (filterable)")
    p_jobs.add_argument("--state", default=None)
    p_jobs.add_argument("--stage", default=None)
    p_jobs.set_defaults(func=_cmd_jobs)

    p_failed = sub.add_parser("failed", parents=[common], help="list FAILED_RETRYABLE/FAILED_TERMINAL jobs")
    p_failed.set_defaults(func=_cmd_failed)

    p_workers = sub.add_parser("workers", parents=[common], help="worker status")
    p_workers.set_defaults(func=_cmd_workers)

    p_timeline = sub.add_parser("timeline", parents=[common], help="asset event timeline")
    p_timeline.add_argument("canonical_id")
    p_timeline.add_argument("--limit", type=int, default=None)
    p_timeline.set_defaults(func=_cmd_timeline)

    p_retry = sub.add_parser("retry", parents=[common], help="admin retry a job")
    p_retry.add_argument("job_id")
    p_retry.add_argument("--force", action="store_true")
    p_retry.add_argument("--reason", default=None)
    p_retry.add_argument("--input-fingerprint", default=None)
    p_retry.set_defaults(func=_cmd_retry)

    p_cancel = sub.add_parser("cancel", parents=[common], help="admin cancel a job")
    p_cancel.add_argument("job_id")
    p_cancel.add_argument("--reason", default=None)
    p_cancel.set_defaults(func=_cmd_cancel)

    p_recover = sub.add_parser("recover", parents=[common], help="run a recovery pass")
    p_recover.add_argument(
        "--poll",
        action="append",
        default=None,
        metavar="PLATFORM:SOURCE:INTERVAL",
        help="optional poll source to also schedule (e.g. douyin:douyin:3600)",
    )
    p_recover.set_defaults(func=_cmd_recover)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())