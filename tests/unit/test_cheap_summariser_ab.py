"""A/B trust-mode tests: pairing arithmetic, branch assignment,
quality-regression auto-disable, and the report-side smell."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from inkfoot.policy import CallContext
from inkfoot.policy._ab_pairing import ABObservation, compute_quality_delta
from inkfoot.policy.cheap_summariser import (
    CheapSummariser,
    _clear_disabled_tasks,
    summariser_disabled_for_task,
)
from inkfoot.smells import DEFAULT_SMELLS, get_smell
from inkfoot.smells.summariser_quality_regression import (
    SUMMARISER_QUALITY_REGRESSION,
)
from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# Pure pairing arithmetic
# ----------------------------------------------------------------------


def _obs(
    branch: str,
    outcome: Optional[str],
    *,
    task: str = "triage",
    quality: Optional[float] = None,
    run_id: str = "r",
) -> ABObservation:
    return ABObservation(
        run_id=run_id,
        task=task,
        branch=branch,
        outcome=outcome,
        quality_score=quality,
    )


def test_delta_is_none_below_min_runs_per_branch() -> None:
    observations = [_obs("A", "success")] * 5 + [_obs("B", "failure")] * 4
    assert (
        compute_quality_delta("triage", observations, min_runs_per_branch=5)
        is None
    )


def test_min_runs_validation() -> None:
    with pytest.raises(ValueError, match="min_runs_per_branch"):
        compute_quality_delta("triage", [], min_runs_per_branch=0)


def test_success_rate_drop_arithmetic() -> None:
    observations = (
        [_obs("A", "success")] * 4
        + [_obs("A", "failure")]
        + [_obs("B", "success")] * 2
        + [_obs("B", "failure")] * 3
    )
    delta = compute_quality_delta("triage", observations, min_runs_per_branch=5)
    assert delta is not None
    assert delta.control_runs == 5
    assert delta.treatment_runs == 5
    assert delta.control_success_rate == pytest.approx(0.8)
    assert delta.treatment_success_rate == pytest.approx(0.4)
    assert delta.success_rate_drop == pytest.approx(0.4)


def test_other_tasks_and_outcomeless_runs_are_excluded() -> None:
    observations = (
        [_obs("A", "success")] * 5
        + [_obs("B", "success")] * 5
        + [_obs("A", "failure", task="other")] * 10
        + [_obs("B", None)] * 10  # still running: no outcome yet
    )
    delta = compute_quality_delta("triage", observations, min_runs_per_branch=5)
    assert delta is not None
    assert delta.control_runs == 5
    assert delta.treatment_runs == 5
    assert delta.success_rate_drop == pytest.approx(0.0)


def test_quality_score_delta_none_when_either_branch_unscored() -> None:
    observations = (
        [_obs("A", "success", quality=0.9)] * 5
        + [_obs("B", "success")] * 5  # no scores on treatment
    )
    delta = compute_quality_delta("triage", observations, min_runs_per_branch=5)
    assert delta is not None
    assert delta.quality_score_delta is None


def test_quality_score_delta_arithmetic() -> None:
    observations = (
        [_obs("A", "success", quality=0.9)] * 5
        + [_obs("B", "success", quality=0.6)] * 5
    )
    delta = compute_quality_delta("triage", observations, min_runs_per_branch=5)
    assert delta is not None
    assert delta.quality_score_delta == pytest.approx(0.3)


# ----------------------------------------------------------------------
# Branch assignment
# ----------------------------------------------------------------------


THRESHOLD = 50
BIG_TEXT = " ".join(f"row-{i} status=ok latency={i % 97}ms" for i in range(200))


@pytest.fixture(autouse=True)
def clean_kill_switch():
    _clear_disabled_tasks()
    yield
    _clear_disabled_tasks()


@pytest.fixture()
def events(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser.emit_policy_event",
        lambda run_id, kind, payload: captured.append((kind, payload)),
    )
    return captured


@pytest.fixture()
def model_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake(model: str, prompt: str, max_tokens: int) -> str:
        calls.append(model)
        return "condensed summary"

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._anthropic_summary", fake
    )
    return calls


def _ctx(run_id: str = "run-1") -> CallContext:
    return CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id=run_id,
        request_kwargs={
            "messages": [
                {"role": "user", "content": "what failed?"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": BIG_TEXT,
                        }
                    ],
                },
            ]
        },
    )


def _result_content(ctx: CallContext) -> Any:
    return ctx.request_kwargs["messages"][1]["content"][0]["content"]


def test_control_branch_keeps_raw_results(events, model_calls, monkeypatch) -> None:
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", lambda run_id: "triage"
    )
    policy = CheapSummariser(
        threshold_tokens=THRESHOLD,
        ab_mode=True,
        ab_sample_rate=0.10,
        rng=lambda: 0.05,  # < sample rate -> control
    )
    ctx = _ctx()
    policy.before_call(ctx)

    assert _result_content(ctx) == BIG_TEXT
    assert model_calls == []
    assignment = [e for e in events if e[0] == "summariser_ab_assignment"]
    assert assignment == [
        ("summariser_ab_assignment", {"task": "triage", "branch": "A"})
    ]


def test_treatment_branch_summarises(events, model_calls, monkeypatch) -> None:
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", lambda run_id: "triage"
    )
    policy = CheapSummariser(
        threshold_tokens=THRESHOLD,
        ab_mode=True,
        ab_sample_rate=0.10,
        rng=lambda: 0.95,  # >= sample rate -> treatment
    )
    ctx = _ctx()
    policy.before_call(ctx)

    assert _result_content(ctx) == "condensed summary"
    assignment = [e for e in events if e[0] == "summariser_ab_assignment"]
    assert assignment == [
        ("summariser_ab_assignment", {"task": "triage", "branch": "B"})
    ]


def test_branch_assignment_is_sticky_per_run(events, model_calls, monkeypatch) -> None:
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", lambda run_id: "triage"
    )
    rolls = iter([0.05, 0.95, 0.95])  # only the first roll may be consumed
    policy = CheapSummariser(
        threshold_tokens=THRESHOLD,
        ab_mode=True,
        rng=lambda: next(rolls),
    )
    for _ in range(3):
        ctx = _ctx(run_id="run-1")
        policy.before_call(ctx)
        assert _result_content(ctx) == BIG_TEXT  # control on every turn

    assignment = [e for e in events if e[0] == "summariser_ab_assignment"]
    assert len(assignment) == 1  # one event per run, not per turn


def test_ab_mode_without_task_summarises_unconditionally(
    events, model_calls, monkeypatch
) -> None:
    """No task -> no pairable population -> A/B machinery stays out
    of the way and the result is summarised as usual."""
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", lambda run_id: None
    )
    policy = CheapSummariser(
        threshold_tokens=THRESHOLD, ab_mode=True, rng=lambda: 0.0
    )
    ctx = _ctx()
    policy.before_call(ctx)
    assert _result_content(ctx) == "condensed summary"
    assert [e[0] for e in events] == ["summariser_replaced"]


# ----------------------------------------------------------------------
# Regression auto-disable
# ----------------------------------------------------------------------


def _seed_history(
    storage: SQLiteStorage,
    *,
    task: str,
    control: list[str],
    treatment: list[str],
) -> None:
    """Insert completed runs with A/B assignment events + outcomes."""
    conn = storage._conn()  # noqa: SLF001
    seq = 0
    for i, (branch, outcome) in enumerate(
        [("A", o) for o in control] + [("B", o) for o in treatment]
    ):
        run_id = f"hist-{i}"
        storage.start_run(
            run_id=run_id, task=task, agent_kind=None, started_at=1000 + i
        )
        seq += 1
        storage.insert_event(
            event_id=f"ev-{i}",
            run_id=run_id,
            kind="summariser_ab_assignment",
            occurred_at=1001 + i,
            sequence=1,
            payload_json=json.dumps({"task": task, "branch": branch}),
        )
        conn.execute(
            "UPDATE runs SET outcome = ?, status = 'complete' WHERE id = ?",
            (outcome, run_id),
        )
    conn.commit()


def _live_run(storage: SQLiteStorage, task: str, run_id: str = "run-live") -> None:
    storage.start_run(
        run_id=run_id, task=task, agent_kind=None, started_at=5000
    )


def test_regression_auto_disables_task(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    _seed_history(
        storage,
        task="triage",
        control=["success"] * 5,
        treatment=["failure", "failure", "failure", "success", "success"],
    )
    _live_run(storage, "triage")
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    policy = CheapSummariser(
        threshold_tokens=THRESHOLD, ab_mode=True, rng=lambda: 0.95
    )
    ctx = _ctx(run_id="run-live")
    policy.before_call(ctx)

    # Raw result kept: the task is disabled from this run onward.
    assert _result_content(ctx) == BIG_TEXT
    assert summariser_disabled_for_task("triage")

    regression = [e for e in events if e[0] == "summariser_quality_regression"]
    assert len(regression) == 1
    payload = regression[0][1]
    assert payload["task"] == "triage"
    assert payload["control_runs"] == 5
    assert payload["treatment_runs"] == 5
    assert payload["success_rate_drop"] == pytest.approx(0.6)
    assert payload["threshold"] == pytest.approx(0.05)


def test_no_disable_below_threshold(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    _seed_history(
        storage,
        task="triage",
        control=["success"] * 5,
        treatment=["success"] * 5,
    )
    _live_run(storage, "triage")
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    policy = CheapSummariser(
        threshold_tokens=THRESHOLD, ab_mode=True, rng=lambda: 0.95
    )
    ctx = _ctx(run_id="run-live")
    policy.before_call(ctx)

    assert _result_content(ctx) == "condensed summary"
    assert not summariser_disabled_for_task("triage")
    assert [e[0] for e in events if e[0] == "summariser_quality_regression"] == []


def test_no_disable_below_min_runs(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    """A one-sided or tiny sample must not flip the kill switch."""
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    _seed_history(
        storage,
        task="triage",
        control=["success"] * 5,
        treatment=["failure"] * 4,  # one short of regression_min_runs
    )
    _live_run(storage, "triage")
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    policy = CheapSummariser(
        threshold_tokens=THRESHOLD, ab_mode=True, rng=lambda: 0.95
    )
    ctx = _ctx(run_id="run-live")
    policy.before_call(ctx)

    assert _result_content(ctx) == "condensed summary"
    assert not summariser_disabled_for_task("triage")


def test_regression_check_runs_once_per_run(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    _live_run(storage, "triage")
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    gather_calls: list[str] = []
    real_gather = (
        __import__(
            "inkfoot.policy.cheap_summariser", fromlist=["x"]
        )._gather_ab_observations
    )

    def spy(storage_arg, task, **kw):
        gather_calls.append(task)
        return real_gather(storage_arg, task, **kw)

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._gather_ab_observations", spy
    )

    policy = CheapSummariser(
        threshold_tokens=THRESHOLD, ab_mode=True, rng=lambda: 0.95
    )
    for _ in range(3):
        policy.before_call(_ctx(run_id="run-live"))
    assert gather_calls == ["triage"]


# ----------------------------------------------------------------------
# The report-side smell
# ----------------------------------------------------------------------


def test_smell_is_registered_in_defaults() -> None:
    assert SUMMARISER_QUALITY_REGRESSION in DEFAULT_SMELLS
    assert get_smell("summariser-quality-regression").severity == "critical"
    assert (
        get_smell("summariser-quality-regression").primary_category
        == "summariser_tokens"
    )


def test_smell_fires_on_regression_event() -> None:
    payload = {
        "task": "triage",
        "control_runs": 5,
        "treatment_runs": 5,
        "control_success_rate": 1.0,
        "treatment_success_rate": 0.4,
        "success_rate_drop": 0.6,
        "quality_score_delta": None,
        "threshold": 0.05,
    }
    events_stream = [
        {"kind": "llm_call", "sequence": 1, "payload_json": "{}"},
        {
            "kind": "summariser_quality_regression",
            "sequence": 2,
            "payload_json": json.dumps(payload),
        },
    ]
    result = SUMMARISER_QUALITY_REGRESSION.detect({"id": "r1"}, events_stream)
    assert result is not None
    assert result.severity == "critical"
    assert result.triggered_at_sequence == 2
    assert result.evidence["task"] == "triage"
    assert result.evidence["success_rate_drop"] == pytest.approx(0.6)
    assert result.estimated_cost_impact_nd == 0


def test_smell_silent_on_clean_run() -> None:
    events_stream = [
        {"kind": "llm_call", "sequence": 1, "payload_json": "{}"},
        {"kind": "summariser_replaced", "sequence": 2, "payload_json": "{}"},
    ]
    assert (
        SUMMARISER_QUALITY_REGRESSION.detect({"id": "r1"}, events_stream) is None
    )


def test_smell_survives_malformed_payload() -> None:
    events_stream = [
        {
            "kind": "summariser_quality_regression",
            "sequence": 3,
            "payload_json": "{not json",
        },
    ]
    result = SUMMARISER_QUALITY_REGRESSION.detect({"id": "r1"}, events_stream)
    assert result is not None
    assert result.evidence == {}
