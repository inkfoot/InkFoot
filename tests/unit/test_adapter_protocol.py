"""E1-S1 — FrameworkAdapter Protocol + adapter registry.

Tests cover:
  * Protocol conformance via ``runtime_checkable``.
  * Registry duplicate-name rejection + re-registration of the same
    instance.
  * Auto-detection of an active adapter inside
    :func:`register_policies` so Phase-2 modification policies that
    only Pattern C supports can register cleanly.
  * Activate/deactivate plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest

from inkfoot.adapters import (
    AdapterRegistry,
    DuplicateAdapterName,
    FrameworkAdapter,
    Instrumentation,
    get_active_adapter,
)
from inkfoot.adapters.base import FrameworkAdapter as Proto
from inkfoot.errors import PolicyNotSupported
from inkfoot.policy import (
    BudgetCap,
    IntegrationPattern,
    Policy,
    PolicyDecision,
    register_policies,
)
from inkfoot.policy.registry import PolicyRegistry


class _StubInstrumentation:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _StubAdapter:
    """Minimal Protocol-satisfying adapter for tests."""

    name = "stub"

    def __init__(self, supports: set[type[Policy]] | None = None) -> None:
        self._supports = supports or set()
        self._shutdown_calls = 0
        self.last_instrumentation: _StubInstrumentation | None = None

    def detect(self) -> bool:
        return True

    def instrument(self, target: Any, **kwargs: Any) -> Instrumentation:
        self.last_instrumentation = _StubInstrumentation()
        return self.last_instrumentation

    def supported_policies(self) -> set[type[Policy]]:
        return set(self._supports)

    def shutdown(self) -> None:
        self._shutdown_calls += 1


class _FakeLazyToolExposure(Policy):
    """Stand-in for the Phase-2 modification policy that only Pattern
    C supports. Mirrors the production class's SUPPORTED_PATTERNS so
    the auto-detect path's tests are realistic."""

    NAME = "LazyToolExposure"
    SUPPORTED_PATTERNS = {IntegrationPattern.C}

    def before_call(self, ctx: Any) -> PolicyDecision:  # pragma: no cover
        return PolicyDecision(action="allow")

    def after_call(self, ctx: Any, response: Any) -> None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _reset_state() -> Any:
    """Clear adapter + policy registry before each test."""
    AdapterRegistry.clear()
    PolicyRegistry.clear()
    yield
    AdapterRegistry.clear()
    PolicyRegistry.clear()


# ----------------------------------------------------------------------
# Protocol conformance
# ----------------------------------------------------------------------


def test_stub_adapter_satisfies_runtime_checkable_protocol() -> None:
    stub = _StubAdapter()
    assert isinstance(stub, Proto)
    assert isinstance(stub, FrameworkAdapter)


def test_protocol_rejects_object_missing_required_members() -> None:
    class _PartiallyImplemented:
        name = "broken"

        def detect(self) -> bool:
            return True

    # Missing instrument/supported_policies/shutdown.
    assert not isinstance(_PartiallyImplemented(), FrameworkAdapter)


def test_instrumentation_protocol_round_trip() -> None:
    inst = _StubInstrumentation()
    assert isinstance(inst, Instrumentation)
    inst.shutdown()
    assert inst.shutdown_calls == 1


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_register_two_adapters_with_same_name_is_rejected() -> None:
    a = _StubAdapter()
    b = _StubAdapter()  # different instance, same .name == "stub"
    AdapterRegistry.register(a)
    with pytest.raises(DuplicateAdapterName, match="stub"):
        AdapterRegistry.register(b)


def test_re_registering_same_instance_is_idempotent() -> None:
    a = _StubAdapter()
    AdapterRegistry.register(a)
    AdapterRegistry.register(a)  # no-op
    assert AdapterRegistry.names() == ["stub"]


def test_unregister_clears_active_pointer_when_pointing_at_removed() -> None:
    a = _StubAdapter()
    AdapterRegistry.set_active(a)
    assert get_active_adapter() is a
    AdapterRegistry.unregister(a.name)
    assert get_active_adapter() is None
    assert AdapterRegistry.names() == []


def test_set_active_registers_unknown_adapter_lazily() -> None:
    a = _StubAdapter()
    # set_active should be a one-step shortcut for the common path.
    AdapterRegistry.set_active(a)
    assert AdapterRegistry.names() == ["stub"]
    assert AdapterRegistry.get_active() is a


def test_set_active_rejects_a_different_instance_under_same_name() -> None:
    a = _StubAdapter()
    b = _StubAdapter()
    AdapterRegistry.register(a)
    with pytest.raises(DuplicateAdapterName):
        AdapterRegistry.set_active(b)


def test_clear_active_leaves_registered_adapter_in_place() -> None:
    a = _StubAdapter()
    AdapterRegistry.set_active(a)
    AdapterRegistry.clear_active()
    assert get_active_adapter() is None
    # Still registered, just not active.
    assert AdapterRegistry.get("stub") is a


# ----------------------------------------------------------------------
# Capability propagation through register_policies
# ----------------------------------------------------------------------


def test_register_policies_uses_active_adapter_supported_set() -> None:
    """When an adapter declares LazyToolExposure as supported, the
    Pattern-C-only policy registers without error."""
    adapter = _StubAdapter(supports={_FakeLazyToolExposure})
    AdapterRegistry.set_active(adapter)

    policy = _FakeLazyToolExposure()
    register_policies([policy])  # no kwargs; auto-detects active adapter

    assert len(PolicyRegistry) == 1


def test_register_policies_falls_back_to_pattern_check_for_observation_policy() -> None:
    """An observation policy (BudgetCap) supports all three patterns,
    so a Pattern-C adapter that hasn't enumerated it still accepts
    the registration — the SUPPORTED_PATTERNS fallback fires."""
    adapter = _StubAdapter(supports=set())  # empty supported set
    AdapterRegistry.set_active(adapter)

    register_policies([BudgetCap(max_nd=50)])

    assert len(PolicyRegistry) == 1


def test_register_policies_rejects_pattern_c_only_policy_when_adapter_omits_it() -> None:
    """A Phase-2 modification policy that only supports Pattern C
    must be enumerated by the adapter — otherwise the adapter has no
    way to wire it into the framework and registration fails loud."""
    adapter = _StubAdapter(supports=set())  # adapter doesn't know it
    AdapterRegistry.set_active(adapter)

    with pytest.raises(PolicyNotSupported, match="LazyToolExposure"):
        register_policies([_FakeLazyToolExposure()])


def test_explicit_adapter_arg_overrides_auto_detected_active() -> None:
    """Tests + advanced callers can pass ``adapter=`` directly. The
    explicit arg should win over whatever's currently active."""
    auto_adapter = _StubAdapter(supports=set())
    AdapterRegistry.set_active(auto_adapter)

    other_adapter = _StubAdapter(supports={_FakeLazyToolExposure})
    # Force the explicit adapter — the active stub doesn't support it,
    # but the explicit one does, so registration succeeds.
    register_policies([_FakeLazyToolExposure()], adapter=other_adapter)
    assert len(PolicyRegistry) == 1


def test_no_active_adapter_keeps_legacy_pattern_a_check() -> None:
    """Without any adapter, register_policies validates against the
    pattern enum (Pattern A by default). A modification policy that
    only supports Pattern C raises ``PolicyNotSupported``."""
    with pytest.raises(PolicyNotSupported, match="LazyToolExposure"):
        register_policies([_FakeLazyToolExposure()])


def test_no_active_adapter_accepts_observation_policy_on_pattern_a() -> None:
    register_policies([BudgetCap(max_nd=10)])
    assert len(PolicyRegistry) == 1
