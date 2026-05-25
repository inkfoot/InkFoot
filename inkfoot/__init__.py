"""Inkfoot — causal token economics layer for LLM agents.

The package layout follows phase-0-classify.md §4. Underscore-prefixed
modules are private and not part of the SemVer contract. The public
surface — what users `from inkfoot import` — is the names in
``__all__`` below.

Phase 0 ships the foundation (storage + money + run-state). The
public callables are *declared* here from day one so the import
contract is stable across phases; the ones that ship in later epics
raise ``NotImplementedError`` with a pointer to the epic where they
land.
"""

from inkfoot._version import __version__
from inkfoot.errors import InkfootError, PolicyNotSupported, StorageError

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


def instrument(*args, **kwargs):
    """Install Pattern A monkey-patches for detected SDKs and start the
    aggregator. Ships in **E3 — Pattern A Instrumentation**.

    See phase-0-classify.md §5.1 for the contract.
    """
    raise NotImplementedError(
        "inkfoot.instrument() ships in Phase 0 epic E3 (Pattern A "
        "Instrumentation). E1 delivers the storage foundation only."
    )


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
    ``retrieved_context`` ledger field). Ships in **E2 — Causal Token
    Ledger**.
    """
    raise NotImplementedError(
        "inkfoot.tag_retrieval() ships in Phase 0 epic E2 (Causal "
        "Token Ledger)."
    )


def report_cost(*args, **kwargs):
    """Return a cost summary for one run or an aggregate. Ships in
    **E5 — Report CLI + Outcome Tagging**.
    """
    raise NotImplementedError(
        "inkfoot.report_cost() ships in Phase 0 epic E5 (Report CLI "
        "+ Outcome Tagging)."
    )
