"""Docs site build gate.

These tests invoke ``mkdocs build --strict`` against the docs
tree in this repository so a broken internal link, an orphan
file, or a missing nav target trips CI rather than landing in a
shipped release.

The tests skip when the ``mkdocs`` toolchain isn't installed —
the docs extra (`pip install -e ".[docs]"`) is optional in the
main dev install and the CI workflow installs it explicitly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_YAML = REPO_ROOT / "mkdocs.yml"


def _mkdocs_available() -> bool:
    """Check the import surface rather than the binary path.

    ``shutil.which`` doesn't see the venv binary when pytest runs
    as a module; an import probe matches the actual execution path
    of ``python -m mkdocs build`` further down."""
    return importlib.util.find_spec("mkdocs") is not None


pytestmark = pytest.mark.skipif(
    not _mkdocs_available(),
    reason="mkdocs not installed (install with: pip install -e \".[docs]\")",
)


def test_docs_tree_exists():
    """Sanity check: the docs site lives where the build expects it."""
    assert MKDOCS_YAML.exists(), f"missing {MKDOCS_YAML}"
    assert DOCS_DIR.exists(), f"missing {DOCS_DIR}"
    assert (DOCS_DIR / "index.md").exists(), "missing docs/index.md"
    assert (DOCS_DIR / "quickstart.md").exists(), "missing docs/quickstart.md"


def test_required_pages_exist():
    """The pages enumerated in the docs spec must each be present.

    Renaming or relocating one would surface as a broken nav link
    via ``mkdocs build --strict`` further down, but listing them
    explicitly gives a clearer error message when a maintainer
    deletes a required page by accident."""
    required = [
        # Concepts
        "concepts/causal-token-ledger.md",
        "concepts/cost-smells.md",
        "concepts/accuracy.md",
        "concepts/otel.md",
        # Recipes
        "recipes/find-expensive-agent.md",
        "recipes/spot-cache-misses.md",
        "recipes/set-up-ci.md",
        "recipes/otel-honeycomb.md",
        # Framework guides
        "frameworks/langgraph.md",
        "frameworks/openai-agents.md",
        "frameworks/anthropic-agent.md",
        "frameworks/raw-sdk.md",
        # Reference
        "reference/cli.md",
        "reference/api.md",
    ]
    missing = [p for p in required if not (DOCS_DIR / p).exists()]
    assert not missing, f"missing required docs pages: {missing}"


def test_strict_build_succeeds(tmp_path):
    """``mkdocs build --strict`` must complete with zero warnings.

    The ``--strict`` flag upgrades broken links, orphan files, and
    missing nav targets to errors. Any of those land here as a
    non-zero exit code that surfaces in the test failure message
    instead of as a silent 404 on the deployed site."""
    site_dir = tmp_path / "_site"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"mkdocs build --strict failed (exit {result.returncode}).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    # The build is a destructive operation against ``site_dir``; if
    # we got this far, the index landed.
    assert (site_dir / "index.html").exists()
