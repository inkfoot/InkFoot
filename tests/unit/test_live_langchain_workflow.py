"""Static-shape gate for the weekly live LangChain workflow.

The weekly run is the only place partner-package drift surfaces (the
unit suite runs on golden fixtures), so its shape is pinned: a
dropped integration leg, a lost cron trigger, a silently-removed
status artifact, or a disabled failure-issue step should fail CI,
not go unnoticed until the handler rots against upstream.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_WF = REPO_ROOT / ".github" / "workflows" / "live-langchain.yml"

INTEGRATIONS = (
    "anthropic",
    "openai",
    "azure",
    "gemini",
    "bedrock",
)

yaml_required = pytest.mark.skipif(
    importlib.util.find_spec("yaml") is None,
    reason="pyyaml not installed (install with: pip install -e \".[dev]\")",
)


def _load() -> dict:
    import yaml

    return yaml.safe_load(LIVE_WF.read_text(encoding="utf-8"))


@yaml_required
def test_live_langchain_workflow_exists_and_parses() -> None:
    assert LIVE_WF.exists(), f"missing {LIVE_WF}"
    assert _load()


@yaml_required
def test_live_langchain_workflow_runs_weekly_and_on_dispatch() -> None:
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
def test_live_langchain_workflow_matrixes_every_integration() -> None:
    wf = _load()
    strategy = wf["jobs"]["integration"]["strategy"]
    legs = {
        entry["integration"] for entry in strategy["matrix"]["include"]
    }
    assert legs == set(INTEGRATIONS)
    # fail-fast would cancel the surviving legs the moment one
    # integration drifts — the whole point is per-integration status.
    assert strategy["fail-fast"] is False


@yaml_required
def test_live_langchain_workflow_installs_the_partner_package() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["integration"]["steps"]
    )
    assert "matrix.packages" in runs
    assert "pip install" in runs
    # The handler ships behind the langchain extra.
    assert "langchain" in runs


@yaml_required
def test_live_langchain_workflow_runs_the_marked_live_suite() -> None:
    wf = _load()
    runs = " ".join(
        step.get("run", "")
        for step in wf["jobs"]["integration"]["steps"]
    )
    assert "tests/integration/test_langchain_e2e.py" in runs
    assert "matrix.marker" in runs


@yaml_required
def test_live_langchain_workflow_publishes_status_and_artifact() -> None:
    wf = _load()
    steps = wf["jobs"]["integration"]["steps"]
    runs = " ".join(step.get("run", "") for step in steps)
    assert "GITHUB_STEP_SUMMARY" in runs
    upload_steps = [
        s for s in steps if "upload-artifact" in s.get("uses", "")
    ]
    assert upload_steps, "no results artifact uploaded"
    # The status row must be written even when a step fails — that is
    # what makes the run usable as a status page.
    assert upload_steps[0].get("if") == "always()"


@yaml_required
def test_live_langchain_workflow_opens_an_issue_on_failure() -> None:
    wf = _load()
    report = wf["jobs"]["report-failure"]
    assert report["needs"] == "integration"
    assert "failure()" in report["if"]
    assert report["permissions"]["issues"] == "write"
    runs = " ".join(step.get("run", "") for step in report["steps"])
    assert "gh issue create" in runs
    # Repeat failures append to the open issue instead of spamming
    # a fresh one every week.
    assert "gh issue comment" in runs
