"""Tests for :func:`inkfoot.outcomes.set_outcome_from_heuristic`.

The helper is duck-typed over framework result shapes (LangGraph
state dicts, OpenAI Agents SDK / Pydantic AI / CrewAI result
objects) so the tests use small stand-in classes rather than the
real frameworks — exactly the surface the heuristic sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot._run_lifecycle import NoActiveRun
from inkfoot.outcomes import set_outcome_from_heuristic
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage


@pytest.fixture
def instrumented(tmp_path: Path):
    """Spin up a clean storage + instrumentation per test."""
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    yield storage
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()


def _recorded_outcomes(storage, run_id):
    return [
        json.loads(e["payload_json"])["outcome"]
        for e in storage.iter_events(run_id)
        if e["kind"] == "outcome"
    ]


class _AgentsResult:
    """OpenAI Agents SDK ``RunResult`` shape."""

    def __init__(self, final_output):
        self.final_output = final_output


class _PydanticAIResult:
    """Pydantic AI ``AgentRunResult`` shape."""

    def __init__(self, output):
        self.output = output


class _LegacyPydanticAIResult:
    """Older Pydantic AI spelling (``.data``)."""

    def __init__(self, data):
        self.data = data


class _CrewResult:
    """CrewAI ``CrewOutput`` shape."""

    def __init__(self, raw):
        self.raw = raw


# ----------------------------------------------------------------------
# Success inference
# ----------------------------------------------------------------------


def test_langgraph_state_dict_records_success(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic({"messages": ["done"]})
    assert outcome == "success"
    assert _recorded_outcomes(instrumented, handle.id) == ["success"]


def test_empty_state_dict_still_means_the_graph_completed(instrumented) -> None:
    """LangGraph hands back the final state when the graph reaches
    END — even an empty mapping proves it ran to completion."""
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic({})
    assert outcome == "success"
    assert _recorded_outcomes(instrumented, handle.id) == ["success"]


@pytest.mark.parametrize(
    "result",
    [
        _AgentsResult(final_output="answer"),
        _PydanticAIResult(output={"answer": 42}),
        _LegacyPydanticAIResult(data="answer"),
        _CrewResult(raw="answer"),
    ],
)
def test_result_object_with_populated_payload_records_success(
    instrumented, result
) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(result)
    assert outcome == "success"
    assert _recorded_outcomes(instrumented, handle.id) == ["success"]


def test_plain_truthy_result_records_success(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic("rendered report")
    assert outcome == "success"
    assert _recorded_outcomes(instrumented, handle.id) == ["success"]


# ----------------------------------------------------------------------
# Failure inference
# ----------------------------------------------------------------------


def test_error_keyword_records_failure(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(error=RuntimeError("boom"))
    assert outcome == "failure"
    assert _recorded_outcomes(instrumented, handle.id) == ["failure"]


def test_exception_passed_positionally_records_failure(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(ValueError("boom"))
    assert outcome == "failure"
    assert _recorded_outcomes(instrumented, handle.id) == ["failure"]


def test_error_wins_over_a_result_value(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(
            {"state": "partial"}, error=TimeoutError("upstream")
        )
    assert outcome == "failure"
    assert _recorded_outcomes(instrumented, handle.id) == ["failure"]


# ----------------------------------------------------------------------
# No inference — the run must stay visibly uninstrumented
# ----------------------------------------------------------------------


def test_none_result_makes_no_set_outcome_call(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(None)
    assert outcome is None
    assert _recorded_outcomes(instrumented, handle.id) == []


def test_result_object_with_empty_payload_is_not_guessed(instrumented) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(_AgentsResult(final_output=None))
    assert outcome is None
    assert _recorded_outcomes(instrumented, handle.id) == []


def test_first_present_payload_attribute_decides(instrumented) -> None:
    """Attribute priority is fixed: a present-but-empty
    ``final_output`` is ambiguous even when a later attribute looks
    populated — no guessing."""

    class _Hybrid:
        final_output = None
        output = "looks populated"

    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(_Hybrid())
    assert outcome is None
    assert _recorded_outcomes(instrumented, handle.id) == []


@pytest.mark.parametrize("result", ["", 0, [], False])
def test_falsy_results_make_no_set_outcome_call(instrumented, result) -> None:
    with inkfoot.agent_run(task="t") as handle:
        outcome = set_outcome_from_heuristic(result)
    assert outcome is None
    assert _recorded_outcomes(instrumented, handle.id) == []


# ----------------------------------------------------------------------
# Outside a run
# ----------------------------------------------------------------------


def test_no_inference_path_never_raises_outside_a_run(instrumented) -> None:
    assert set_outcome_from_heuristic(None) is None


def test_inferred_outcome_outside_a_run_raises_like_set_outcome(
    instrumented,
) -> None:
    with pytest.raises(NoActiveRun, match="agent_run"):
        set_outcome_from_heuristic({"messages": []})
