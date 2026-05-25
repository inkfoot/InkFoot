"""unstable-prompt-prefix smell tests (positive + negative + edges)."""

from __future__ import annotations

from inkfoot.smells.unstable_prompt_prefix import UNSTABLE_PROMPT_PREFIX

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return UNSTABLE_PROMPT_PREFIX.detect(fixture_run(), events)


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_dynamic_exceeds_ten_percent() -> None:
    # 100 static, 20 dynamic across one call → 20 / 120 ≈ 16.7% → fires.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 20,
                    "output_tokens": 5,
                },
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "unstable-prompt-prefix"
    assert result.severity == "warn"
    assert result.evidence["system_dynamic_tokens"] == 20
    assert result.evidence["system_static_tokens"] == 100
    assert result.evidence["dynamic_fraction"] > 0.10
    # Cost impact: 20 dynamic tokens × Sonnet cache_read rate (300 nd).
    assert result.estimated_cost_impact_nd == 20 * 300


def test_aggregates_across_multiple_calls() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 15,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(3)
    ]
    # 3 calls × (100 static + 15 dynamic) = 300 + 45 = 345; 45/345 = 13% → fires.
    result = _detect(events)
    assert result is not None
    assert result.evidence["system_dynamic_tokens"] == 45


def test_first_dynamic_sequence_is_recorded() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 0,  # no dynamic on call 1
                    "output_tokens": 5,
                },
            )
        ),
        event_from_neutral_call(
            make_neutral_call(
                sequence=2,
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 50,  # dynamic appears on call 2
                    "output_tokens": 5,
                },
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    assert result.triggered_at_sequence == 2


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_when_dynamic_below_threshold() -> None:
    # 100 static, 5 dynamic → 5/105 ≈ 4.8% → silent.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 5,
                    "output_tokens": 5,
                },
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_when_no_system_block() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={"output_tokens": 5},
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_when_dynamic_at_exactly_ten_percent() -> None:
    """Strict greater-than threshold; exactly 10% does not fire."""
    # 90 static, 10 dynamic → 10/100 = 0.10 → not > 0.10 → silent.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={
                    "system_static_tokens": 90,
                    "system_dynamic_tokens": 10,
                    "output_tokens": 5,
                },
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None


def test_cost_impact_zero_when_model_not_in_pricing_table() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                provider="anthropic",
                model="claude-imaginary",  # unknown model
                ledger_fields={
                    "system_static_tokens": 100,
                    "system_dynamic_tokens": 50,
                    "output_tokens": 5,
                },
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    # Smell still fires; cost impact is 0 because pricing is unknown.
    assert result.estimated_cost_impact_nd == 0
