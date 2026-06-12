"""summariser-not-firing smell tests."""

from __future__ import annotations

from inkfoot.smells.summariser_not_firing import SUMMARISER_NOT_FIRING

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return SUMMARISER_NOT_FIRING.detect(fixture_run(), events)


def _call_with_tool_result(sequence, tool_result_tokens, **ledger_extra):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            ledger_fields={
                "tool_result_tokens": tool_result_tokens,
                **ledger_extra,
            },
        )
    )


def _policy_event(sequence, kind):
    return {
        "id": f"e-{sequence}",
        "run_id": "fixture-run",
        "kind": kind,
        "occurred_at": 0,
        "payload_json": "{}",
        "sequence": sequence,
        "capture_mode": "metadata",
    }


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_at_three_oversized_calls_with_no_summariser() -> None:
    events = [_call_with_tool_result(i + 1, 3_000) for i in range(3)]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "summariser-not-firing"
    assert result.severity == "warn"
    assert result.evidence["oversized_calls"] == 3
    assert result.evidence["threshold_tokens"] == 2_000
    assert result.evidence["min_oversized_calls"] == 3
    assert result.evidence["max_tool_result_tokens"] == 3_000
    assert result.evidence["excess_tool_result_tokens"] == 3_000
    # The breach lands on the call that completes the minimum count.
    assert result.triggered_at_sequence == 3


def test_small_calls_between_oversized_ones_do_not_reset_the_count() -> None:
    events = [
        _call_with_tool_result(1, 2_500),
        _call_with_tool_result(2, 100),
        _call_with_tool_result(3, 5_000),
        _call_with_tool_result(4, 2_100),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["oversized_calls"] == 3
    assert result.evidence["max_tool_result_tokens"] == 5_000
    assert result.evidence["excess_tool_result_tokens"] == 500 + 3_000 + 100
    assert result.triggered_at_sequence == 4


def test_cost_impact_prices_the_excess_at_input_rate() -> None:
    events = [_call_with_tool_result(i + 1, 3_000) for i in range(3)]
    result = _detect(events)
    assert result is not None
    # 3 × 1000 excess tokens × 3000 Sonnet input rate — the tokens a
    # summariser would have removed, at the rate they cost today.
    assert result.estimated_cost_impact_nd == 3_000 * 3_000


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_below_three_oversized_calls() -> None:
    events = [
        _call_with_tool_result(1, 9_000),
        _call_with_tool_result(2, 9_000),
    ]
    assert _detect(events) is None


def test_silent_at_exactly_the_size_threshold() -> None:
    events = [_call_with_tool_result(i + 1, 2_000) for i in range(5)]
    assert _detect(events) is None


def test_silent_when_a_summariser_policy_event_is_present() -> None:
    events = [
        _policy_event(1, "summariser_replaced"),
        _call_with_tool_result(2, 3_000),
        _call_with_tool_result(3, 3_000),
        _call_with_tool_result(4, 3_000),
    ]
    assert _detect(events) is None


def test_silent_when_any_ledger_reports_summariser_tokens() -> None:
    events = [
        _call_with_tool_result(1, 3_000),
        _call_with_tool_result(2, 3_000),
        _call_with_tool_result(3, 3_000, summariser_tokens=50),
    ]
    assert _detect(events) is None


def test_silent_when_a_call_is_stamped_as_a_summariser_call() -> None:
    events = [
        _call_with_tool_result(1, 3_000),
        _call_with_tool_result(2, 3_000),
        _call_with_tool_result(3, 3_000),
        event_from_neutral_call(
            make_neutral_call(
                sequence=4,
                ledger_fields={"output_tokens": 100},
                metadata={"summariser_call": True},
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None
