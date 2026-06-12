"""cost-skewed-by-outlier smell tests.

The detector is a pure function: peer context arrives as enrichment
keys on the run dict (the report CLI attaches them), so these tests
build the run dict directly instead of seeding a database.
"""

from __future__ import annotations

from inkfoot.smells.cost_skewed_by_outlier import (
    COST_SKEWED_BY_OUTLIER,
    MIN_PEER_RUNS,
    PEER_COUNT_KEY,
    PEER_P50_KEY,
    peer_p50,
)

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _run_with_peers(*, p50, count, total_nanodollars=None):
    run = fixture_run()
    run[PEER_P50_KEY] = p50
    run[PEER_COUNT_KEY] = count
    if total_nanodollars is not None:
        run["total_nanodollars"] = total_nanodollars
    return run


def _call_costing(sequence, nanodollars):
    return event_from_neutral_call(
        make_neutral_call(
            sequence=sequence,
            ledger_fields={"output_tokens": 5},
            estimated_nanodollars=nanodollars,
        )
    )


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_when_run_exceeds_ten_times_peer_median() -> None:
    run = _run_with_peers(p50=1_000_000, count=6)
    events = [
        _call_costing(1, 8_000_000),
        _call_costing(2, 4_000_000),
    ]
    result = COST_SKEWED_BY_OUTLIER.detect(run, events)
    assert result is not None
    assert result.smell.id == "cost-skewed-by-outlier"
    assert result.severity == "warn"
    assert result.evidence["run_cost_nanodollars"] == 12_000_000
    assert result.evidence["task_peer_p50_nanodollars"] == 1_000_000
    assert result.evidence["task_peer_count"] == 6
    assert result.evidence["ratio"] == 12.0
    # Points at the costliest call.
    assert result.triggered_at_sequence == 1


def test_cost_impact_is_the_excess_over_the_peer_median() -> None:
    run = _run_with_peers(p50=1_000_000, count=6)
    events = [_call_costing(1, 12_000_000)]
    result = COST_SKEWED_BY_OUTLIER.detect(run, events)
    assert result is not None
    # Direct nanodollar excess — no token-rate conversion.
    assert result.estimated_cost_impact_nd == 11_000_000


def test_falls_back_to_run_total_when_events_carry_no_estimates() -> None:
    """Event streams captured without per-call pricing still get the
    cross-run check via the projected runs-table total."""
    run = _run_with_peers(
        p50=2_000_000, count=MIN_PEER_RUNS, total_nanodollars=25_000_000
    )
    result = COST_SKEWED_BY_OUTLIER.detect(run, [])
    assert result is not None
    assert result.evidence["run_cost_nanodollars"] == 25_000_000
    assert result.estimated_cost_impact_nd == 23_000_000
    # No call to point at — sequence 0 marks "the run as a whole".
    assert result.triggered_at_sequence == 0


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_without_enrichment_keys() -> None:
    """Live in-process evaluation has no peer context — the smell
    must stay silent rather than guess."""
    events = [_call_costing(1, 999_000_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(fixture_run(), events) is None


def test_silent_below_minimum_peer_count() -> None:
    run = _run_with_peers(p50=1_000_000, count=MIN_PEER_RUNS - 1)
    events = [_call_costing(1, 50_000_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(run, events) is None


def test_silent_when_peer_median_is_zero() -> None:
    run = _run_with_peers(p50=0, count=10)
    events = [_call_costing(1, 50_000_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(run, events) is None


def test_silent_at_exactly_ten_times_the_median() -> None:
    run = _run_with_peers(p50=1_000_000, count=6)
    events = [_call_costing(1, 10_000_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(run, events) is None


def test_silent_on_a_typical_run() -> None:
    run = _run_with_peers(p50=1_000_000, count=6)
    events = [_call_costing(1, 1_200_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(run, events) is None


def test_rejects_malformed_enrichment_values() -> None:
    run = fixture_run()
    run[PEER_P50_KEY] = True  # bool is not a usable median
    run[PEER_COUNT_KEY] = 10
    events = [_call_costing(1, 50_000_000)]
    assert COST_SKEWED_BY_OUTLIER.detect(run, events) is None


# ----------------------------------------------------------------------
# peer_p50 helper
# ----------------------------------------------------------------------


def test_peer_p50_of_empty_is_zero() -> None:
    assert peer_p50([]) == 0


def test_peer_p50_uses_the_report_index_convention() -> None:
    # idx = int(n * 0.50), same clamped-index convention as the
    # report's p95 so the two stats stay comparable.
    assert peer_p50([1, 2, 3, 4, 5]) == 3
    assert peer_p50([5, 1, 3]) == 3  # order-independent
    assert peer_p50([1, 2]) == 2
    assert peer_p50([7]) == 7
