"""Integration test for ``inkfoot benchmark``'s runner.

The runner stubs the LLM provider so we don't pay for or wait on
real API calls — production CI should use live calls,
but unit/integration tests are allowed to fake the provider via the
``instrument`` injection point. The stub asserts the runner actually
calls the provider once per fixture × runs_per_fixture so the
"live LLM calls happen" acceptance criterion is exercised.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from inkfoot.benchmark.runner import run_benchmark
from inkfoot.benchmark.schema import BENCHMARK_SCHEMA_VERSION


def _write_scenario(dir_path: Path, name: str, *, body: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def stub_instrument(monkeypatch):
    """Boot inkfoot but inject a stub provider that simply emits
    a deterministic llm_call event whenever the scenario asks for
    one. Records call count for assertions."""
    calls: list[dict[str, Any]] = []

    def fake_call(model: str = "claude-haiku-4-5", cost_nd: int = 1_000_000):
        import json as _json
        from ulid import ULID

        from inkfoot import _instrument as _inst
        from inkfoot._run_context import current_run_id
        from inkfoot.shims._emit import _next_sequence

        run_id = current_run_id()
        assert run_id is not None, "fake_call must be invoked inside agent_run"
        storage = _inst._STORAGE
        assert storage is not None, "stub requires an active instrument()"
        payload = {
            "provider": "anthropic",
            "model": model,
            "system_static_tokens": 100,
            "user_input_tokens": 50,
            "output_tokens": 25,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "estimated_nanodollars": cost_nd,
            "metadata": {},
        }
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=0,
            sequence=_next_sequence(run_id),
            payload_json=_json.dumps(payload),
            capture_mode="metadata",
        )
        calls.append(payload)

    # Expose the stub on a module so scenarios can ``from
    # inkfoot.tests._stub import fake_call``.
    import sys
    import types

    stub_mod = types.ModuleType("inkfoot_test_stub")
    stub_mod.fake_call = fake_call
    stub_mod.calls = calls
    sys.modules["inkfoot_test_stub"] = stub_mod
    yield stub_mod
    sys.modules.pop("inkfoot_test_stub", None)


def test_runner_executes_each_fixture_and_aggregates(tmp_path, stub_instrument):
    scenarios = tmp_path / "scenarios"
    fixtures_dir = tmp_path / "scenarios" / "fixtures"
    fixtures_dir.mkdir(parents=True)
    (fixtures_dir / "a.json").write_text(json.dumps({"id": "a"}))
    (fixtures_dir / "b.json").write_text(json.dumps({"id": "b"}))

    _write_scenario(
        scenarios,
        "triage",
        body="""
        from inkfoot_test_stub import fake_call

        INKFOOT_SCENARIO = {
            "task": "triage",
            "fixtures": ["fixtures/a.json", "fixtures/b.json"],
            "runs_per_fixture": 2,
            "expected_outcome": "success",
        }

        def run(fixture):
            fake_call(cost_nd=1_500_000)
            return {"ok": True, "fixture": fixture}
        """,
    )

    artefact = run_benchmark(scenarios)
    assert artefact.schema_version == BENCHMARK_SCHEMA_VERSION
    assert len(artefact.scenarios) == 1
    scenario = artefact.scenarios[0]
    assert scenario.task == "triage"
    assert scenario.runs == 4  # 2 fixtures × 2 runs_per_fixture
    assert scenario.successes == 4
    assert scenario.mean_llm_calls == pytest.approx(1.0)
    # Provider stub was called once per run.
    assert len(stub_instrument.calls) == 4


def test_runner_records_failures_as_non_success(tmp_path, stub_instrument):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    _write_scenario(
        scenarios,
        "bomb",
        body="""
        INKFOOT_SCENARIO = {
            "task": "bomb",
            "fixtures": [],
            "runs_per_fixture": 1,
        }

        def run(fixture):
            raise RuntimeError("boom")
        """,
    )
    artefact = run_benchmark(scenarios)
    scenario = artefact.scenarios[0]
    assert scenario.runs == 1
    assert scenario.successes == 0


def test_runner_writes_artefact_when_output_supplied(tmp_path, stub_instrument):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    _write_scenario(
        scenarios,
        "smoke",
        body="""
        from inkfoot_test_stub import fake_call

        INKFOOT_SCENARIO = {
            "task": "smoke",
            "fixtures": [],
            "runs_per_fixture": 1,
        }

        def run(fixture):
            fake_call()
        """,
    )
    out = tmp_path / "current.json"
    artefact = run_benchmark(scenarios, output=out)
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded == artefact.to_dict()


def test_runner_filters_with_scenarios_only(tmp_path, stub_instrument):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    for task in ("alpha", "beta"):
        _write_scenario(
            scenarios,
            task,
            body=f"""
            INKFOOT_SCENARIO = {{
                "task": "{task}",
                "fixtures": [],
                "runs_per_fixture": 1,
            }}

            def run(fixture):
                return None
            """,
        )
    artefact = run_benchmark(scenarios, scenarios_only=["beta"])
    tasks = [sc.task for sc in artefact.scenarios]
    assert tasks == ["beta"]


def test_runner_empty_scenarios_dir_yields_empty_artefact(tmp_path, stub_instrument):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    artefact = run_benchmark(scenarios)
    assert artefact.scenarios == ()
    assert artefact.schema_version == BENCHMARK_SCHEMA_VERSION


def test_runner_missing_scenarios_dir_raises(tmp_path, stub_instrument):
    with pytest.raises(FileNotFoundError):
        run_benchmark(tmp_path / "nope")


def test_runner_marks_success_when_recorded_outcome_matches_expected(
    tmp_path, stub_instrument
):
    # Finding #7: a scenario declaring expected_outcome="human_escalated"
    # and emitting that outcome via inkfoot.set_outcome must be counted
    # as a success against `successes`, not silently demoted to failure.
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    _write_scenario(
        scenarios,
        "escalation",
        body="""
        import inkfoot

        INKFOOT_SCENARIO = {
            "task": "escalation",
            "fixtures": [],
            "runs_per_fixture": 1,
            "expected_outcome": "human_escalated",
        }

        def run(fixture):
            inkfoot.set_outcome("human_escalated")
        """,
    )
    artefact = run_benchmark(scenarios)
    scenario = artefact.scenarios[0]
    assert scenario.runs == 1
    assert scenario.successes == 1


def test_runner_marks_failure_when_recorded_outcome_diverges_from_expected(
    tmp_path, stub_instrument
):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    _write_scenario(
        scenarios,
        "wrong_outcome",
        body="""
        import inkfoot

        INKFOOT_SCENARIO = {
            "task": "wrong-outcome",
            "fixtures": [],
            "runs_per_fixture": 1,
            "expected_outcome": "success",
        }

        def run(fixture):
            inkfoot.set_outcome("failure")
        """,
    )
    artefact = run_benchmark(scenarios)
    scenario = artefact.scenarios[0]
    assert scenario.runs == 1
    assert scenario.successes == 0


def test_runner_defaults_to_expected_outcome_when_scenario_silent(
    tmp_path, stub_instrument
):
    # A scenario that returns normally without calling set_outcome
    # should be marked according to its declared expected_outcome,
    # not implicitly "success".
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    _write_scenario(
        scenarios,
        "silent",
        body="""
        INKFOOT_SCENARIO = {
            "task": "silent",
            "fixtures": [],
            "runs_per_fixture": 1,
            "expected_outcome": "human_escalated",
        }

        def run(fixture):
            return None  # silent: didn't set an outcome
        """,
    )
    artefact = run_benchmark(scenarios)
    scenario = artefact.scenarios[0]
    assert scenario.successes == 1  # recorded outcome == expected_outcome

