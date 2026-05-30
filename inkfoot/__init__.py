"""Inkfoot — causal token economics layer for LLM agents.

Underscore-prefixed modules are private and not part of the SemVer
contract. The public surface — what users ``from inkfoot import`` —
is the names in ``__all__`` below.

Implementation internals such as ``CausalTokenLedger``,
``NeutralCall``, the translators, shim classes, and policy classes are
intentionally *not* re-exported on the top-level package. They live
behind ``inkfoot.ledger`` / ``inkfoot.normalise`` /
``inkfoot.pricing`` / ``inkfoot.shims`` / ``inkfoot.policy`` to keep
the user-facing surface tight. The exception is
:class:`inkfoot.policy.BudgetCap` / ``RetryThrottle`` /
``CacheControlPlacer`` which users import to pass into
:func:`instrument`.
"""

from inkfoot._version import __version__
from inkfoot.errors import InkfootError, PolicyNotSupported, StorageError
from inkfoot._instrument import instrument
from inkfoot._run_lifecycle import (
    agent_run,
    checkpoint,
    report_cost,
    set_outcome,
    tag,
    tag_node,
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
# ``inkfoot._run_lifecycle`` above. The previous stubs that raised
# ``NotImplementedError`` are gone.
