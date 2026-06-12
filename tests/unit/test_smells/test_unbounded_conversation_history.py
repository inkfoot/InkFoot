"""unbounded-conversation-history smell tests."""

from __future__ import annotations

from inkfoot.smells.unbounded_conversation_history import (
    UNBOUNDED_CONVERSATION_HISTORY,
)

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return UNBOUNDED_CONVERSATION_HISTORY.detect(fixture_run(), events)


def _call_with_memory(sequence, memory_tokens):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            ledger_fields={"memory_tokens": memory_tokens},
        )
    )


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_one_call_crosses_fifty_thousand_memory_tokens() -> None:
    events = [
        _call_with_memory(1, 10_000),
        _call_with_memory(2, 60_000),
        _call_with_memory(3, 20_000),
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "unbounded-conversation-history"
    assert result.severity == "warn"
    assert result.evidence["max_memory_tokens"] == 60_000
    assert result.evidence["threshold_tokens"] == 50_000
    assert result.evidence["breaching_calls"] == 1
    assert result.evidence["excess_memory_tokens"] == 10_000
    assert result.triggered_at_sequence == 2


def test_accumulates_excess_across_breaching_calls() -> None:
    events = [
        _call_with_memory(1, 55_000),
        _call_with_memory(2, 70_000),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["breaching_calls"] == 2
    assert result.evidence["excess_memory_tokens"] == 5_000 + 20_000
    # First breach, not the largest one.
    assert result.triggered_at_sequence == 1


def test_cost_impact_prices_the_excess_at_cache_read() -> None:
    events = [_call_with_memory(1, 60_000)]
    result = _detect(events)
    assert result is not None
    # 10_000 excess tokens × 300 Sonnet cache-read rate — history is
    # a stable prefix, so cache_read is the optimistic floor on what
    # trimming would recover.
    assert result.estimated_cost_impact_nd == 10_000 * 300


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_at_exactly_the_threshold() -> None:
    assert _detect([_call_with_memory(1, 50_000)]) is None


def test_silent_when_only_the_sum_crosses_the_threshold() -> None:
    """History is recycled context — the threshold applies to the
    largest single call, never to the sum across turns."""
    events = [_call_with_memory(i + 1, 30_000) for i in range(3)]
    assert _detect(events) is None


def test_silent_when_memory_is_modest() -> None:
    events = [_call_with_memory(i + 1, 2_000) for i in range(10)]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None
