"""`inkfoot benchmark` — scenario runner + JSON artefact (Phase 1 / E2-S1).

This package wraps three small responsibilities:

* :mod:`inkfoot.benchmark.scenario` — discovery and loading of
  ``.py`` scenario files (per phase-1-explain §4.3).
* :mod:`inkfoot.benchmark.runner` — executes ``scenario × fixture``
  under instrumentation and aggregates per-scenario stats.
* :mod:`inkfoot.benchmark.schema` — the stable JSON artefact shape
  the CLI writes and ``inkfoot diff`` consumes.

The public surface is intentionally small. Callers ordinarily hit
the CLI (``inkfoot benchmark``) and never import these modules; tests
use them directly to exercise the pieces without booting the CLI.
"""

from __future__ import annotations

from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    ScenarioResult,
    SmellCount,
)

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkArtifact",
    "ScenarioResult",
    "SmellCount",
]
