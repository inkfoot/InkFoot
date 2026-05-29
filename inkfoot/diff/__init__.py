"""`inkfoot diff` — structural comparison of two benchmark artefacts.

Public surface:

* :func:`compare_artifacts` — pure comparison; returns a
  :class:`DiffReport`.
* :func:`render_markdown` — PR-comment renderer.
* :func:`render_json` — machine-readable renderer for CI consumers.
* :data:`Thresholds`, :func:`load_thresholds` — verdict thresholds.

The CLI lives in :mod:`inkfoot.cli.diff` and is a thin wrapper that
parses argv, loads two JSONs, and dispatches into the renderers
above.
"""

from __future__ import annotations

from inkfoot.diff.compare import (
    DiffReport,
    ScenarioDiff,
    Verdict,
    compare_artifacts,
)
from inkfoot.diff.render_json import render_json
from inkfoot.diff.render_markdown import render_markdown
from inkfoot.diff.thresholds import (
    DEFAULT_THRESHOLD_NAME,
    THRESHOLD_PRESETS,
    Thresholds,
    load_thresholds,
)

__all__ = [
    "DiffReport",
    "ScenarioDiff",
    "Verdict",
    "compare_artifacts",
    "render_markdown",
    "render_json",
    "Thresholds",
    "load_thresholds",
    "THRESHOLD_PRESETS",
    "DEFAULT_THRESHOLD_NAME",
]
