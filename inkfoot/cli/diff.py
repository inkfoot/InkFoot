"""CLI glue for ``inkfoot diff``.

Loads two benchmark artefacts, runs the comparison engine, and
renders to stdout in the requested format. Exit code mirrors the
verdict (``0`` ok / ``1`` warn / ``2`` fail). ``--format markdown``
is the default — that's what the GitHub
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

    # Optional composition: when a contracts directory is supplied, run
    # the contract check against the *current* artefact and fold its
    # output into the same report so CI posts a single sticky PR comment
    # covering both the cost diff and the contract verdict. The combined
    # exit code is the more severe of the two.
    contracts_dir = getattr(args, "contracts", None)
    contract_report = None
    if contracts_dir:
        from inkfoot.contracts.check import check_contracts  # noqa: PLC0415
        from inkfoot.contracts.loader import load_contracts  # noqa: PLC0415
        from inkfoot.contracts.schema import (  # noqa: PLC0415
            ContractValidationError,
        )

        try:
            loaded = load_contracts([contracts_dir])
        except ContractValidationError as exc:
            print(f"inkfoot diff: {exc}", file=sys.stderr)
            return 2
        contract_report = check_contracts(loaded, current)

    if fmt == "markdown":
        rendered = render_markdown(report)
        if contract_report is not None:
            from inkfoot.contracts.check import (  # noqa: PLC0415
                render_markdown as render_contract_markdown,
            )

            rendered = (
                rendered.rstrip("\n")
                + "\n\n---\n\n"
                + render_contract_markdown(contract_report)
            )
    else:
        if contract_report is not None:
            import json as _json  # noqa: PLC0415

            combined = {
                "diff": report.to_dict(),
                "contract_check": contract_report.to_dict(),
            }
            rendered = _json.dumps(combined, indent=2, sort_keys=False) + "\n"
        else:
            rendered = render_json(report) + "\n"

    exit_code = report.exit_code
    if contract_report is not None:
        exit_code = max(exit_code, contract_report.exit_code)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return exit_code
