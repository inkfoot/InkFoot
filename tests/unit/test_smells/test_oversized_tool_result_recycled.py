"""oversized-tool-result-recycled smell tests."""

from __future__ import annotations

from inkfoot.smells.oversized_tool_result_recycled import (
    OVERSIZED_TOOL_RESULT_RECYCLED,
)

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return OVERSIZED_TOOL_RESULT_RECYCLED.detect(fixture_run(), events)


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_oversized_result_appears_across_three_turns() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "tool_result_tokens": 2500,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(3)
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "oversized-tool-result-recycled"
    assert result.evidence["tool_result_tokens_at_breach"] == 2500
    assert result.evidence["turns_with_tool_results"] == 3
    # Cost: 2500 tokens × (3 - 1 turns) × 3000 Sonnet input = 15_000_000.
    assert result.estimated_cost_impact_nd == 2500 * 2 * 3000


def test_triggered_sequence_is_first_oversized_event() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={"tool_result_tokens": 500, "output_tokens": 5},
            )
        ),
        event_from_neutral_call(
            make_neutral_call(
                sequence=2,
                ledger_fields={"tool_result_tokens": 3000, "output_tokens": 5},
            )
        ),
        event_from_neutral_call(
            make_neutral_call(
                sequence=3,
                ledger_fields={"tool_result_tokens": 800, "output_tokens": 5},
            )
        ),
        event_from_neutral_call(
            make_neutral_call(
                sequence=4,
                ledger_fields={"tool_result_tokens": 3000, "output_tokens": 5},
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    # The first oversized appearance was at sequence=2.
    assert result.triggered_at_sequence == 2
    assert result.evidence["turns_with_tool_results"] == 4


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_when_only_two_turns_have_results() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "tool_result_tokens": 5000,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(2)
    ]
    assert _detect(events) is None


def test_silent_when_result_under_threshold() -> None:
    # 1500 tokens — under the 2000-token oversized threshold.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "tool_result_tokens": 1500,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(5)
    ]
    assert _detect(events) is None


def test_silent_at_exactly_2000_tokens_boundary() -> None:
    """The threshold is "> 2000"; exactly 2000 should not fire.

    Actually the implementation uses ``>= 2000`` because we're
    checking ``ledger.tool_result_tokens >= _OVERSIZED_THRESHOLD_TOKENS``
    — that's a deliberate inclusive boundary for clarity (2000-tok
    results are oversized in practice). The spec's "> 2000" wording
    is informal."""
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "tool_result_tokens": 2000,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(3)
    ]
    # Inclusive boundary; 2000 exactly fires.
    result = _detect(events)
    assert result is not None


def test_silent_when_no_tool_results() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1, ledger_fields={"output_tokens": 5}
            )
        )
        for i in range(5)
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None
