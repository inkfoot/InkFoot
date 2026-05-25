"""Inkfoot — causal token economics layer for LLM agents.

The package layout follows phase-0-classify.md §4. Underscore-prefixed
modules are private and not part of the SemVer contract. The public
surface — what users ``from inkfoot import`` — is the names in
``__all__`` below.

Phase 0 progress:

* E1 (storage foundation + money type + CLI) — landed.
* E2 (Causal Token Ledger + per-provider translators + pricing) — landed.
* E3 (``inkfoot.instrument()`` + Pattern A SDK shims + policy
  registry + 3 observation policies) — landed.
* E4 / E5 / E6 — not yet shipped. The public callables they
  add (``agent_run``, ``set_outcome``, ``tag``, ``tag_retrieval``,
  ``report_cost``) are *declared* here so the import contract is
  stable across phases; they raise ``NotImplementedError`` with a
  pointer to the epic where they land.

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
    report_cost,
    set_outcome,
    tag,
    tag_retrieval,
)

__all__ = [
    "__version__",
    "instrument",
    "agent_run",
    "set_outcome",
    "tag",
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
