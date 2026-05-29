"""Unit tests for ``inkfoot.benchmark.scenario`` (Phase 1 / E2-S1 T1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.benchmark.scenario import (
    Scenario,
    ScenarioLoader,
    ScenarioLoadError,
)


def _write_scenario(
    path: Path,
    *,
    task: str = "demo",
    fixtures: list[str] | None = None,
    runs_per_fixture: int = 1,
    expected_outcome: str = "success",
    body: str | None = None,
) -> Path:
    fixtures = fixtures or []
    body = body or (
        f"INKFOOT_SCENARIO = {{\n"
        f"  'task': {task!r},\n"
        f"  'fixtures': {fixtures!r},\n"
        f"  'runs_per_fixture': {runs_per_fixture},\n"
        f"  'expected_outcome': {expected_outcome!r},\n"
        f"}}\n"
        f"\n"
        f"def run(fixture):\n"
        f"    return {{'fixture': fixture, 'task': {task!r}}}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def test_loader_discovers_scenarios_in_lex_order(tmp_path: Path):
    _write_scenario(tmp_path / "b_second.py", task="b")
    _write_scenario(tmp_path / "a_first.py", task="a")
    loader = ScenarioLoader()
    found = loader.discover(tmp_path)
    assert [s.task for s in found] == ["a", "b"]


def test_loader_skips_underscore_and_conftest(tmp_path: Path):
    _write_scenario(tmp_path / "_helper.py", task="helper")
    _write_scenario(tmp_path / "conftest.py", task="conf")
    _write_scenario(tmp_path / "real.py", task="real")
    loader = ScenarioLoader()
    found = loader.discover(tmp_path)
    assert [s.task for s in found] == ["real"]


def test_loader_rejects_missing_directory(tmp_path: Path):
    loader = ScenarioLoader()
    with pytest.raises(FileNotFoundError):
        loader.discover(tmp_path / "nope")


def test_loader_rejects_not_a_directory(tmp_path: Path):
    file_path = tmp_path / "a-file.txt"
    file_path.write_text("hi")
    loader = ScenarioLoader()
    with pytest.raises(NotADirectoryError):
        loader.discover(file_path)


def test_loader_rejects_scenario_without_inkfoot_meta(tmp_path: Path):
    path = tmp_path / "broken.py"
    path.write_text("def run(fixture):\n    return 1\n")
    loader = ScenarioLoader()
    with pytest.raises(ScenarioLoadError, match="INKFOOT_SCENARIO"):
        loader.discover(tmp_path)


def test_loader_rejects_scenario_without_run_callable(tmp_path: Path):
    path = tmp_path / "broken.py"
    path.write_text(
        "INKFOOT_SCENARIO = {'task': 'x', 'fixtures': [], 'expected_outcome': 'success'}\n"
    )
    loader = ScenarioLoader()
    with pytest.raises(ScenarioLoadError, match="run\\(fixture\\)"):
        loader.discover(tmp_path)


def test_loader_rejects_runs_per_fixture_zero(tmp_path: Path):
    _write_scenario(tmp_path / "demo.py", task="x", runs_per_fixture=0)
    loader = ScenarioLoader()
    with pytest.raises(ScenarioLoadError, match="runs_per_fixture"):
        loader.discover(tmp_path)


def test_loader_handles_scenario_with_no_fixtures(tmp_path: Path):
    _write_scenario(tmp_path / "smoke.py", task="smoke", fixtures=[])
    loader = ScenarioLoader()
    scenario = loader.discover(tmp_path)[0]
    fixtures = list(loader.iter_fixture_payloads(scenario))
    assert len(fixtures) == 1
    label, payload = fixtures[0]
    assert label.endswith("default")
    assert payload is None


def test_loader_loads_json_fixtures(tmp_path: Path):
    fixture_path = tmp_path / "ticket.json"
    fixture_path.write_text(json.dumps({"ticket_id": 42}))
    _write_scenario(
        tmp_path / "demo.py",
        task="demo",
        fixtures=["ticket.json"],
    )
    loader = ScenarioLoader()
    scenario = loader.discover(tmp_path)[0]
    fixtures = list(loader.iter_fixture_payloads(scenario))
    assert fixtures == [(str(fixture_path), {"ticket_id": 42})]


def test_loader_raises_on_missing_fixture(tmp_path: Path):
    _write_scenario(
        tmp_path / "demo.py",
        task="demo",
        fixtures=["does-not-exist.json"],
    )
    loader = ScenarioLoader()
    scenario = loader.discover(tmp_path)[0]
    with pytest.raises(FileNotFoundError):
        list(loader.iter_fixture_payloads(scenario))


def test_loader_returns_scenario_with_path_and_name(tmp_path: Path):
    path = _write_scenario(tmp_path / "ticket_triage.py", task="ticket-triage")
    loader = ScenarioLoader()
    scenario = loader.discover(tmp_path)[0]
    assert isinstance(scenario, Scenario)
    assert scenario.name == "ticket_triage"
    assert scenario.path == path
