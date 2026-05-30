"""Inkfoot — causal token economics layer for LLM agents.

The package layout follows phase-0-classify.md §4. Underscore-prefixed
modules are private and not part of the SemVer contract. The public
surface — what users ``from inkfoot import`` — is the names in
``__all__`` below.

Phase 0 (code-complete):

* E1 — storage foundation + money type + CLI.
* E2 — Causal Token Ledger + per-provider translators + pricing.
* E3 — ``inkfoot.instrument()`` + Pattern A SDK shims + policy
  registry + 3 observation policies.
* E4 — Smell engine + 5 built-in smells.
* E5 — ``@agent_run`` decorator/context manager, ``set_outcome``,
  ``tag``, ``tag_retrieval``, ``report_cost``, and ``inkfoot report``
  CLI (single-run + aggregate).
* E6 — Validation harness + corpus + perf benchmarks + CI gate.

Phase 1 progress:

* E1 — Framework adapter foundation (Pattern C). Ships:
  ``inkfoot.langgraph.instrument`` / ``inkfoot.openai_agents.instrument``
  / ``inkfoot.anthropic_agent.instrument``, plus the Pattern-B
  ergonomic helpers ``inkfoot.tag_node`` / ``inkfoot.checkpoint``.
  ``inkfoot report --run <id> --group-by node`` shows per-node
  ledger totals when a Pattern-C adapter is wired in.
* E2 — Benchmark + Diff + GitHub Action. Ships:
  ``inkfoot benchmark`` (scenario runner with stable JSON artefact),
  ``inkfoot diff`` (Markdown / JSON regression report with
  ``ok|warn|fail`` verdict + exit codes), and the composite
  ``inkfoot/diff-action`` GitHub Action (sticky PR comment via the
  hidden ``<!-- inkfoot-diff-action -->`` marker). Modules:
  ``inkfoot.benchmark`` and ``inkfoot.diff``.
* E3 — OpenTelemetry ingest + export. Ships
  ``inkfoot.instrument(otel_ingest_port=..., otel_export_endpoint=...)``
  and the :mod:`inkfoot.otel` package (pinned GenAI conventions
  v1.27.0, bidirectional mapping, stdlib-only OTLP/JSON listener
  with ADR-1-2 ``(span_id, response_id)`` dedup, batched
  background exporter that WARN-and-continues on collector
  failures).
* E4 / E5 / E6 — not yet shipped.

E2 + E3 internals (``CausalTokenLedger``, ``NeutralCall``,
``AnthropicTranslator``, ``OpenAITranslator``,
``estimate_nanodollars``, the shim classes, the policy classes) are
intentionally *not* re-exported on the top-level package — they
live behind ``inkfoot.ledger`` / ``inkfoot.normalise`` /
``inkfoot.pricing`` / ``inkfoot.shims`` / ``inkfoot.policy`` to keep
the user-facing surface tight (see architecture §6). The exception
is :class:`inkfoot.policy.BudgetCap` / ``RetryThrottle`` /
``CacheControlPlacer`` which users *do* import to pass into
:func:`instrument`.
"""

from inkfoot._version import __version__
from inkfoot.errors import InkfootError, PolicyNotSupported, StorageError
from inkfoot._instrument import instrument  # E3 — Pattern A Instrumentation
from inkfoot._run_lifecycle import (  # E5 — Report CLI + Outcome Tagging
    agent_run,
    checkpoint,  # Phase 1 / E1-S5
    report_cost,
    set_outcome,
    tag,
    tag_node,  # Phase 1 / E1-S5
    tag_retrieval,
)

__all__ = [
    "__version__",
    "instrument",
    "agent_run",
    "checkpoint",
    "set_outcome",
    "tag",
    "tag_node",
    "tag_retrieval",
    "report_cost",
    "InkfootError",
    "PolicyNotSupported",
    "StorageError",
]


# ``agent_run`` / ``set_outcome`` / ``tag`` / ``tag_retrieval`` /
# ``report_cost`` are real callables imported from
# ``inkfoot._run_lifecycle`` above (E5). The previous stubs that
# raised ``NotImplementedError`` are gone.
