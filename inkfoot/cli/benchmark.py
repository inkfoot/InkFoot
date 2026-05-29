"""CLI glue for ``inkfoot benchmark``.

A thin argparse-friendly wrapper around
:func:`inkfoot.benchmark.runner.run_benchmark`. The CLI emits the
artefact JSON (default: stdout; ``--output`` writes a file too) and
exits ``0`` on a successful benchmark, ``1`` if the discovery walk
finds zero scenarios, ``2`` on scenario load / runtime errors.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from inkfoot.benchmark.runner import run_benchmark
from inkfoot.benchmark.scenario import ScenarioLoadError


def run(args: Any) -> int:
    scenarios_dir = Path(getattr(args, "scenarios_dir"))
    output: Path | None = (
        Path(args.output) if getattr(args, "output", None) else None
    )
    scenarios_only_raw = getattr(args, "scenarios_only", None) or []
    scenarios_only = [name.strip() for name in scenarios_only_raw if name.strip()]
    quiet = bool(getattr(args, "quiet", False))

    try:
        artefact = run_benchmark(
            scenarios_dir,
            output=output,
            scenarios_only=scenarios_only or None,
        )
    except FileNotFoundError as exc:
        print(f"inkfoot benchmark: {exc}", file=sys.stderr)
        return 2
    except ScenarioLoadError as exc:
        print(f"inkfoot benchmark: {exc}", file=sys.stderr)
        return 2

    if not quiet:
        # Emit JSON on stdout so CI scripts can pipe it. ``--output``
        # is the durable copy; stdout is the inspection aid.
        sys.stdout.write(artefact.to_json() + "\n")

    if not artefact.scenarios:
        print(
            f"inkfoot benchmark: no scenarios discovered under {scenarios_dir}",
            file=sys.stderr,
        )
        return 1
    return 0
