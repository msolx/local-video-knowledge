"""Top-level CLI router for local-video-knowledge repository."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .collector.cli import build_collector_parser, main as collector_main


def main(argv: Sequence[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # Route collector subcommand directly
    if args and args[0] == "collector":
        return collector_main(args[1:])

    # Top-level fallback parser
    parser = argparse.ArgumentParser(
        prog="local-video-knowledge",
        description="Local Video Knowledge Ingestion Engine CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")
    subparsers.add_parser("collector", help="Run the collection ingestion subsystem")

    parsed = parser.parse_args(args[:1])
    if parsed.subcommand == "collector":
        return collector_main(args[1:])

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
