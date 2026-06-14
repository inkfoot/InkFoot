#!/usr/bin/env python3
"""Render the library site's per-smell pages from the smell YAML files.

Reads every ``smells/*.yaml``, renders one Markdown page per smell from
``_template/smell.md.j2``, and writes a catalogue index. The deploy
workflow runs this immediately before ``mkdocs build``; the generated
pages under ``site/docs/smells/`` are build artefacts and are not
committed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

SITE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SITE_DIR.parent
SMELLS_DIR = REPO_ROOT / "smells"
TEMPLATE_DIR = SITE_DIR / "_template"
OUTPUT_DIR = SITE_DIR / "docs" / "smells"

_SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


def page_name(smell_id: str) -> str:
    """Filesystem-safe page name for a smell id (namespaced ids carry a '/')."""
    return smell_id.replace("/", "__")


def md_cell(value: Any) -> str:
    """Make a value safe inside a Markdown table cell.

    A detection query may contain a pipe or a newline; either would break
    the table layout (and ``--strict`` doesn't catch a malformed table).
    Escape pipes and flatten newlines so any query renders as one cell.
    """
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|")


def usd(nanodollars: Any) -> str:
    """Format nanodollars as USD without collapsing tiny sums to $0.0000."""
    dollars = (nanodollars or 0) / 1_000_000_000
    if dollars == 0:
        return "$0.0000"
    if abs(dollars) < 0.0001:
        return "<$0.0001"
    return f"${dollars:.4f}"


def make_env() -> Environment:
    """Build the Jinja environment with the template filters registered."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["cell"] = md_cell
    env.filters["usd"] = usd
    return env


def _with_defaults(smell: dict[str, Any]) -> dict[str, Any]:
    """Fill optional keys so StrictUndefined still catches real typos
    while absent reserved/optional fields render as 'not set'."""
    smell.setdefault("suggested_policy", None)
    smell.setdefault("primary_category", None)
    smell.setdefault("evidence_query", None)
    smell.setdefault("evidence_kind", None)
    smell.setdefault("estimated_savings", None)
    smell.get("detection", {}).setdefault("trigger_condition", None)
    return smell


def load_smells(smells_dir: Path = SMELLS_DIR) -> list[dict[str, Any]]:
    """Load every smell YAML, sorted by id for deterministic output."""
    smells = [
        _with_defaults(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(smells_dir.glob("*.yaml"))
    ]
    return sorted(smells, key=lambda s: s["id"])


def render_page(env: Environment, smell: dict[str, Any]) -> str:
    """Render a single smell's page."""
    return env.get_template("smell.md.j2").render(smell=smell)


def render_index(smells: list[dict[str, Any]]) -> str:
    """Render the catalogue index, grouped by severity."""
    lines = [
        "# Cost smell library",
        "",
        f"{len(smells)} named cost smells. Each is a pattern in an agent run's "
        "token attribution that usually means wasted money.",
        "",
        "| Smell | Severity | Suggested policy | Savings |",
        "|---|---|---|---|",
    ]
    for smell in sorted(
        smells, key=lambda s: (_SEVERITY_ORDER.get(s["severity"], 9), s["id"])
    ):
        policy = smell.get("suggested_policy")
        policy_cell = f"`{policy}`" if policy else "—"
        savings = "estimated" if smell.get("estimated_savings") else "not yet estimated"
        link = f"smells/{page_name(smell['id'])}.md"
        lines.append(
            f"| [{smell['title']}]({link}) | {smell['severity']} "
            f"| {policy_cell} | {savings} |"
        )
    lines.append("")
    return "\n".join(lines)


def build(output_dir: Path = OUTPUT_DIR) -> int:
    """Render all pages and the index. Returns the number of smell pages."""
    env = make_env()
    smells = load_smells()
    output_dir.mkdir(parents=True, exist_ok=True)

    for smell in smells:
        page = render_page(env, smell)
        (output_dir / f"{page_name(smell['id'])}.md").write_text(
            page, encoding="utf-8"
        )

    (output_dir.parent / "catalogue.md").write_text(
        render_index(smells), encoding="utf-8"
    )
    return len(smells)


def main() -> int:
    count = build()
    print(f"rendered {count} smell page(s) to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
