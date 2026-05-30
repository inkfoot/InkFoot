"""Top-level ``inkfoot`` CLI entry point.

Currently shipping subcommands:

* ``inkfoot report`` — single-run attribution + smells, or
  aggregate view across recent runs (``--last 7d``).
* ``inkfoot rebuild-aggregates`` — recover ``runs.total_*`` from
  the event log after a crash or manual edit.
* ``inkfoot tag`` — attach a ``user_tag`` event to an existing
  run after the fact.
* ``inkfoot benchmark`` / ``inkfoot diff`` — scenario runner and
  artefact comparison for the CI cost-review workflow.
* ``inkfoot tail`` — live event stream for debugging an agent
  while it runs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from inkfoot._version import __version__
from inkfoot.cli import (
    benchmark,
    diff,
    rebuild_aggregates,
    report,
    tag,
    tail,
)
from inkfoot.diff.thresholds import THRESHOLD_PRESETS, DEFAULT_THRESHOLD_NAME


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
            "Pattern-B node_name (framework metadata contract)."
        ),
    )
    rep.add_argument(
        "--show-zero",
        action="store_true",
        help="Show all 14 ledger fields including always-zero ones.",
    )
    rep.add_argument(
        "--no-smells",
        action="store_true",
        help=(
            "Suppress the smells stanza. By default `report` evaluates "
            "the smell engine and renders any hits inline; pass this "
            "for a smell-free attribution view."
        ),
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

    bench = subparsers.add_parser(
        "benchmark",
        help=(
            "Run scenario suites under instrumentation and emit a "
            "benchmark JSON artefact."
        ),
    )
    bench.add_argument(
        "scenarios_dir",
        help="Directory of `.py` scenario files to discover and run.",
    )
    bench.add_argument(
        "--output",
        default=None,
        help="Write the artefact JSON to this path (in addition to stdout).",
    )
    bench.add_argument(
        "--scenarios-only",
        action="append",
        default=None,
        help=(
            "Run only scenarios matching this name. Matched against "
            "either the scenario's `INKFOOT_SCENARIO['task']` value "
            "or the bare filename stem (e.g. `triage` matches "
            "`triage.py`). Pass multiple times to whitelist several."
        ),
    )
    bench.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the artefact JSON on stdout; rely on --output.",
    )
    bench.set_defaults(func=benchmark.run)

    df = subparsers.add_parser(
        "diff",
        help=(
            "Compare two benchmark artefacts and emit a Markdown or JSON "
            "report."
        ),
    )
    df.add_argument("baseline", help="Baseline benchmark JSON artefact.")
    df.add_argument("current", help="Current benchmark JSON artefact.")
    df.add_argument(
        "--format",
        default="markdown",
        choices=["markdown", "json"],
        help="Output format. 'markdown' is the PR-comment shape (default).",
    )
    df.add_argument(
        "--thresholds",
        default=DEFAULT_THRESHOLD_NAME,
        help=(
            "Threshold preset or path to a JSON file. Presets: "
            f"{sorted(THRESHOLD_PRESETS)}."
        ),
    )
    df.add_argument(
        "--output",
        default=None,
        help="Also write the rendered report to this path.",
    )
    df.set_defaults(func=diff.run)

    tl = subparsers.add_parser(
        "tail",
        help=(
            "Stream events live from the database. Useful for "
            "watching an agent's calls + smells as they happen."
        ),
    )
    tl.add_argument("--db", default=None, help="Override the default DB path.")
    tl.add_argument(
        "--task",
        default=None,
        help="Only show events on runs whose `task` matches this value.",
    )
    tl.add_argument(
        "--since",
        default=None,
        help=(
            "Backfill events occurring within this window before tailing "
            "live (e.g. `10m`, `2h`, `7d`). Default: no backfill — only "
            "events inserted after the command starts."
        ),
    )
    tl.add_argument(
        "--poll-interval-ms",
        type=int,
        default=200,
        help="Storage poll interval in ms (default: 200).",
    )
    tl.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Exit after this many poll iterations. Mostly useful for "
            "tests and one-shot scripts; omit to run until interrupted."
        ),
    )
    tl.set_defaults(func=tail.run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
