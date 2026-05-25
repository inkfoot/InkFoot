"""Top-level ``inkfoot`` CLI entry point.

Only the subcommands shipped this phase are wired. Future epics
register more subcommands (``inkfoot report``, ``inkfoot tail``,
``inkfoot contract check``, etc.).
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from inkfoot._version import __version__
from inkfoot.cli import rebuild_aggregates


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inkfoot",
        description=(
            "Causal token economics layer for LLM agents. "
            "See https://inkfoot.dev for the docs."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"inkfoot {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rebuild = subparsers.add_parser(
        "rebuild-aggregates",
        help="Re-project runs.total_* from the event log.",
    )
    rebuild.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite DB. Defaults to ~/.inkfoot/runs.db.",
    )
    rebuild.set_defaults(func=rebuild_aggregates.run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
