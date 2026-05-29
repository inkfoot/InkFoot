"""CLI glue for ``inkfoot diff``.

Loads two benchmark artefacts, runs the comparison engine, and
renders to stdout in the requested format. Exit code mirrors the
verdict (``0`` ok / ``1`` warn / ``2`` fail) per phase-1-explain
§4.4. ``--format markdown`` is the default — that's what the GitHub
Action posts to PRs; ``--format json`` is the machine-friendly
counterpart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from inkfoot.benchmark.schema import BenchmarkArtifact, BenchmarkSchemaError
from inkfoot.diff.compare import compare_artifacts
from inkfoot.diff.render_json import render_json
from inkfoot.diff.render_markdown import render_markdown
from inkfoot.diff.thresholds import ThresholdsError, load_thresholds


_VALID_FORMATS = ("markdown", "json")


def run(args: Any) -> int:
    baseline_path = Path(getattr(args, "baseline"))
    current_path = Path(getattr(args, "current"))
    fmt = getattr(args, "format", "markdown") or "markdown"
    if fmt not in _VALID_FORMATS:
        print(
            f"inkfoot diff: invalid --format {fmt!r}; expected one of "
            f"{_VALID_FORMATS}",
            file=sys.stderr,
        )
        return 2
    thresholds_name = getattr(args, "thresholds", None)
    output_path = (
        Path(args.output) if getattr(args, "output", None) else None
    )

    try:
        baseline = BenchmarkArtifact.load(baseline_path)
        current = BenchmarkArtifact.load(current_path)
    except (FileNotFoundError, BenchmarkSchemaError) as exc:
        print(f"inkfoot diff: {exc}", file=sys.stderr)
        return 2

    try:
        thresholds = load_thresholds(thresholds_name)
    except ThresholdsError as exc:
        print(f"inkfoot diff: {exc}", file=sys.stderr)
        return 2

    try:
        report = compare_artifacts(baseline, current, thresholds=thresholds)
    except ValueError as exc:
        print(f"inkfoot diff: {exc}", file=sys.stderr)
        return 2

    if fmt == "markdown":
        rendered = render_markdown(report)
    else:
        rendered = render_json(report) + "\n"

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return report.exit_code
