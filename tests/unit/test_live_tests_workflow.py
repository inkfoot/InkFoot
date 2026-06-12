"""Static-shape gate for the weekly live-tests workflow.

The weekly run is the only place adapter/SDK drift surfaces (unit
tests run on stubs), so its shape is pinned the same way the release
workflows are: a dropped framework or provider leg, a lost cron
trigger, or a silently-removed status artifact should fail CI, not
go unnoticed until the integrations rot.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_WF = REPO_ROOT / ".github" / "workflows" / "live-tests.yml"

FRAMEWORKS = (
    "langgraph",
    "openai-agents",
    "anthropic-agent",
    "pydantic-ai",
    "crewai",
)

PROVIDERS = (
    "gemini",
    "bedrock",
    "ollama",
)

yaml_required = pytest.mark.skipif(
    importlib.util.find_spec("yaml") is None,
    reason="pyyaml not installed (install with: pip install -e \".[dev]\")",
)


def _load() -> dict:
    import yaml

    return yaml.safe_load(LIVE_WF.read_text(encoding="utf-8"))


@yaml_required
def test_live_workflow_exists_and_parses() -> None:
    assert LIVE_WF.exists(), f"missing {LIVE_WF}"
    assert _load()


@yaml_required
def test_live_workflow_runs_weekly_and_on_dispatch() -> None:
    wf = _load()
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    crons = [entry["cron"] for entry in on["schedule"]]
    assert len(crons) == 1
    # Five cron fields, day-of-week pinned (weekly, not daily).
    fields = crons[0].split()
    assert len(fields) == 5
    assert fields[2] == "*" and fields[3] == "*" and fields[4] != "*"
    assert "workflow_dispatch" in on


@yaml_required
def test_live_workflow_matrixes_every_framework_extra() -> None:
    wf = _load()
    matrix = wf["jobs"]["framework"]["strategy"]["matrix"]["framework"]
    assert set(matrix) == set(FRAMEWORKS)
    # fail-fast would cancel the surviving legs the moment one
    # framework drifts — the whole point is per-framework status.
    assert wf["jobs"]["framework"]["strategy"]["fail-fast"] is False


@yaml_required
def test_live_workflow_matrixes_every_provider_leg() -> None:
    wf = _load()
    strategy = wf["jobs"]["provider"]["strategy"]
    legs = {
        entry["provider"] for entry in strategy["matrix"]["include"]
    }
    assert legs == set(PROVIDERS)
    assert strategy["fail-fast"] is False


@yaml_required
def test_live_workflow_installs_the_matrix_extra() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["framework"]["steps"]
    )
    assert "matrix.framework" in runs
    assert "pip install" in runs


@yaml_required
def test_live_workflow_installs_the_provider_sdk() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["provider"]["steps"]
    )
    assert "matrix.extras" in runs
    assert "pip install" in runs


@yaml_required
def test_live_workflow_runs_contract_and_integration_suites() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["framework"]["steps"]
    )
    assert "tests/contract" in runs
    assert "tests/integration" in runs


@yaml_required
def test_live_workflow_runs_provider_contract_and_integration() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["provider"]["steps"]
    )
    assert "tests/unit/test_provider_abstraction.py" in runs
    assert "matrix.tests" in runs


@yaml_required
@pytest.mark.parametrize("job", ["framework", "provider"])
def test_live_workflow_publishes_status_summary_and_artifact(
    job: str,
) -> None:
    wf = _load()
    steps = wf["jobs"][job]["steps"]
    runs = " ".join(step.get("run", "") for step in steps)
    assert "GITHUB_STEP_SUMMARY" in runs
    upload_steps = [
        s for s in steps if "upload-artifact" in s.get("uses", "")
    ]
    assert upload_steps, "no results artifact uploaded"
    # The status row must be written even when a step fails — that is
    # what makes the run usable as a status page.
    assert upload_steps[0].get("if") == "always()"
