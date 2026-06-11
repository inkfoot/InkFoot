"""The ``policy`` package — Policy ABC, capability matrix, and the
``PolicyDecision`` / ``CallContext`` shapes that flow through every
shim.

The package ships two kinds of policy. The three *observation*
policies (``BudgetCap``, ``RetryThrottle``, ``CacheControlPlacer``)
never block calls or rewrite requests and work under every
integration pattern. The two *modification* policies
(``LazyToolExposure``, ``CheapSummariser``) rewrite the outgoing
request and therefore require a framework adapter (Pattern C);
registering them on Pattern A or B raises
:class:`~inkfoot.errors.PolicyNotSupported` with a remediation hint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Iterable, Literal, Optional

from inkfoot.errors import PolicyNotSupported


__all__ = [
    "IntegrationPattern",
    "Policy",
    "PolicyDecision",
    "CallContext",
    "register_policies",
    # Observation policies (re-exported for the public surface).
    "BudgetCap",
    "RetryThrottle",
    "CacheControlPlacer",
    # Modification policies (framework adapters only).
    "LazyToolExposure",
    "CheapSummariser",
]


class IntegrationPattern(Enum):
    """The three integration shapes Inkfoot supports.

    * ``A`` — SDK shim (monkey-patch ``anthropic.*`` or
      ``openai.*`` directly).
    * ``B`` — raw-SDK decorator + run context manager. Internal in
      Raw-SDK decorator + run context manager.
    * ``C`` — framework adapter (LangGraph, OpenAI Agents SDK,
      Anthropic Agent SDK, ...).
    """

    A = "A"
    B = "B"
    C = "C"


@dataclass
class CallContext:
    """Mutable per-call context handed to every policy hook.

    Carries enough state for an observation policy to decide whether
    to ``warn`` or ``block`` (the current implementation never blocks) and to attribute
    the decision back to the call. Mutable so a ``before_call`` hook
    can stash metadata for ``after_call`` to read.
    """

    provider: str
    model: str
    run_id: str
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    estimated_nanodollars: Optional[int] = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """What a policy's ``before_call`` returns.

    The current implementation only ever uses ``allow`` (the policy did nothing) and
    ``warn`` (the policy wants to emit an event but the call goes
    through). ``block`` is wired so a future ``ContractEnforcer``
    can refuse a call when ``BudgetCap`` enforcement turns on.
    """

    action: Literal["allow", "warn", "block"] = "allow"
    reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-policy event name to emit when ``action != "allow"``.
    # The shim writes a row of this kind into the events table.
    emit_event_kind: Optional[str] = None


class Policy(ABC):
    """Base class for every policy.

    Subclasses declare their supported integration patterns via the
    class-level :attr:`SUPPORTED_PATTERNS`. The default is "all three"
    — observation policies that work everywhere don't need to
    override. Modification policies (a future release) restrict to
    ``{IntegrationPattern.C}``.

    Both hooks must be implemented but may no-op. They are wrapped
    in :func:`inkfoot.shims._isolation.isolated_hook` at the shim
    boundary, so raising from inside is *contained* — the user's
    LLM call always returns the original SDK response.
    """

    NAME: ClassVar[str] = ""
    SUPPORTED_PATTERNS: ClassVar[set[IntegrationPattern]] = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }
    DOCS_URL: ClassVar[str] = "https://inkfoot.dev/docs/policies"

    @abstractmethod
    def before_call(self, ctx: CallContext) -> PolicyDecision:
        """Called *before* the SDK call is invoked. Return
        ``PolicyDecision(action="allow")`` to no-op."""

    @abstractmethod
    def after_call(self, ctx: CallContext, response: Any) -> None:
        """Called *after* the SDK call returns. Used to update
        per-policy state (e.g. ``RetryThrottle`` updating its
        counter) and to emit follow-up events (e.g.
        ``CacheControlPlacer`` emitting cache-advice when the hit
        ratio doesn't improve)."""


def _check_supports(
    policy: Policy, active_pattern: IntegrationPattern
) -> None:
    """Raise :class:`PolicyNotSupported` with a remediation hint
    when ``policy`` doesn't support ``active_pattern``.

    The message is the customer-visible bit — it names what
    they tried, what's active, and how to fix it.
    """
    supported = policy.SUPPORTED_PATTERNS
    if active_pattern in supported:
        return

    policy_name = policy.NAME or type(policy).__name__
    docs_url = getattr(policy, "DOCS_URL", "https://inkfoot.dev/docs/policies")
    supported_names = ", ".join(p.name for p in sorted(supported, key=lambda p: p.value))
    raise PolicyNotSupported(
        f"{policy_name} requires integration pattern(s) "
        f"{{{supported_names}}}.\n"
        f"  Active integration: Pattern {active_pattern.value} "
        f"(SDK shim).\n"
        f"  Fix: install inkfoot[<framework>] and call the framework "
        f"adapter's instrument(...) instead of inkfoot.instrument().\n"
        f"  See: {docs_url}/{policy_name.lower() or 'index'}"
    )


def _check_adapter_supports(policy: Policy, adapter: Any) -> None:
    """Pattern-C path: when a framework adapter is active, the
    adapter's ``supported_policies()`` is the source of truth — the
    static :attr:`SUPPORTED_PATTERNS` on the policy class doesn't
    enumerate which adapters know how to wire it.

    Modification policies (``LazyToolExposure``, ``CheapSummariser``)
    declare ``SUPPORTED_PATTERNS = {C}``: the adapter has to declare
    the class in its ``supported_policies()`` set, otherwise it
    doesn't know how to wire the policy into the framework.
    Observation policies declare every pattern and pass the full-set
    fallback below without adapter enumeration.
    """
    supported_classes = adapter.supported_policies()
    if type(policy) in supported_classes:
        return

    # The fallback path: a policy that declares it works across
    # *every* integration pattern (``{A, B, C}``) — the
    # observation-policy shape — doesn't need adapter-specific
    # wiring. The shim handles it identically under any adapter, so
    # an adapter that hasn't enumerated ``BudgetCap`` / ``RetryThrottle``
    # / ``CacheControlPlacer`` still accepts the registration.
    #
    # Anything narrower than the full set (``{C}``, ``{B, C}``,
    # ``{A, C}``, ...) is a policy that needs the adapter to know
    # how to wire it — a hypothetical modification policy declared
    # ``{B, C}`` ("works in raw decorator + framework adapter, but
    # not under the bare SDK shim") should NOT silently pass a
    # registration against an adapter that hasn't enumerated it.
    # Hence ``equals {A, B, C}`` rather than a looser has-C check.
    if policy.SUPPORTED_PATTERNS == {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }:
        return

    policy_name = policy.NAME or type(policy).__name__
    docs_url = getattr(policy, "DOCS_URL", "https://inkfoot.dev/docs/policies")
    adapter_name = getattr(adapter, "name", type(adapter).__name__)
    supported_names = ", ".join(
        sorted(cls.__name__ for cls in supported_classes)
    ) or "(none)"
    raise PolicyNotSupported(
        f"{policy_name} is not supported by the {adapter_name!r} adapter.\n"
        f"  Adapter supports: {{{supported_names}}}.\n"
        f"  Fix: either upgrade the adapter (future versions ship richer "
        f"capability surfaces) or drop the policy from this run.\n"
        f"  See: {docs_url}/{policy_name.lower() or 'index'}"
    )


# Re-imports at the bottom to avoid circulars with the registry +
# concrete policy classes. Both modules ``from inkfoot.policy import
# Policy`` — they're not imported here until needed.
from inkfoot.policy.budget_cap import BudgetCap  # noqa: E402
from inkfoot.policy.cache_control_placer import CacheControlPlacer  # noqa: E402
from inkfoot.policy.cheap_summariser import CheapSummariser  # noqa: E402
from inkfoot.policy.lazy_tool_exposure import LazyToolExposure  # noqa: E402
from inkfoot.policy.retry_throttle import RetryThrottle  # noqa: E402


def register_policies(
    policies: Iterable[Policy],
    *,
    active_pattern: IntegrationPattern = IntegrationPattern.A,
    adapter: Optional[Any] = None,
) -> None:
    """Validate every policy against the active integration's
    capability surface and add it to the global registry.

    When ``adapter`` is set (Pattern-C path), the adapter's
    ``supported_policies()`` is the authoritative gate. Otherwise the
    legacy ``IntegrationPattern`` check on the policy class applies
    (Pattern A SDK shim, Pattern B raw decorator).

    Idempotent over distinct objects but *not* deduplicating —
    passing the same policy instance twice registers it once (the
    registry collapses by identity); passing two ``BudgetCap``
    instances registers both.
    """
    from inkfoot.policy.registry import PolicyRegistry

    # The adapter param wins over active_pattern when both are given;
    # the adapter knows more about the runtime than the pattern enum.
    use_adapter = adapter is not None
    if not use_adapter:
        # Auto-detect: when an adapter has been activated via
        # ``inkfoot.adapters.AdapterRegistry.set_active`` the policy
        # registration should consult it without callers threading
        # the parameter through every layer.
        from inkfoot.adapters._registry import (  # noqa: PLC0415
            get_active_adapter,
        )

        active_adapter = get_active_adapter()
        if active_adapter is not None:
            adapter = active_adapter
            use_adapter = True

    for policy in policies:
        if use_adapter:
            _check_adapter_supports(policy, adapter)
        else:
            _check_supports(policy, active_pattern)
        PolicyRegistry.add(policy)
