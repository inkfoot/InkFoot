"""Tests for ``inkfoot contract check`` benchmark evaluation."""

from __future__ import annotations

from inkfoot.benchmark.schema import BenchmarkArtifact, ScenarioResult
from inkfoot.contracts.check import check_contracts, render_json, render_markdown
from inkfoot.contracts.schema import BudgetClause, Contract, OutcomeClause


def _artifact(scenario: ScenarioResult) -> BenchmarkArtifact:
    return BenchmarkArtifact(
        inkfoot_version="0.0.0",
        schema_version="1",
        captured_at="2026-01-01T00:00:00Z",
        scenarios=(scenario,),
    )


def _scenario(
    *,
    task: str = "triage",
    runs: int = 100,
    successes: int = 96,
    p95: int = 40_000_000,
    mean_calls: float = 5.0,
    cache: float = 0.8,
) -> ScenarioResult:
    return ScenarioResult(
        task=task,
        runs=runs,
        successes=successes,
        p50_nanodollars=p95 // 2,
        p95_nanodollars=p95,
        mean_llm_calls=mean_calls,
        mean_cache_hit_rate=cache,
    )


def _contract(
    *,
    max_nanodollars: int | None = None,
    max_llm_calls: int | None = None,
    cache_hit_rate_min: float | None = None,
    required_success_rate: float | None = None,
) -> Contract:
    return Contract(
        schema_version=1,
        task="triage",
        budget=BudgetClause(
            max_nanodollars=max_nanodollars,
            max_llm_calls=max_llm_calls,
            cache_hit_rate_min=cache_hit_rate_min,
        ),
        outcome=OutcomeClause(required_success_rate=required_success_rate),
    )


def _status_for(report, clause: str) -> str:
    for r in report.results:
        if r.clause_name == clause:
            return r.status
    raise AssertionError(f"no result for clause {clause!r}")


def test_budget_within_ceiling_passes() -> None:
    report = check_contracts(
        {"triage": _contract(max_nanodollars=60_000_000)},
        _artifact(_scenario(p95=40_000_000)),
    )
    assert report.exit_code == 0
    assert _status_for(report, "max_nanodollars") == "ok"


def test_budget_violation_exits_2() -> None:
    report = check_contracts(
        {"triage": _contract(max_nanodollars=30_000_000)},
        _artifact(_scenario(p95=40_000_000)),
    )
    assert report.exit_code == 2
    assert _status_for(report, "max_nanodollars") == "violation"


def test_cache_floor_violation_exits_2() -> None:
    # Observed cache rate 0.50 is below the contract floor 0.70.
    report = check_contracts(
        {"triage": _contract(cache_hit_rate_min=0.70)},
        _artifact(_scenario(cache=0.50)),
    )
    assert report.exit_code == 2
    assert _status_for(report, "cache_hit_rate_min") == "violation"


def test_soft_warning_band_exits_1() -> None:
    # Observed p95 is 95% of the ceiling → within the 10% warn band.
    report = check_contracts(
        {"triage": _contract(max_nanodollars=42_000_000)},
        _artifact(_scenario(p95=40_000_000)),
    )
    assert report.exit_code == 1
    assert _status_for(report, "max_nanodollars") == "warn"


def test_outcome_clause_is_advisory_and_never_fails() -> None:
    # Observed success rate 0.50 is far below the required 0.95, yet the
    # build must not fail on it.
    report = check_contracts(
        {"triage": _contract(required_success_rate=0.95)},
        _artifact(_scenario(runs=100, successes=50)),
    )
    assert report.exit_code == 0
    assert _status_for(report, "required_success_rate") == "advisory"


def test_outcome_tagged_advisory_in_markdown() -> None:
    report = check_contracts(
        {"triage": _contract(required_success_rate=0.95)},
        _artifact(_scenario()),
    )
    md = render_markdown(report)
    assert "advisory" in md


def test_missing_scenario_is_not_evaluable_not_a_failure() -> None:
    report = check_contracts(
        {"triage": _contract(max_nanodollars=1)},
        _artifact(_scenario(task="something-else")),
    )
    assert report.exit_code == 0
    assert any(r.status == "not_evaluable" for r in report.results)


def test_unmeasurable_clauses_reported_not_gated() -> None:
    contract = Contract(
        schema_version=1,
        task="triage",
        budget=BudgetClause(max_tool_result_tokens=1500),
    )
    report = check_contracts({"triage": contract}, _artifact(_scenario()))
    assert report.exit_code == 0
    assert _status_for(report, "max_tool_result_tokens") == "not_evaluable"


def test_json_render_round_trips() -> None:
    import json

    report = check_contracts(
        {"triage": _contract(max_nanodollars=30_000_000)},
        _artifact(_scenario(p95=40_000_000)),
    )
    parsed = json.loads(render_json(report))
    assert parsed["kind"] == "contract_check"
    assert parsed["exit_code"] == 2
    assert parsed["results"][0]["clause_name"] == "max_nanodollars"
