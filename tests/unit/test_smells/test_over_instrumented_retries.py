"""over-instrumented-retries smell tests.

The shim records SDK errors as ``llm_call`` events with an ``error``
payload and an all-zero ledger; the smell approximates "retries per
call" as failed ÷ completed calls.
"""

from __future__ import annotations

from inkfoot.normalise import NeutralError
from inkfoot.smells.over_instrumented_retries import OVER_INSTRUMENTED_RETRIES

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return OVER_INSTRUMENTED_RETRIES.detect(fixture_run(), events)


def _completed_call(sequence, *, retry_overhead_tokens=0):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            ledger_fields={
                "output_tokens": 5,
                "retry_overhead_tokens": retry_overhead_tokens,
            },
        )
    )


def _failed_call(sequence, *, error_type="rate_limit_error"):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            error=NeutralError(type=error_type, message="upstream said no"),
        )
    )


def _retry_throttle_event(sequence):
    return {
        "id": f"e-{sequence}",
        "run_id": "fixture-run",
        "kind": "retry_throttle",
        "occurred_at": 0,
        "payload_json": "{}",
        "sequence": sequence,
        "capture_mode": "metadata",
    }


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_failures_exceed_three_per_completed_call() -> None:
    events = [
        _completed_call(1),
        _failed_call(2),
        _failed_call(3),
        _failed_call(4),
        _failed_call(5),
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "over-instrumented-retries"
    assert result.severity == "warn"
    assert result.evidence["failed_calls"] == 4
    assert result.evidence["completed_calls"] == 1
    assert result.evidence["retries_per_call"] == 4.0
    # The ratio first exceeds 3.0 on the fourth failure.
    assert result.triggered_at_sequence == 5


def test_fires_with_zero_completed_calls() -> None:
    """A run that only ever failed still fires — max(1, completed)
    keeps the division honest."""
    events = [_failed_call(i + 1) for i in range(4)]
    result = _detect(events)
    assert result is not None
    assert result.evidence["completed_calls"] == 0
    assert result.evidence["retries_per_call"] == 4.0


def test_tallies_error_types_in_evidence() -> None:
    events = [
        _failed_call(1, error_type="rate_limit_error"),
        _failed_call(2, error_type="rate_limit_error"),
        _failed_call(3, error_type="rate_limit_error"),
        _failed_call(4, error_type="overloaded_error"),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["error_types"] == {
        "rate_limit_error": 3,
        "overloaded_error": 1,
    }


def test_counts_retry_throttle_events_in_evidence() -> None:
    """RetryThrottle's own events surface in the evidence so the
    reader can see the policy is already pushing back."""
    events = [
        _retry_throttle_event(1),
        _failed_call(2),
        _failed_call(3),
        _failed_call(4),
        _failed_call(5),
        _retry_throttle_event(6),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["retry_throttle_events"] == 2
    # Throttle events are not llm_calls — they never skew the ratio.
    assert result.evidence["failed_calls"] == 4
    assert result.evidence["completed_calls"] == 0


def test_cost_impact_prices_retry_overhead_at_input_rate() -> None:
    events = [
        _completed_call(1, retry_overhead_tokens=200),
        _failed_call(2),
        _failed_call(3),
        _failed_call(4),
        _failed_call(5),
    ]
    result = _detect(events)
    assert result is not None
    # 200 retry-overhead tokens × 3000 Sonnet input rate.
    assert result.estimated_cost_impact_nd == 200 * 3000


def test_cost_impact_is_zero_when_overhead_is_unreported() -> None:
    """Today's translators report retry_overhead_tokens as 0 — the
    smell fires on the run-shape signal alone, with no dollar
    figure attached."""
    events = [_failed_call(i + 1) for i in range(4)]
    result = _detect(events)
    assert result is not None
    assert result.estimated_cost_impact_nd == 0


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_at_exactly_three_failures_per_completed_call() -> None:
    events = [
        _completed_call(1),
        _failed_call(2),
        _failed_call(3),
        _failed_call(4),
    ]
    assert _detect(events) is None


def test_silent_on_an_all_completed_run() -> None:
    events = [_completed_call(i + 1) for i in range(8)]
    assert _detect(events) is None


def test_silent_on_a_healthy_failure_mix() -> None:
    events = [
        _completed_call(1),
        _failed_call(2),
        _completed_call(3),
        _failed_call(4),
        _completed_call(5),
        _completed_call(6),
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None
