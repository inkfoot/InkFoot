"""SmellEngine + CostSmell + DetectionResult tests (E4-S1)."""

from __future__ import annotations

from typing import Any, Iterable

import pytest

from inkfoot.smells import (
    CostSmell,
    DEFAULT_SMELLS,
    DetectionResult,
    iter_llm_calls,
)
from inkfoot.smells.engine import SmellEngine

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


# ----------------------------------------------------------------------
# Dataclass invariants
# ----------------------------------------------------------------------


def test_cost_smell_is_frozen() -> None:
    smell = DEFAULT_SMELLS[0]
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        smell.id = "different"  # type: ignore[misc]


def test_cost_smell_rejects_invalid_severity() -> None:
    def _no_op(run, events):
        return None

    with pytest.raises(ValueError, match="severity"):
        CostSmell(
            id="bad", title="x", description="x", severity="oops",  # type: ignore[arg-type]
            detect=_no_op, recommendation="x"
        )


def test_cost_smell_rejects_empty_id() -> None:
    def _no_op(run, events):
        return None

    with pytest.raises(ValueError, match="id"):
        CostSmell(
            id="", title="x", description="x", severity="info",
            detect=_no_op, recommendation="x"
        )


def test_detection_result_is_frozen() -> None:
    smell = DEFAULT_SMELLS[0]
    result = DetectionResult(
        smell=smell, triggered_at_sequence=1, severity="warn"
    )
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.severity = "info"  # type: ignore[misc]


def test_detection_result_rejects_invalid_severity() -> None:
    smell = DEFAULT_SMELLS[0]
    with pytest.raises(ValueError, match="severity"):
        DetectionResult(
            smell=smell, triggered_at_sequence=0, severity="catastrophic"  # type: ignore[arg-type]
        )


# ----------------------------------------------------------------------
# iter_llm_calls helper
# ----------------------------------------------------------------------


def test_iter_llm_calls_skips_non_llm_call_kinds() -> None:
    call = make_neutral_call(sequence=1)
    llm_event = event_from_neutral_call(call)
    policy_event = {
        "id": "p-1",
        "run_id": "fixture-run",
        "kind": "budget_warning",
        "occurred_at": 1,
        "payload_json": '{"reason": "over budget"}',
        "sequence": 2,
        "capture_mode": "metadata",
    }
    pairs = list(iter_llm_calls([policy_event, llm_event]))
    assert len(pairs) == 1
    assert pairs[0][0]["kind"] == "llm_call"


def test_iter_llm_calls_skips_unparseable_payloads() -> None:
    pairs = list(
        iter_llm_calls(
            [
                {"kind": "llm_call", "payload_json": "not valid json"},
                {"kind": "llm_call", "payload_json": None},
                {"kind": "llm_call"},  # missing payload_json
            ]
        )
    )
    assert pairs == []


# ----------------------------------------------------------------------
# Engine semantics
# ----------------------------------------------------------------------


def test_empty_smell_set_returns_empty_list() -> None:
    engine = SmellEngine([])
    assert engine.evaluate(fixture_run(), []) == []


def test_always_fires_smell_returns_one_result_per_call() -> None:
    def _always_fire(run, events):
        return DetectionResult(
            smell=ALWAYS, triggered_at_sequence=0, severity="info"
        )

    ALWAYS = CostSmell(
        id="always-fires",
        title="t",
        description="d",
        severity="info",
        detect=_always_fire,
        recommendation="r",
    )
    engine = SmellEngine([ALWAYS])
    results = engine.evaluate(fixture_run(), [])
    assert len(results) == 1
    # Second call: still exactly one (engine doesn't dedup across calls).
    results = engine.evaluate(fixture_run(), [])
    assert len(results) == 1


def test_raising_smell_is_isolated_others_continue() -> None:
    def _boom(run, events):
        raise RuntimeError("smell is broken")

    def _ok(run, events):
        return DetectionResult(
            smell=OK_SMELL, triggered_at_sequence=0, severity="info"
        )

    BOOM = CostSmell(
        id="boom", title="t", description="d", severity="warn",
        detect=_boom, recommendation="r",
    )
    OK_SMELL = CostSmell(
        id="ok", title="t", description="d", severity="info",
        detect=_ok, recommendation="r",
    )
    engine = SmellEngine([BOOM, OK_SMELL])
    results = engine.evaluate(fixture_run(), [])
    # BOOM crashed; OK fired.
    assert len(results) == 1
    assert results[0].smell.id == "ok"


def test_evaluate_aggregate_concatenates_per_run_findings() -> None:
    def _always(run, events):
        return DetectionResult(
            smell=ALW, triggered_at_sequence=0, severity="info"
        )

    ALW = CostSmell(
        id="alw", title="t", description="d", severity="info",
        detect=_always, recommendation="r",
    )
    engine = SmellEngine([ALW])
    runs: list[tuple[Any, Iterable[dict]]] = [
        (fixture_run(), []),
        (fixture_run(), []),
        (fixture_run(), []),
    ]
    results = engine.evaluate_aggregate(runs)
    assert len(results) == 3


def test_engine_smells_attr_is_read_only_tuple() -> None:
    engine = SmellEngine(list(DEFAULT_SMELLS))
    assert isinstance(engine.smells, tuple)
    assert len(engine.smells) == len(DEFAULT_SMELLS)
    with pytest.raises(AttributeError):
        engine.smells = ()  # type: ignore[misc]


def test_engine_with_no_smells_arg_uses_defaults() -> None:
    engine = SmellEngine()
    assert tuple(s.id for s in engine.smells) == tuple(
        s.id for s in DEFAULT_SMELLS
    )


def test_events_snapshot_protects_against_iterator_exhaustion() -> None:
    """The engine snapshots events at call time so each smell sees
    the same set — a smell that walks the iterator can't starve
    later smells."""
    seen_lengths: list[int] = []

    def _count(run, events):
        seen_lengths.append(len(list(events)))
        return None

    SMELL1 = CostSmell(
        id="c1", title="t", description="d", severity="info",
        detect=_count, recommendation="r",
    )
    SMELL2 = CostSmell(
        id="c2", title="t", description="d", severity="info",
        detect=_count, recommendation="r",
    )
    # Pass a generator — would be exhausted by the first call if
    # the engine didn't snapshot.
    one_shot = (
        e for e in [
            {"kind": "llm_call", "payload_json": "{}", "sequence": 1},
            {"kind": "llm_call", "payload_json": "{}", "sequence": 2},
        ]
    )
    SmellEngine([SMELL1, SMELL2]).evaluate(fixture_run(), one_shot)
    assert seen_lengths == [2, 2]
