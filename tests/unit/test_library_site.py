"""Tests for the library site generator (``cost-smells/site/build_pages.py``).

The site is generated from the smell YAML by a Jinja template. These
tests pin the two things that are easy to get subtly wrong: the
savings-conditional rendering (present vs. absent) and the Markdown-cell
escaping that keeps a query containing a pipe from breaking the table.

Skips when Jinja isn't installed (it ships in the dev extra, but the
loader probes so a minimal environment doesn't error at collection).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_PAGES = _REPO_ROOT / "cost-smells" / "site" / "build_pages.py"
_JINJA = importlib.util.find_spec("jinja2") is not None

pytestmark = pytest.mark.skipif(not _JINJA, reason="jinja2 not installed")


def _load_build_pages():
    spec = importlib.util.spec_from_file_location("build_pages", _BUILD_PAGES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_pages"] = module
    spec.loader.exec_module(module)
    return module


_bp = _load_build_pages() if _JINJA else None


def _smell(**overrides):
    base = {
        "id": "example",
        "title": "Example",
        "severity": "warn",
        "description": "An example smell.",
        "detection": {"language": "jsonpath", "query": "$.a"},
        "recommendation": "Do the thing.",
    }
    base.update(overrides)
    return _bp._with_defaults(base)


# ----------------------------------------------------------------------
# md_cell — Markdown table-cell safety (N1)
# ----------------------------------------------------------------------


def test_md_cell_escapes_pipe() -> None:
    assert _bp.md_cell("a | b") == "a \\| b"


def test_md_cell_flattens_newline() -> None:
    assert _bp.md_cell("line1\nline2") == "line1 line2"


def test_md_cell_none_is_empty() -> None:
    assert _bp.md_cell(None) == ""


def test_query_with_pipe_does_not_break_the_table() -> None:
    smell = _smell(detection={"language": "sql", "query": "SELECT a FROM x WHERE b | c"})
    page = _bp.render_page(_bp.make_env(), smell)
    query_row = next(line for line in page.splitlines() if line.startswith("| Query"))
    # The raw pipe is escaped, so the row still has exactly the two cell
    # separators of a two-column table.
    assert "\\|" in query_row
    assert query_row.count("|") - query_row.count("\\|") == 3


# ----------------------------------------------------------------------
# usd — adaptive money formatting (N2)
# ----------------------------------------------------------------------


def test_usd_zero() -> None:
    assert _bp.usd(0) == "$0.0000"


def test_usd_normal_value() -> None:
    assert _bp.usd(4_300_000) == "$0.0043"


def test_usd_tiny_nonzero_does_not_collapse_to_zero() -> None:
    assert _bp.usd(1) == "<$0.0001"


# ----------------------------------------------------------------------
# Savings-conditional rendering
# ----------------------------------------------------------------------


def test_page_without_savings_says_not_estimated() -> None:
    page = _bp.render_page(_bp.make_env(), _smell())
    assert "not yet estimated" in page
    assert "Evidence:" not in page


def test_page_with_savings_shows_evidence_and_money() -> None:
    smell = _smell(
        evidence_kind="simulation",
        estimated_savings={
            "corpus_runs": 8412,
            "triggered_in": 1240,
            "estimated_potential_saved_avg_percent": 18.7,
            "estimated_potential_saved_nanodollars_per_run": 4_300_000,
            "confidence": "medium",
            "last_computed": "2026-09-15",
        },
    )
    page = _bp.render_page(_bp.make_env(), smell)
    assert "not yet estimated" not in page
    assert "`simulation`" in page
    assert "$0.0043" in page
    assert "18.7%" in page


def test_render_index_links_each_smell() -> None:
    smells = [_smell(id="a-smell", title="A"), _smell(id="b-smell", title="B")]
    index = _bp.render_index(smells)
    assert "smells/a-smell.md" in index
    assert "smells/b-smell.md" in index
