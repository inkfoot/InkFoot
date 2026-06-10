"""CLI glue for ``inkfoot contract`` — ``draft`` and ``check``.

``draft`` reads a task's run history and writes a starting-point
contract YAML. ``check`` evaluates a directory of contracts against a
benchmark artefact and exits with the CI verdict code (``0`` ok / ``1``
warn / ``2`` violation), mirroring ``inkfoot diff``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from inkfoot.benchmark.schema import BenchmarkArtifact, BenchmarkSchemaError
from inkfoot.contracts.check import check_contracts, render_json, render_markdown
from inkfoot.contracts.draft import (
    DraftError,
    build_draft,
    collect_run_facts,
    parse_window,
)
from inkfoot.contracts.loader import load_contracts
from inkfoot.contracts.schema import ContractValidationError

_VALID_FORMATS = ("markdown", "json")


def run_draft(args: Any) -> int:
    task = getattr(args, "task", None)
    if not task:
        print("inkfoot contract draft: --task is required", file=sys.stderr)
        return 2
    window = getattr(args, "window", None) or "30d"
    from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path  # noqa: PLC0415

    db_path = Path(args.db) if getattr(args, "db", None) else _default_db_path()
    storage = SQLiteStorage(path=db_path)
    storage.connect()
    try:
        window_seconds = parse_window(window)
        facts = collect_run_facts(storage, task, window_seconds)
        result = build_draft(task, window, facts)
    except DraftError as exc:
        print(f"inkfoot contract draft: {exc}", file=sys.stderr)
        return 2
    finally:
        close = getattr(storage, "close", None)
        if callable(close):
            close()

    output_path = Path(args.output) if getattr(args, "output", None) else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.yaml_text, encoding="utf-8")
        print(
            f"Wrote draft contract for {task!r} to {output_path} "
            f"({result.run_count} run(s) over {window}).",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(result.yaml_text)
    return 0


def run_check(args: Any) -> int:
    contracts_dir = getattr(args, "contracts", None) or "."
    against = getattr(args, "against", None)
    fmt = getattr(args, "format", "markdown") or "markdown"
    if fmt not in _VALID_FORMATS:
        print(
            f"inkfoot contract check: invalid --format {fmt!r}; expected one "
            f"of {_VALID_FORMATS}",
            file=sys.stderr,
        )
        return 2
    if not against:
        print(
            "inkfoot contract check: --against <benchmark.json> is required",
            file=sys.stderr,
        )
        return 2

    try:
        contracts = load_contracts([contracts_dir])
    except ContractValidationError as exc:
        print(f"inkfoot contract check: {exc}", file=sys.stderr)
        return 2

    try:
        artifact = BenchmarkArtifact.load(Path(against))
    except (FileNotFoundError, BenchmarkSchemaError) as exc:
        print(f"inkfoot contract check: {exc}", file=sys.stderr)
        return 2

    report = check_contracts(contracts, artifact)
    rendered = (
        render_markdown(report) if fmt == "markdown" else render_json(report) + "\n"
    )
    output_path = Path(args.output) if getattr(args, "output", None) else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return report.exit_code


def run(args: Any) -> int:
    """Dispatch to the chosen ``contract`` subcommand."""
    sub = getattr(args, "contract_command", None)
    if sub == "draft":
        return run_draft(args)
    if sub == "check":
        return run_check(args)
    print(
        "inkfoot contract: choose a subcommand (draft, check). "
        "See `inkfoot contract --help`.",
        file=sys.stderr,
    )
    return 2
