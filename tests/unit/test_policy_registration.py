"""Policy registration + capability matrix tests."""

from __future__ import annotations

from typing import Any

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot.errors import PolicyNotSupported
from inkfoot.policy import (
    BudgetCap,
    CacheControlPlacer,
    CallContext,
    IntegrationPattern,
    Policy,
    PolicyDecision,
    RetryThrottle,
    register_policies,
)
from inkfoot.policy.registry import PolicyRegistry


class _PatternAOk(Policy):
    NAME = "PatternAOk"
    SUPPORTED_PATTERNS = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }

    def before_call(self, ctx: CallContext) -> PolicyDecision:
        return PolicyDecision()

    def after_call(self, ctx: CallContext, response: Any) -> None:
        return None


class _PatternCOnly(Policy):
    NAME = "PatternCOnly"
    SUPPORTED_PATTERNS = {IntegrationPattern.C}
    DOCS_URL = "https://inkfoot.dev/docs/policies"

    def before_call(self, ctx: CallContext) -> PolicyDecision:
        return PolicyDecision()

    def after_call(self, ctx: CallContext, response: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()


def test_pattern_a_policy_registers_cleanly() -> None:
    register_policies(
        [_PatternAOk()], active_pattern=IntegrationPattern.A
    )
    assert len(PolicyRegistry) == 1


def test_pattern_c_only_policy_on_pattern_a_raises() -> None:
    with pytest.raises(PolicyNotSupported) as exc:
        register_policies(
            [_PatternCOnly()], active_pattern=IntegrationPattern.A
        )
    message = str(exc.value)
    assert "PatternCOnly" in message
    # The customer-visible bits:
    assert "Pattern A" in message
    assert "inkfoot.dev/docs/policies" in message


def test_error_message_lists_supported_patterns() -> None:
    with pytest.raises(PolicyNotSupported) as exc:
        register_policies(
            [_PatternCOnly()], active_pattern=IntegrationPattern.A
        )
    # "{C}" should appear in the message because that's the lone
    # supported pattern.
    assert "C" in str(exc.value)


def test_two_instances_of_same_policy_class_coexist() -> None:
    register_policies(
        [BudgetCap(max_nd=1000), BudgetCap(max_nd=2000)],
        active_pattern=IntegrationPattern.A,
    )
    assert len(PolicyRegistry) == 2


def test_same_instance_twice_is_dedupd() -> None:
    p = _PatternAOk()
    register_policies([p, p], active_pattern=IntegrationPattern.A)
    assert len(PolicyRegistry) == 1


def test_registry_dispatches_before_and_after_to_every_policy() -> None:
    calls: list[str] = []

    class Recorder(Policy):
        NAME = "Recorder"
        SUPPORTED_PATTERNS = {IntegrationPattern.A}

        def __init__(self, name: str) -> None:
            self._name = name

        def before_call(self, ctx: CallContext) -> PolicyDecision:
            calls.append(f"{self._name}.before")
            return PolicyDecision()

        def after_call(self, ctx: CallContext, response: Any) -> None:
            calls.append(f"{self._name}.after")

    register_policies(
        [Recorder("a"), Recorder("b")],
        active_pattern=IntegrationPattern.A,
    )
    ctx = CallContext(
        provider="anthropic", model="claude-sonnet-4-6", run_id="r1"
    )
    PolicyRegistry.before_call(ctx)
    PolicyRegistry.after_call(ctx, response={"usage": {}})
    assert calls == ["a.before", "b.before", "a.after", "b.after"]


def test_observation_policies_all_support_pattern_a() -> None:
    """Sanity: every current policy can be registered on Pattern A."""
    register_policies(
        [
            BudgetCap(max_nd=1_000_000_000),
            RetryThrottle(window_s=60, max=3),
            CacheControlPlacer(),
        ],
        active_pattern=IntegrationPattern.A,
    )
    assert len(PolicyRegistry) == 3
