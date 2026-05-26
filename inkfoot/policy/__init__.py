"""The ``policy`` package — Policy ABC, capability matrix, and the
``PolicyDecision`` / ``CallContext`` shapes that flow through every
shim.

Phase 0 ships three *observation* policies (``BudgetCap``,
``RetryThrottle``, ``CacheControlPlacer``) — none of them block calls
or rewrite requests (per ADR-0-2's "Phase 0 is observe-only"
posture). Phase 2 introduces *modification* policies
(``LazyToolExposure``, ``CheapSummariser``) that require a framework
adapter (Pattern C); registering them on Pattern A raises
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
]


class IntegrationPattern(Enum):
    """The three integration shapes Inkfoot supports.

    * ``A`` — SDK shim (monkey-patch ``anthropic.*`` or
      ``openai.*`` directly). Ships in Phase 0.
    * ``B`` — raw-SDK decorator + run context manager. Internal in
      Phase 0; public in Phase 1.
    * ``C`` — framework adapter (LangGraph, OpenAI Agents SDK,
      Anthropic Agent SDK, ...). Ships in Phase 1.
    """

    A = "A"
    B = "B"
    C = "C"


@dataclass
class CallContext:
    """Mutable per-call context handed to every policy hook.

    Carries enough state for an observation policy to decide whether
    to ``warn`` or ``block`` (Phase 0 never blocks) and to attribute
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

    Phase 0 only ever uses ``allow`` (the policy did nothing) and
    ``warn`` (the policy wants to emit an event but the call goes
    through). ``block`` is wired so Phase 2's ``ContractEnforcer``
    can refuse a call when ``BudgetCap`` enforcement turns on.
    """

    action: Literal["allow", "warn", "block"] = "allow"
    reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-policy event name to emit when ``action != "allow"``.
    # The shim writes a row of this kind into the events table.
    emit_event_kind: Optional[str] = None


class Policy(ABC):
    """Base class for every Phase 0 + Phase 2 policy.

    Subclasses declare their supported integration patterns via the
    class-level :attr:`SUPPORTED_PATTERNS`. The default is "all three"
    — observation policies that work everywhere don't need to
    override. Modification policies (Phase 2) restrict to
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

    The message is the customer-visible bit (§5.8) — it names what
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

    Phase 0 ships only observation policies that work everywhere; the
    early-exit on ``IntegrationPattern.C ∈ policy.SUPPORTED_PATTERNS``
    handles them without consulting the adapter. Phase 2's
    modification policies (``LazyToolExposure``, ``CheapSummariser``)
    will land with ``SUPPORTED_PATTERNS = {C}`` — for those, the
    adapter has to *also* declare the class in its
    ``supported_policies()`` set, otherwise it doesn't know how to
    wire the policy into the framework.
    """
    supported_classes = adapter.supported_policies()
    if type(policy) in supported_classes:
        return

    # Phase-0 observation policies declare ``{A, B, C}`` — they work
    # without adapter-specific wiring (the shim handles them). Let
    # them through on the legacy pattern check so a Pattern-C adapter
    # that hasn't enumerated them doesn't accidentally reject the
    # BudgetCap users already had registered.
    #
    # A Phase-2 *modification* policy ships with ``{C}`` only — those
    # need the adapter to know how to wire them, so the explicit
    # enumeration is mandatory and the fallback path doesn't fire.
    patterns = policy.SUPPORTED_PATTERNS
    if (
        IntegrationPattern.A in patterns
        or IntegrationPattern.B in patterns
    ) and IntegrationPattern.C in patterns:
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
        f"  Fix: either upgrade the adapter (Phase 2 ships richer "
        f"capability surfaces) or drop the policy from this run.\n"
        f"  See: {docs_url}/{policy_name.lower() or 'index'}"
    )


# Re-imports at the bottom to avoid circulars with the registry +
# concrete policy classes. Both modules ``from inkfoot.policy import
# Policy`` — they're not imported here until needed.
from inkfoot.policy.budget_cap import BudgetCap  # noqa: E402
from inkfoot.policy.cache_control_placer import CacheControlPlacer  # noqa: E402
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
