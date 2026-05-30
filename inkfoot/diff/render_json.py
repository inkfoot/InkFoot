"""JSON renderer for ``inkfoot diff``.

This is the machine-readable counterpart to
:mod:`inkfoot.diff.render_markdown`. The shape mirrors the benchmark
artefact (per the documented diff contract: "JSON shape mirrors the benchmark
JSON, with a `delta` section added per scenario") and is consumed by:

* downstream ``jq`` queries (badge generation, dashboards),
* the GitHub Action's exit-code logic,
* tests that pin the exact shape via snapshot.

The function emits a JSON string; the CLI handles file I/O.
"""

from __future__ import annotations

import json

from inkfoot.diff.compare import DiffReport


def render_json(report: DiffReport, *, indent: int | None = 2) -> str:
    """Serialise ``report`` as JSON.

    ``indent=None`` produces a compact one-line form for pipes/
    streams; the default ``2`` is pleasant in PR artefact viewers
    and matches the benchmark artefact's indentation.
    """
    return json.dumps(report.to_dict(), indent=indent, sort_keys=False)
