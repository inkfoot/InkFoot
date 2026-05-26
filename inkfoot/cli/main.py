"""Top-level ``inkfoot`` CLI entry point.

Phase 0 ships three subcommands:

* ``inkfoot report`` — single-run attribution + smells, or
  aggregate view across recent runs (``--last 7d``).
* ``inkfoot rebuild-aggregates`` — recover ``runs.total_*`` from
  the event log after a crash or manual edit.
* ``inkfoot tag`` — attach a ``user_tag`` event to an existing
  run after the fact.

Future epics register more subcommands (``inkfoot tail``,
``inkfoot contract check``, etc.).
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from inkfoot._version import __version__
from inkfoot.cli import rebuild_aggregates, report, tag


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

    rep = subparsers.add_parser(
        "report",
        help="Render a run's attribution bar chart + detected smells.",
    )
    rep.add_argument("--db", default=None, help="Override the default DB path.")
    rep.add_argument(
        "--run",
        default=None,
        help="Render a single run by its id.",
    )
    rep.add_argument(
        "--last",
        default=None,
        help=(
            "Aggregate window (e.g. 7d, 24h, 30d). Renders a "
            "per-bucket summary table instead of a single run."
        ),
    )
    rep.add_argument(
        "--task",
        default=None,
        help="Filter the aggregate view by task name.",
    )
    rep.add_argument(
        "--group-by",
        default="task",
        choices=["task", "agent_kind", "node"],
        help=(
            "Bucket the report. 'task' / 'agent_kind' apply to the "
            "aggregate view (--last); 'node' applies to single-run "
            "view (--run) and slices the ledger by LangGraph / "
            "Pattern-B node_name (ADR-1-1)."
        ),
    )
    rep.add_argument(
        "--show-zero",
        action="store_true",
        help="Show all 14 ledger fields including always-zero ones.",
    )
    rep.set_defaults(func=report.run)

    tg = subparsers.add_parser(
        "tag",
        help="Attach a (key, value) tag to an existing run.",
    )
    tg.add_argument("--db", default=None, help="Override the default DB path.")
    tg.add_argument("run_id", help="The ULID of the run to tag.")
    tg.add_argument("key", help="Tag key (string).")
    tg.add_argument(
        "value",
        help="Tag value. Parsed as JSON when possible (so 5 is int, true is bool).",
    )
    tg.set_defaults(func=tag.run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
