"""Tests for the three Phase-0 observation policies (E3-S4)."""

from __future__ import annotations

import time

import pytest

from inkfoot.policy import (
    BudgetCap,
    CacheControlPlacer,
    CallContext,
    PolicyDecision,
    RetryThrottle,
)


# ----------------------------------------------------------------------
# BudgetCap
# ----------------------------------------------------------------------


def _ctx(estimated_nd: int = 0, **overrides) -> CallContext:
    defaults = dict(
        provider=overrides.pop("provider", "anthropic"),
        model=overrides.pop("model", "claude-sonnet-4-6"),
        run_id=overrides.pop("run_id", "r1"),
    )
    ctx = CallContext(**defaults)
    ctx.estimated_nanodollars = estimated_nd
    ctx.metadata.update(overrides.pop("metadata", {}))
    ctx.request_kwargs.update(overrides.pop("request_kwargs", {}))
    return ctx


def test_budget_cap_constructor_rejects_negative() -> None:
    with pytest.raises(ValueError):
        BudgetCap(max_nd=-1)


def test_budget_cap_constructor_rejects_float() -> None:
    with pytest.raises(TypeError):
        BudgetCap(max_nd=1.5)  # type: ignore[arg-type]


def test_budget_cap_no_warn_when_under_budget() -> None:
    policy = BudgetCap(max_nd=10_000_000)
    ctx = _ctx(estimated_nd=1_000_000)
    decision = policy.before_call(ctx)
    assert decision.action == "allow"
    policy.after_call(ctx, response=object())
    assert policy.current_total("r1") == 1_000_000


def test_budget_cap_warns_when_running_total_passes_threshold() -> None:
    policy = BudgetCap(max_nd=5_000_000)
    # Three calls of 2_000_000 each — total = 6_000_000 > 5_000_000.
    for _ in range(3):
        ctx = _ctx(estimated_nd=2_000_000)
        policy.before_call(ctx)
        policy.after_call(ctx, response=object())

    # The fourth call's before_call should fire the warning.
    decision = policy.before_call(_ctx(estimated_nd=1_000_000))
    assert decision.action == "warn"
    assert decision.emit_event_kind == "budget_warning"
    assert decision.metadata["max_nd"] == 5_000_000


def test_budget_cap_fires_only_once_per_run() -> None:
    policy = BudgetCap(max_nd=1_000_000)
    # Push the total well past the budget.
    ctx = _ctx(estimated_nd=10_000_000)
    policy.before_call(ctx)
    policy.after_call(ctx, response=object())

    first = policy.before_call(_ctx(estimated_nd=1))
    second = policy.before_call(_ctx(estimated_nd=1))
    assert first.action == "warn"
    assert second.action == "allow"


def test_budget_cap_after_call_with_none_estimate_is_noop() -> None:
    policy = BudgetCap(max_nd=1_000_000)
    ctx = _ctx(estimated_nd=0)
    ctx.estimated_nanodollars = None
    policy.after_call(ctx, response=object())
    assert policy.current_total("r1") == 0


# ----------------------------------------------------------------------
# RetryThrottle
# ----------------------------------------------------------------------


def test_retry_throttle_constructor_validates_bounds() -> None:
    with pytest.raises(ValueError):
        RetryThrottle(window_s=0, max=3)
    with pytest.raises(ValueError):
        RetryThrottle(window_s=60, max=0)


def test_retry_throttle_no_warn_when_call_is_not_a_retry() -> None:
    policy = RetryThrottle(window_s=60, max=2)
    for _ in range(5):
        ctx = _ctx()  # no retry metadata
        decision = policy.before_call(ctx)
        assert decision.action == "allow"


def test_retry_throttle_fires_on_max_plus_one() -> None:
    policy = RetryThrottle(window_s=60, max=3)
    # 3 retries — no warn.
    for _ in range(3):
        ctx = _ctx(metadata={"retry": True})
        decision = policy.before_call(ctx)
        assert decision.action == "allow"
    # 4th retry crosses the threshold.
    ctx = _ctx(metadata={"retry": True})
    decision = policy.before_call(ctx)
    assert decision.action == "warn"
    assert decision.emit_event_kind == "retry_throttle"
    assert decision.metadata["retry_count"] == 4


def test_retry_throttle_fires_only_once_until_count_drops() -> None:
    policy = RetryThrottle(window_s=60, max=2)
    # First burst: the 3rd retry breaches max=2; the next two stay quiet.
    decisions = [
        policy.before_call(_ctx(metadata={"retry": True}))
        for _ in range(5)
    ]
    warn_count_first_burst = sum(1 for d in decisions if d.action == "warn")
    assert warn_count_first_burst == 1
    # The warn must be the *third* call (count exceeds max for the
    # first time on that one).
    assert decisions[2].action == "warn"

    # A non-retry call resets the fired flag so a future burst can
    # re-fire. We don't claim the *next* retry fires (count is still
    # high) — only that the fired-once-per-run latch released.
    policy.before_call(_ctx())  # not a retry
    # After enough non-retry intervals (or window roll-off) the
    # latch can fire again. We assert the latch was released by
    # peeking at the policy's private state — the public-behaviour
    # check is the window-rolloff test above.
    assert "r1" not in policy._fired  # type: ignore[attr-defined]


def test_retry_throttle_window_drops_old_events(monkeypatch) -> None:
    policy = RetryThrottle(window_s=1, max=2)
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    for _ in range(2):
        policy.before_call(_ctx(metadata={"retry": True}))
    # Advance past the window.
    now[0] += 5.0
    # A single retry now sits alone in the window — no warn.
    decision = policy.before_call(_ctx(metadata={"retry": True}))
    assert decision.action == "allow"


# ----------------------------------------------------------------------
# CacheControlPlacer
# ----------------------------------------------------------------------


def test_cache_control_placer_ignores_openai() -> None:
    policy = CacheControlPlacer()
    ctx = _ctx(
        provider="openai",
        request_kwargs={"system": "x" * 10_000},
    )
    assert policy.before_call(ctx).action == "allow"


def test_cache_control_placer_advises_on_large_unmarked_system() -> None:
    policy = CacheControlPlacer()
    ctx = _ctx(
        provider="anthropic",
        request_kwargs={"system": "x" * 8000},
    )
    decision = policy.before_call(ctx)
    assert decision.action == "warn"
    assert decision.emit_event_kind == "cache_control_advice"
    assert "system" in decision.metadata["blocks"]


def test_cache_control_placer_no_advice_on_small_system() -> None:
    policy = CacheControlPlacer()
    ctx = _ctx(
        provider="anthropic",
        request_kwargs={"system": "short system block"},
    )
    assert policy.before_call(ctx).action == "allow"


def test_cache_control_placer_recognises_existing_marker() -> None:
    policy = CacheControlPlacer()
    ctx = _ctx(
        provider="anthropic",
        request_kwargs={
            "system": [
                {
                    "type": "text",
                    "text": "x" * 8000,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        },
    )
    assert policy.before_call(ctx).action == "allow"


def test_cache_control_placer_fires_once_per_block_per_run() -> None:
    policy = CacheControlPlacer()
    request = {"system": "x" * 8000}

    first = policy.before_call(
        _ctx(provider="anthropic", request_kwargs=request)
    )
    second = policy.before_call(
        _ctx(provider="anthropic", request_kwargs=request)
    )
    assert first.action == "warn"
    assert second.action == "allow"


def test_cache_control_placer_advises_on_tools_too() -> None:
    policy = CacheControlPlacer()
    # Build a tools array large enough to exceed the threshold.
    big_tools = [
        {
            "name": f"tool_{i}",
            "description": "x" * 1000,
            "input_schema": {"type": "object"},
        }
        for i in range(5)
    ]
    ctx = _ctx(
        provider="anthropic",
        request_kwargs={"tools": big_tools},
    )
    decision = policy.before_call(ctx)
    assert decision.action == "warn"
    assert "tools" in decision.metadata["blocks"]
