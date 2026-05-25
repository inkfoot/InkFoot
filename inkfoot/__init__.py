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


def agent_run(*args, **kwargs):
    """Context manager / decorator that scopes events to a run.
    Ships in **E5 — Report CLI + Outcome Tagging**.
    """
    raise NotImplementedError(
        "inkfoot.agent_run() ships in Phase 0 epic E5 (Report CLI + "
        "Outcome Tagging). E1 delivers the storage foundation only."
    )


def set_outcome(*args, **kwargs):
    """Mark the current run's outcome. Ships in **E5**."""
    raise NotImplementedError(
        "inkfoot.set_outcome() ships in Phase 0 epic E5 (Report CLI + "
        "Outcome Tagging)."
    )


def tag(*args, **kwargs):
    """Attach a free-form tag to the current run. Ships in **E5**."""
    raise NotImplementedError(
        "inkfoot.tag() ships in Phase 0 epic E5 (Report CLI + Outcome "
        "Tagging)."
    )


def tag_retrieval(*args, **kwargs):
    """Mark a span of messages as retrieved context (lifts the
    ``CausalTokenLedger.retrieved_context_tokens`` field). Ships in
    **E5 — Report CLI + Outcome Tagging** alongside ``set_outcome`` /
    ``tag``. The underlying ledger field exists today (E2) but no
    translator populates it until the E5 marker API lands.
    """
    raise NotImplementedError(
        "inkfoot.tag_retrieval() ships in Phase 0 epic E5 (Report CLI "
        "+ Outcome Tagging)."
    )


def report_cost(*args, **kwargs):
    """Return a cost summary for one run or an aggregate. Ships in
    **E5 — Report CLI + Outcome Tagging**.
    """
    raise NotImplementedError(
        "inkfoot.report_cost() ships in Phase 0 epic E5 (Report CLI "
        "+ Outcome Tagging)."
    )
