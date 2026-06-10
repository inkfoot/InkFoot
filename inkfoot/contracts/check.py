"""Evaluate Token Contracts against a benchmark artefact — the CI gate.

``inkfoot contract check ./contracts --against current.json`` matches
each contract to the benchmark scenario of the same task name and
checks every measurable budget clause against that scenario's stats.
The result is an exit code CI can branch on:

* ``0`` — every budget clause is comfortably within its ceiling.
* ``1`` — soft warning: a clause is within 10% of its ceiling (or a
  floor is only just being cleared). Worth a look; not a failure.
* ``2`` — at least one budget clause is violated.

Outcome clauses (``required_success_rate``) are reported but never
affect the exit code: a benchmark scenario measures cost and shape
deterministically, but it can't stand in for production outcome
quality, so gating a build on it would be misleading. They are tagged
"(advisory)" in the output.

Two renderers are provided: ``markdown`` for the PR comment and
``json`` for machine composition with ``inkfoot diff``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from inkfoot.benchmark.schema import BenchmarkArtifact, ScenarioResult
from inkfoot.contracts.schema import BudgetClause, Contract

# A clause within this fraction of its ceiling (or this fraction above
# a floor) is flagged as a soft warning rather than passing silently —
# it gives a team lead-time before a regression actually breaks CI.
WARN_BAND = 0.10

# Status values, ordered by severity so the worst clause drives the
# overall verdict.
_OK = "ok"
_WARN = "warn"
_VIOLATION = "violation"
_ADVISORY = "advisory"
_NOT_EVALUABLE = "not_evaluable"

_EXIT_FOR_STATUS = {_OK: 0, _ADVISORY: 0, _NOT_EVALUABLE: 0, _WARN: 1, _VIOLATION: 2}


@dataclass(frozen=True)
class ClauseResult:
    """One clause's verdict against the benchmark."""

    task: str
    clause_name: str
    status: str
    observed: Optional[float]
    threshold: Optional[float]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "clause_name": self.clause_name,
            "status": self.status,
            "observed": self.observed,
            "threshold": self.threshold,
            "note": self.note,
        }


@dataclass(frozen=True)
class CheckReport:
    """The full set of clause verdicts plus the derived exit code."""

    results: tuple[ClauseResult, ...]

    @property
    def exit_code(self) -> int:
        return max(
            (_EXIT_FOR_STATUS.get(r.status, 0) for r in self.results), default=0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "contract_check",
            "exit_code": self.exit_code,
            "results": [r.to_dict() for r in self.results],
        }


def check_contracts(
    contracts: dict[str, Contract], artifact: BenchmarkArtifact
) -> CheckReport:
    """Evaluate every contract against its matching benchmark scenario."""
    scenarios = {s.task: s for s in artifact.scenarios}
    results: list[ClauseResult] = []
    for task in sorted(contracts):
        contract = contracts[task]
        scenario = scenarios.get(task)
        if scenario is None:
            results.append(
                ClauseResult(
                    task=task,
                    clause_name="(scenario)",
                    status=_NOT_EVALUABLE,
                    observed=None,
                    threshold=None,
                    note="no matching benchmark scenario for this task",
                )
            )
            continue
        results.extend(_check_budget(task, contract.budget, scenario))
        results.extend(_check_outcome(task, contract, scenario))
    return CheckReport(results=tuple(results))


def _check_budget(
    task: str, budget: Optional[BudgetClause], scenario: ScenarioResult
) -> list[ClauseResult]:
    if budget is None:
        return []
    out: list[ClauseResult] = []
    if budget.max_nanodollars is not None:
        out.append(
            _ceiling(
                task, "max_nanodollars", scenario.p95_nanodollars, budget.max_nanodollars
            )
        )
    if budget.max_llm_calls is not None:
        out.append(
            _ceiling(
                task, "max_llm_calls", scenario.mean_llm_calls, budget.max_llm_calls
            )
        )
    if budget.cache_hit_rate_min is not None:
        out.append(
            _floor(
                task,
                "cache_hit_rate_min",
                scenario.mean_cache_hit_rate,
                budget.cache_hit_rate_min,
            )
        )
    # ``max_tool_result_tokens`` and ``max_run_duration_seconds`` aren't
    # carried in the benchmark artefact, so they can't be gated here.
    # They're enforced at runtime instead; flag them as not-evaluable so
    # the report is honest about what CI did and didn't check.
    for name, value in (
        ("max_tool_result_tokens", budget.max_tool_result_tokens),
        ("max_run_duration_seconds", budget.max_run_duration_seconds),
    ):
        if value is not None:
            out.append(
                ClauseResult(
                    task=task,
                    clause_name=name,
                    status=_NOT_EVALUABLE,
                    observed=None,
                    threshold=float(value),
                    note="not measured by the benchmark; enforced at runtime",
                )
            )
    return out


def _check_outcome(
    task: str, contract: Contract, scenario: ScenarioResult
) -> list[ClauseResult]:
    outcome = contract.outcome
    if outcome is None or outcome.required_success_rate is None:
        return []
    observed = scenario.successes / scenario.runs if scenario.runs else 0.0
    status = _ADVISORY
    note = "advisory — outcome quality is not gated by CI"
    return [
        ClauseResult(
            task=task,
            clause_name="required_success_rate",
            status=status,
            observed=round(observed, 4),
            threshold=outcome.required_success_rate,
            note=note,
        )
    ]


def _ceiling(
    task: str, clause: str, observed: float, threshold: float
) -> ClauseResult:
    ratio = observed / threshold if threshold else 0.0
    if ratio > 1.0:
        status = _VIOLATION
    elif ratio >= 1.0 - WARN_BAND:
        status = _WARN
    else:
        status = _OK
    return ClauseResult(
        task=task,
        clause_name=clause,
        status=status,
        observed=float(observed),
        threshold=float(threshold),
    )


def _floor(
    task: str, clause: str, observed: float, threshold: float
) -> ClauseResult:
    if observed < threshold:
        status = _VIOLATION
    elif observed <= threshold * (1.0 + WARN_BAND):
        status = _WARN
    else:
        status = _OK
    return ClauseResult(
        task=task,
        clause_name=clause,
        status=status,
        observed=float(observed),
        threshold=float(threshold),
    )


# ----------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------

_STATUS_MARK = {
    _OK: "ok",
    _WARN: "warn",
    _VIOLATION: "VIOLATION",
    _ADVISORY: "(advisory)",
    _NOT_EVALUABLE: "not checked",
}


def render_markdown(report: CheckReport) -> str:
    """The PR-comment shape for ``inkfoot contract check``."""
    lines = ["## Token Contract check", ""]
    if not report.results:
        lines.append("_No contracts evaluated._")
        return "\n".join(lines) + "\n"
    lines.append("| Task | Clause | Status | Observed | Threshold |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in report.results:
        observed = "—" if r.observed is None else _fmt(r.observed)
        threshold = "—" if r.threshold is None else _fmt(r.threshold)
        mark = _STATUS_MARK.get(r.status, r.status)
        lines.append(
            f"| {r.task} | {r.clause_name} | {mark} | {observed} | {threshold} |"
        )
    verdict = {0: "passed", 1: "passed with warnings", 2: "failed"}[
        report.exit_code
    ]
    lines.append("")
    lines.append(f"**Verdict:** {verdict} (exit {report.exit_code}).")
    return "\n".join(lines) + "\n"


def render_json(report: CheckReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=False)


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"
