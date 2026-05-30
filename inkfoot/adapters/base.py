"""``FrameworkAdapter`` Protocol — the framework-adapter contract.

a framework adapter wraps a third-party
agent framework (LangGraph, OpenAI Agents SDK, Anthropic Agent SDK)
so Inkfoot's ledger can see per-node attribution, tool-dispatch
metadata, and the capability surface that future modification
policies depend on.

The Protocol is :func:`~typing.runtime_checkable` so the adapter
registry can guard against partially-implemented stubs at install
time. The five members are deliberately minimal — anything richer
goes in adapter-specific subclasses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy


@runtime_checkable
class Instrumentation(Protocol):
    """Returned by :meth:`FrameworkAdapter.instrument`. Holds whatever
    teardown handle the adapter needs to undo its monkey-patches.

    Adapters are free to make this richer (e.g. a context-manager
    object that doubles as a shutdown hook). The minimum contract is
    a ``shutdown()`` method the caller can invoke at teardown.
    """

    def shutdown(self) -> None:
        """Reverse the instrument call. Idempotent."""


@runtime_checkable
class FrameworkAdapter(Protocol):
    """Pattern-C framework adapter — one per supported framework.

    Members:
      * :attr:`name` — short identifier used by the registry and CLI
        (``"langgraph"``, ``"openai_agents"``, ``"anthropic_agent"``).
        Must be unique across adapters.
      * :meth:`detect` — returns ``True`` when the framework is
        importable in the current process. The registry calls this
        to pick an adapter when the user doesn't name one explicitly.
      * :meth:`instrument` — install monkey-patches against ``target``
        and return an :class:`Instrumentation` handle. ``**kwargs``
        is reserved for adapter-specific options (e.g. opting out of
        tool-dispatch wrapping).
      * :meth:`supported_policies` — the set of :class:`Policy`
        *subclasses* this adapter supports at runtime. The current implementation ships
        only observation policies (``BudgetCap``, ``RetryThrottle``,
        ``CacheControlPlacer``) which support all three patterns;
        future modification policies (``LazyToolExposure``,
        ``CheapSummariser``) restrict to Pattern C, so the registered
        adapter's surface decides whether the policy registers.
      * :meth:`shutdown` — adapter-level teardown (e.g. clear the
        active-adapter cache). Distinct from per-instrument shutdown.
    """

    name: str

    def detect(self) -> bool:
        """Return ``True`` when the framework's import lands."""

    def instrument(self, target: Any, **kwargs: Any) -> Instrumentation:
        """Install instrumentation on ``target`` (a graph object, an
        Agent class, etc.) and return a teardown handle."""

    def supported_policies(self) -> set[type["Policy"]]:
        """Set of policy classes this adapter supports. Used by
        :func:`inkfoot.policy.register_policies` to accept or reject
        policy registrations while this adapter is active."""

    def shutdown(self) -> None:
        """Reverse the adapter-level state. Idempotent."""
