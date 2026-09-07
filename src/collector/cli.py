"""CLI interface for the collector subsystem."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .base import CollectorMode, CollectorStatus
from .config import CollectorConfig
from .douyin import DouyinCollector, DouyinCollectorConfig
from .service import CollectorService


def build_collector_parser() -> argparse.ArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to collector configuration JSON file.",
    )
    common_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit output as serialized JSON CollectorRunResult.",
    )

    parser = argparse.ArgumentParser(
        prog="collector",
        description="Unified Collection Ingestion CLI for local-video-knowledge.",
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="platform", help="Target platform")

    # Douyin platform parser
    douyin_parser = subparsers.add_parser(
        "douyin",
        help="Douyin Collection Ingestion commands",
        parents=[common_parser],
    )
    douyin_sub = douyin_parser.add_subparsers(dest="action", required=True, help="Collector action")

    # Probe command
    douyin_sub.add_parser(
        "probe",
        help="Inspect collector health, dependencies, and authentication",
        parents=[common_parser],
    )

    # Sync command
    sync_parser = douyin_sub.add_parser(
        "sync",
        help="Run incremental collection synchronization down to watermark",
        parents=[common_parser],
    )
    sync_parser.add_argument("--max-pages", type=int, default=None, help="Maximum number of pages to fetch")
    sync_parser.add_argument("--page-size", type=int, default=None, help="Items count requested per page")
    sync_parser.add_argument("--dry-run", action="store_true", default=False, help="Simulate run and discard staging without committing watermark")

    # Backfill command
    bf_parser = douyin_sub.add_parser(
        "backfill",
        help="Run historical collection backfill",
        parents=[common_parser],
    )
    bf_parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum number of items to backfill")
    bf_parser.add_argument("--max-pages", type=int, default=None, help="Maximum number of pages to fetch")
    bf_parser.add_argument("--page-size", type=int, default=None, help="Items count requested per page")
    bf_parser.add_argument("--dry-run", action="store_true", default=False, help="Simulate run and discard staging without committing watermark")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_collector_parser()
    args = parser.parse_args(argv)

    if not args.platform:
        parser.print_help()
        return 2

    # Load configuration
    if args.config:
        try:
            if args.platform == "douyin":
                config = DouyinCollectorConfig.load(args.config)
            else:
                config = CollectorConfig.load(args.config)
        except Exception as e:
            err_dict = {"error": f"Failed to load config: {e}"}
            if args.json:
                print(json.dumps(err_dict, indent=2))
            else:
                print(f"[ERROR] {err_dict['error']}", file=sys.stderr)
            return 2
    else:
        # Default configuration
        if args.platform == "douyin":
            config = DouyinCollectorConfig(platform="douyin")
        else:
            config = CollectorConfig(platform=args.platform)

    # Initialize collector adapter
    if args.platform == "douyin":
        collector = DouyinCollector(config=config)
    else:
        print(f"[ERROR] Platform '{args.platform}' not recognized or not implemented.", file=sys.stderr)
        return 2

    service = CollectorService(collector=collector, config=config)

    # Dispatch action
    if args.action == "probe":
        result = service.execute(mode=CollectorMode.PROBE)
    elif args.action == "sync":
        result = service.execute(
            mode=CollectorMode.SYNC,
            max_pages=getattr(args, "max_pages", None),
            page_size=getattr(args, "page_size", None),
            dry_run=getattr(args, "dry_run", False),
        )
    elif args.action == "backfill":
        result = service.execute(
            mode=CollectorMode.BACKFILL,
            limit=getattr(args, "limit", None),
            max_pages=getattr(args, "max_pages", None),
            page_size=getattr(args, "page_size", None),
            dry_run=getattr(args, "dry_run", False),
        )
    else:
        parser.print_help()
        return 2

    # Emit output
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"[{result.status.value}] Run ID: {result.run_id}")
        print(f"Platform: {result.platform} | Mode: {result.mode.value}")
        print(f"Started: {result.started_at} | Finished: {result.finished_at}")
        if result.metrics:
            print("Metrics:")
            for k, v in result.metrics.items():
                print(f"  {k}: {v}")
        if result.error:
            print("Error:")
            print(f"  Code: {result.error.get('code')}")
            print(f"  Message: {result.error.get('message')}")

    # Map exit code:
    # PROBE mode allows PARTIAL readiness diagnosis to return 0.
    # SYNC and BACKFILL must strictly achieve full SUCCESS to return 0.
    if result.status == CollectorStatus.SUCCESS:
        return 0
    if result.status == CollectorStatus.PARTIAL and result.mode == CollectorMode.PROBE:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
