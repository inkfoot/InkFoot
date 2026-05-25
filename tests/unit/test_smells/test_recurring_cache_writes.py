"""recurring-cache-writes smell tests."""

from __future__ import annotations

from inkfoot.smells.recurring_cache_writes import RECURRING_CACHE_WRITES

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return RECURRING_CACHE_WRITES.detect(fixture_run(), events)


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_more_than_eighty_percent_calls_write_cache() -> None:
    # 5 calls, 5 with cache_creation > 0 → 100% > 80%.
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={
                    "cache_creation_tokens": 1000,
                    "output_tokens": 5,
                },
            )
        )
        for i in range(5)
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "recurring-cache-writes"
    assert result.evidence["calls_with_cache_writes"] == 5
    assert result.evidence["total_calls"] == 5
    # Sonnet write premium: 3750 - 3000 = 750. 5000 × 750 = 3_750_000.
    assert result.estimated_cost_impact_nd == 5 * 1000 * (3_750 - 3_000)


def test_fires_at_just_above_threshold() -> None:
    # 10 calls, 9 write — 90% > 80%.
    events = []
    for i in range(9):
        events.append(
            event_from_neutral_call(
                make_neutral_call(
                    sequence=i + 1,
                    ledger_fields={
                        "cache_creation_tokens": 500,
                        "output_tokens": 5,
                    },
                )
            )
        )
    events.append(
        event_from_neutral_call(
            make_neutral_call(
                sequence=10,
                ledger_fields={"output_tokens": 5},  # one clean call
            )
        )
    )
    result = _detect(events)
    assert result is not None
    assert result.evidence["calls_with_cache_writes"] == 9
    assert result.evidence["write_fraction"] == 0.9


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_at_exactly_eighty_percent() -> None:
    """Strict greater-than threshold."""
    events = []
    for i in range(8):
        events.append(
            event_from_neutral_call(
                make_neutral_call(
                    sequence=i + 1,
                    ledger_fields={
                        "cache_creation_tokens": 500,
                        "output_tokens": 5,
                    },
                )
            )
        )
    for i in range(2):
        events.append(
            event_from_neutral_call(
                make_neutral_call(
                    sequence=9 + i, ledger_fields={"output_tokens": 5}
                )
            )
        )
    assert _detect(events) is None


def test_silent_when_no_writes() -> None:
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


def test_silent_when_only_one_call_writes() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                ledger_fields={
                    "cache_creation_tokens": 1000,
                    "output_tokens": 5,
                },
            )
        )
    ]
    for i in range(4):
        events.append(
            event_from_neutral_call(
                make_neutral_call(
                    sequence=i + 2, ledger_fields={"output_tokens": 5}
                )
            )
        )
    # 1 write of 5 calls = 20% → silent.
    assert _detect(events) is None
