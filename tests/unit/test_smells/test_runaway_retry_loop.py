"""runaway-retry-loop smell tests."""

from __future__ import annotations

from inkfoot.smells.runaway_retry_loop import RUNAWAY_RETRY_LOOP

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return RUNAWAY_RETRY_LOOP.detect(fixture_run(), events)


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_same_tool_called_more_than_five_times() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=("search",),
                ledger_fields={"output_tokens": 5},
            )
        )
        for i in range(6)
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "runaway-retry-loop"
    assert result.severity == "critical"
    assert result.evidence["tool_name"] == "search"
    assert result.evidence["call_count"] == 6
    # First breach is on the 6th call (count crosses 5).
    assert result.triggered_at_sequence == 6


def test_counts_tool_distribution_evidence() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=("search",) if i < 6 else ("write",),
                ledger_fields={"output_tokens": 5},
            )
        )
        for i in range(8)
    ]
    result = _detect(events)
    assert result is not None
    dist = result.evidence["tool_call_distribution"]
    assert dist["search"] == 6
    assert dist["write"] == 2


def test_handles_multiple_tools_in_one_call() -> None:
    """Two tools in a single response both count toward their own
    totals."""
    events = []
    for i in range(6):
        events.append(
            event_from_neutral_call(
                make_neutral_call(
                    sequence=i + 1,
                    tools_called=("search", "browse"),
                    ledger_fields={"output_tokens": 5},
                )
            )
        )
    result = _detect(events)
    assert result is not None
    # The first tool to exceed the threshold wins the "breach_tool"
    # slot. With both incrementing in lock-step, "search" hits 6 first.
    assert result.evidence["call_count"] == 6


def test_cost_impact_uses_retry_overhead_tokens_when_populated() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=("search",),
                ledger_fields={
                    "output_tokens": 5,
                    "retry_overhead_tokens": 100,
                },
            )
        )
        for i in range(6)
    ]
    result = _detect(events)
    assert result is not None
    # 6 calls × 100 retry overhead × 3000 Sonnet input rate = 1_800_000.
    assert result.estimated_cost_impact_nd == 6 * 100 * 3000


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_at_exactly_five_calls() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=("search",),
                ledger_fields={"output_tokens": 5},
            )
        )
        for i in range(5)
    ]
    assert _detect(events) is None


def test_silent_when_calls_use_different_tools() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=(f"tool_{i}",),
                ledger_fields={"output_tokens": 5},
            )
        )
        for i in range(8)
    ]
    assert _detect(events) is None


def test_silent_when_no_tools_called() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1, ledger_fields={"output_tokens": 5}
            )
        )
        for i in range(10)
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None


def test_skips_invalid_tool_name_shapes() -> None:
    # A malformed payload that has tools_called as something weird —
    # the smell must not crash.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                tools_called=("",) if i < 6 else (),  # empty names
                ledger_fields={"output_tokens": 5},
            )
        )
        for i in range(8)
    ]
    # Empty tool names are silently filtered; nothing fires.
    assert _detect(events) is None
