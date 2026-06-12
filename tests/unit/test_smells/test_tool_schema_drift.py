"""tool-schema-drift smell tests."""

from __future__ import annotations

from inkfoot.smells.tool_schema_drift import TOOL_SCHEMA_DRIFT

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return TOOL_SCHEMA_DRIFT.detect(fixture_run(), events)


def _fingerprinted_call(sequence, fingerprint, *, schema_tokens=400):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            ledger_fields={"tool_schema_tokens": schema_tokens},
            metadata={"tools_fingerprint": fingerprint},
        )
    )


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_fingerprint_changes_mid_run() -> None:
    events = [
        _fingerprinted_call(1, "fp-a"),
        _fingerprinted_call(2, "fp-a"),
        _fingerprinted_call(3, "fp-b"),
        _fingerprinted_call(4, "fp-b"),
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "tool-schema-drift"
    assert result.severity == "warn"
    assert result.evidence["distinct_fingerprints"] == 2
    assert result.evidence["fingerprints"] == ["fp-a", "fp-b"]
    # The breach is the first call carrying the second fingerprint.
    assert result.triggered_at_sequence == 3
    assert result.evidence["first_change_sequence"] == 3
    # Both calls from the change onward count.
    assert result.evidence["calls_after_change"] == 2
    assert result.evidence["tool_schema_tokens_after_change"] == 800


def test_counts_every_distinct_fingerprint() -> None:
    events = [
        _fingerprinted_call(1, "fp-a"),
        _fingerprinted_call(2, "fp-b"),
        _fingerprinted_call(3, "fp-c"),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["distinct_fingerprints"] == 3
    assert result.evidence["fingerprints"] == ["fp-a", "fp-b", "fp-c"]
    # The drift starts at the *first* change, not the latest.
    assert result.triggered_at_sequence == 2


def test_returning_to_an_earlier_fingerprint_still_counts() -> None:
    """A→B→A is two distinct fingerprints and the A-calls after the
    change still re-tokenise the schema (the cache was already
    broken at B)."""
    events = [
        _fingerprinted_call(1, "fp-a", schema_tokens=500),
        _fingerprinted_call(2, "fp-b", schema_tokens=500),
        _fingerprinted_call(3, "fp-a", schema_tokens=500),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["distinct_fingerprints"] == 2
    assert result.evidence["calls_after_change"] == 2
    assert result.evidence["tool_schema_tokens_after_change"] == 1000


def test_unfingerprinted_calls_are_excluded_from_the_tally() -> None:
    """Calls without a fingerprint (raw SDK shim mixed into an
    adapter run) neither trigger a change nor count toward the
    after-change accumulation."""
    bare_call = event_from_neutral_call(
        make_neutral_call(
            sequence=3, ledger_fields={"tool_schema_tokens": 9_999}
        )
    )
    events = [
        _fingerprinted_call(1, "fp-a"),
        _fingerprinted_call(2, "fp-a"),
        bare_call,
        _fingerprinted_call(4, "fp-b", schema_tokens=600),
    ]
    result = _detect(events)
    assert result is not None
    assert result.triggered_at_sequence == 4
    assert result.evidence["calls_after_change"] == 1
    assert result.evidence["tool_schema_tokens_after_change"] == 600


def test_cost_impact_prices_after_change_schema_tokens_at_cache_read() -> None:
    events = [
        _fingerprinted_call(1, "fp-a", schema_tokens=400),
        _fingerprinted_call(2, "fp-b", schema_tokens=400),
        _fingerprinted_call(3, "fp-b", schema_tokens=400),
    ]
    result = _detect(events)
    assert result is not None
    # 800 after-change schema tokens × 300 Sonnet cache-read rate —
    # the optimistic floor: even served from cache they'd cost this.
    assert result.estimated_cost_impact_nd == 800 * 300


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_when_fingerprint_is_stable() -> None:
    events = [_fingerprinted_call(i + 1, "fp-a") for i in range(6)]
    assert _detect(events) is None


def test_silent_when_no_call_carries_a_fingerprint() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                ledger_fields={"tool_schema_tokens": 500},
            )
        )
        for i in range(4)
    ]
    assert _detect(events) is None


def test_silent_on_a_single_fingerprinted_call() -> None:
    assert _detect([_fingerprinted_call(1, "fp-a")]) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None


def test_ignores_non_string_and_empty_fingerprints() -> None:
    """Malformed fingerprint values (empty string, numbers) are
    treated as absent rather than as a distinct value."""
    events = [
        _fingerprinted_call(1, "fp-a"),
        event_from_neutral_call(
            make_neutral_call(
                sequence=2,
                ledger_fields={"tool_schema_tokens": 400},
                metadata={"tools_fingerprint": ""},
            )
        ),
        event_from_neutral_call(
            make_neutral_call(
                sequence=3,
                ledger_fields={"tool_schema_tokens": 400},
                metadata={"tools_fingerprint": 12345},
            )
        ),
        _fingerprinted_call(4, "fp-a"),
    ]
    assert _detect(events) is None
