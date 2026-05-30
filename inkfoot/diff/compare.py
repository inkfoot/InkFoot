"""The pure comparison core for ``inkfoot diff``.

Inputs: two :class:`inkfoot.benchmark.schema.BenchmarkArtifact` —
the baseline and the current run. Output: a :class:`DiffReport`
with per-scenario deltas, smell deltas, an overall verdict, and a
``fail`` exit code (mapped by the CLI).

The verdict ladder is defined by :class:`Thresholds` (the documented threshold contract
§4.4); this module only *computes* and *labels*, it never renders.
That keeps ``compare_artifacts`` snapshot-testable: same inputs ->
same DiffReport instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Sequence

from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    ScenarioResult,
    SmellCount,
)
from inkfoot.diff.thresholds import Thresholds, THRESHOLD_PRESETS


class Verdict(str, Enum):
    """Comparison outcome — ordered from best to worst.

    The string values map directly into rendered output and CI exit
    codes (:attr:`Verdict.exit_code`). ``str`` is a parent so
    ``Verdict.OK == "ok"`` works in rendering glue without an explicit
    ``.value`` access.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"

    @property
    def exit_code(self) -> int:
        return _VERDICT_EXIT_CODES[self]


_VERDICT_EXIT_CODES: Mapping[Verdict, int] = {
    Verdict.OK: 0,
    Verdict.WARN: 1,
    Verdict.FAIL: 2,
}


# Stable ordering: a per-scenario verdict's contribution to the
# overall verdict is the *worst* across all scenarios.
_VERDICT_RANK: Mapping[Verdict, int] = {
    Verdict.OK: 0,
    Verdict.WARN: 1,
    Verdict.FAIL: 2,
}


def _worst(a: Verdict, b: Verdict) -> Verdict:
    return a if _VERDICT_RANK[a] >= _VERDICT_RANK[b] else b


@dataclass(frozen=True)
class SmellDelta:
    """Per-smell appearance delta within a scenario.

    ``baseline_count`` / ``current_count`` are absolute counts. The
    ``status`` field is one of ``"appeared"`` (new in current),
    ``"disappeared"`` (gone in current), ``"increased"`` (count
    grew), ``"decreased"`` (count fell), or ``"unchanged"``. The
    rendered markdown groups by status so the eye lands on the new
    findings first.
    """

    id: str
    baseline_count: int
    current_count: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "baseline_count": int(self.baseline_count),
            "current_count": int(self.current_count),
            "status": self.status,
        }


@dataclass(frozen=True)
class ScenarioDiff:
    """Per-scenario delta: numbers, smell changes, verdict, reasons.

    ``reasons`` is the list of human-readable strings explaining
    *why* the scenario landed at its verdict. Renderers concatenate
    them into the markdown comment's "regressions" section.
    """

    task: str
    baseline: Optional[ScenarioResult]
    current: Optional[ScenarioResult]
    p50_delta_fraction: Optional[float]
    p95_delta_fraction: Optional[float]
    cache_hit_delta: Optional[float]
    llm_calls_delta: Optional[float]
    success_rate_delta: Optional[float]
    smell_deltas: tuple[SmellDelta, ...]
    verdict: Verdict
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_new(self) -> bool:
        return self.baseline is None and self.current is not None

    @property
    def is_removed(self) -> bool:
        return self.current is None and self.baseline is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "current": self.current.to_dict() if self.current else None,
            "p50_delta_fraction": self.p50_delta_fraction,
            "p95_delta_fraction": self.p95_delta_fraction,
            "cache_hit_delta": self.cache_hit_delta,
            "llm_calls_delta": self.llm_calls_delta,
            "success_rate_delta": self.success_rate_delta,
            "smell_deltas": [s.to_dict() for s in self.smell_deltas],
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DiffReport:
    """Top-level result of comparing two artefacts."""

    baseline_version: str
    current_version: str
    baseline_captured_at: str
    current_captured_at: str
    thresholds: Thresholds
    scenario_diffs: tuple[ScenarioDiff, ...]
    verdict: Verdict
    summary_reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "baseline": {
                "inkfoot_version": self.baseline_version,
                "captured_at": self.baseline_captured_at,
            },
            "current": {
                "inkfoot_version": self.current_version,
                "captured_at": self.current_captured_at,
            },
            "thresholds": self.thresholds.name,
            "verdict": self.verdict.value,
            "summary_reasons": list(self.summary_reasons),
            "scenarios": [s.to_dict() for s in self.scenario_diffs],
        }

    @property
    def exit_code(self) -> int:
        return self.verdict.exit_code


# ----------------------------------------------------------------------
# Comparison entry point.
# ----------------------------------------------------------------------


def compare_artifacts(
    baseline: BenchmarkArtifact,
    current: BenchmarkArtifact,
    *,
    thresholds: Optional[Thresholds] = None,
) -> DiffReport:
    """Compare two artefacts and return a :class:`DiffReport`.

    A missing scenario in ``current`` is recorded as a ``warn``
    (deleted scenarios shouldn't silently sail through CI). A new
    scenario in ``current`` is ``ok`` — there's no baseline to
    regress against.
    """
    if baseline.schema_version != current.schema_version:
        # Hard error: refusing to silently coerce shapes from a
        # different schema version. The CLI surfaces this with the
        # remediation (re-run ``inkfoot benchmark``).
        raise ValueError(
            f"compare_artifacts: schema version mismatch "
            f"(baseline={baseline.schema_version!r}, "
            f"current={current.schema_version!r}). Re-run "
            f"`inkfoot benchmark` on both sides to align."
        )
    thresholds = thresholds or THRESHOLD_PRESETS["default"]
    baseline_by_task = {sc.task: sc for sc in baseline.scenarios}
    current_by_task = {sc.task: sc for sc in current.scenarios}

    all_tasks = sorted(set(baseline_by_task) | set(current_by_task))
    scenario_diffs: list[ScenarioDiff] = []
    overall = Verdict.OK
    summary_reasons: list[str] = []
    for task in all_tasks:
        diff = _diff_scenario(
            task=task,
            baseline=baseline_by_task.get(task),
            current=current_by_task.get(task),
            thresholds=thresholds,
        )
        scenario_diffs.append(diff)
        overall = _worst(overall, diff.verdict)
        if diff.verdict is not Verdict.OK:
            summary_reasons.extend(
                f"{task}: {reason}" for reason in diff.reasons
            )

    return DiffReport(
        baseline_version=baseline.inkfoot_version,
        current_version=current.inkfoot_version,
        baseline_captured_at=baseline.captured_at,
        current_captured_at=current.captured_at,
        thresholds=thresholds,
        scenario_diffs=tuple(scenario_diffs),
        verdict=overall,
        summary_reasons=tuple(summary_reasons),
    )


def _diff_scenario(
    *,
    task: str,
    baseline: Optional[ScenarioResult],
    current: Optional[ScenarioResult],
    thresholds: Thresholds,
) -> ScenarioDiff:
    """Compute the per-scenario delta + verdict.

    Five comparison axes: p50/p95 cost,
    cache-hit rate, LLM-call count, outcome rate, smell deltas.
    """
    if current is None and baseline is not None:
        return ScenarioDiff(
            task=task,
            baseline=baseline,
            current=None,
            p50_delta_fraction=None,
            p95_delta_fraction=None,
            cache_hit_delta=None,
            llm_calls_delta=None,
            success_rate_delta=None,
            smell_deltas=(),
            verdict=Verdict.WARN,
            reasons=(f"scenario {task!r} is missing from current artefact",),
        )
    if baseline is None and current is not None:
        # A new scenario has no baseline to compare cost against, but
        # if it ships *with* a critical smell already firing we want
        # the diff to fail loudly — otherwise a PR can introduce a
        # broken scenario and sail past CI (Finding #5). Build smell
        # deltas + verdict from current alone; the cost / cache /
        # outcome axes carry no signal here so they stay None.
        smell_deltas = tuple(
            SmellDelta(
                id=s.id,
                baseline_count=0,
                current_count=s.count,
                status="appeared",
            )
            for s in current.smells_seen
            if s.count > 0
        )
        verdict, reasons = _verdict(
            thresholds=thresholds,
            p50_delta=None,
            p95_delta=None,
            cache_delta=None,
            success_delta=None,
            smell_deltas=smell_deltas,
        )
        # Prepend the "new scenario" context so the rendered reason
        # explains why cost axes are blank.
        reasons = ("new scenario; no baseline to compare against",) + tuple(
            r for r in reasons if r != "within thresholds"
        )
        return ScenarioDiff(
            task=task,
            baseline=None,
            current=current,
            p50_delta_fraction=None,
            p95_delta_fraction=None,
            cache_hit_delta=None,
            llm_calls_delta=None,
            success_rate_delta=None,
            smell_deltas=smell_deltas,
            verdict=verdict,
            reasons=reasons,
        )

    # Defensive guard rather than ``assert`` (which strips under
    # ``python -O``). Both branches above handled the None cases;
    # reaching here with either None would be a real bug.
    if baseline is None or current is None:
        raise RuntimeError(
            f"_diff_scenario({task!r}): unreachable — both branches handled "
            f"missing inputs; got baseline={baseline!r} current={current!r}"
        )
    p50_delta = _fraction_change(baseline.p50_nanodollars, current.p50_nanodollars)
    p95_delta = _fraction_change(baseline.p95_nanodollars, current.p95_nanodollars)
    p50_cost_appeared = _cost_appeared(
        baseline.p50_nanodollars, current.p50_nanodollars
    )
    p95_cost_appeared = _cost_appeared(
        baseline.p95_nanodollars, current.p95_nanodollars
    )
    cache_delta = current.mean_cache_hit_rate - baseline.mean_cache_hit_rate
    calls_delta = (
        current.mean_llm_calls - baseline.mean_llm_calls
    )
    base_success_rate = (
        baseline.successes / baseline.runs if baseline.runs else 0.0
    )
    cur_success_rate = (
        current.successes / current.runs if current.runs else 0.0
    )
    success_delta = cur_success_rate - base_success_rate

    smell_deltas = _smell_deltas(baseline.smells_seen, current.smells_seen)

    verdict, reasons = _verdict(
        thresholds=thresholds,
        p50_delta=p50_delta,
        p95_delta=p95_delta,
        cache_delta=cache_delta,
        success_delta=success_delta,
        smell_deltas=smell_deltas,
        p50_cost_appeared=p50_cost_appeared,
        p95_cost_appeared=p95_cost_appeared,
    )
    return ScenarioDiff(
        task=task,
        baseline=baseline,
        current=current,
        p50_delta_fraction=p50_delta,
        p95_delta_fraction=p95_delta,
        cache_hit_delta=cache_delta,
        llm_calls_delta=calls_delta,
        success_rate_delta=success_delta,
        smell_deltas=smell_deltas,
        verdict=verdict,
        reasons=reasons,
    )


def _fraction_change(baseline: int, current: int) -> Optional[float]:
    """Signed fractional change (+0.20 means +20%).

    ``None`` when the baseline is zero — the ratio is undefined.
    Callers also pair this with :func:`_cost_appeared` to catch the
    "0 → X" case explicitly so a real cost regression doesn't
    disappear into a missing-data dash in the rendered diff
    (Finding #6).
    """
    if baseline <= 0:
        return None
    return (current - baseline) / baseline


def _cost_appeared(baseline: int, current: int) -> bool:
    """Did the cost go from zero to non-zero on this axis?

    Useful only when :func:`_fraction_change` returned ``None``
    (i.e. baseline was zero). When that's because the scenario
    previously had no measurable LLM calls and now does, we want a
    visible FAIL — not a silent dash on the table row.
    """
    return baseline <= 0 and current > 0


def _smell_deltas(
    baseline: Sequence[SmellCount],
    current: Sequence[SmellCount],
) -> tuple[SmellDelta, ...]:
    """Pair baseline + current smell counts by id, then label."""
    base_by_id = {s.id: s.count for s in baseline}
    cur_by_id = {s.id: s.count for s in current}
    all_ids = sorted(set(base_by_id) | set(cur_by_id))
    out: list[SmellDelta] = []
    for sid in all_ids:
        b = base_by_id.get(sid, 0)
        c = cur_by_id.get(sid, 0)
        if c == b == 0:
            continue
        if b == 0 and c > 0:
            status = "appeared"
        elif c == 0 and b > 0:
            status = "disappeared"
        elif c > b:
            status = "increased"
        elif c < b:
            status = "decreased"
        else:
            status = "unchanged"
        out.append(
            SmellDelta(id=sid, baseline_count=b, current_count=c, status=status)
        )
    return tuple(out)


def _verdict(
    *,
    thresholds: Thresholds,
    p50_delta: Optional[float],
    p95_delta: Optional[float],
    cache_delta: Optional[float],
    success_delta: Optional[float],
    smell_deltas: tuple[SmellDelta, ...],
    p50_cost_appeared: bool = False,
    p95_cost_appeared: bool = False,
) -> tuple[Verdict, tuple[str, ...]]:
    """Decide the per-scenario verdict given the deltas.

    We promote to ``fail`` on the first hard breach; otherwise
    ``warn`` accumulates. Order: cost p95 -> cost p50 -> cache
    drop -> outcome drop -> critical smell appearance.

    ``*_cost_appeared`` flips when the baseline cost on that axis
    was zero and the current is positive — represented as FAIL
    because "we measured nothing before and pay X now" is the
    sharpest possible regression signal (Finding #6).
    """
    reasons: list[str] = []
    verdict = Verdict.OK

    def _bump(target: Verdict, reason: str) -> None:
        nonlocal verdict
        verdict = _worst(verdict, target)
        reasons.append(reason)

    # Cost regressions.
    for label, delta, appeared in (
        ("p50", p50_delta, p50_cost_appeared),
        ("p95", p95_delta, p95_cost_appeared),
    ):
        if appeared:
            _bump(
                Verdict.FAIL,
                f"{label} cost appeared (baseline 0 → current > 0)",
            )
            continue
        if delta is None:
            continue
        if delta >= thresholds.cost_fail:
            _bump(
                Verdict.FAIL,
                f"{label} cost regressed by {delta * 100:.1f}% "
                f"(fail threshold +{thresholds.cost_fail * 100:.1f}%)",
            )
        elif delta >= thresholds.cost_warn:
            _bump(
                Verdict.WARN,
                f"{label} cost regressed by {delta * 100:.1f}% "
                f"(warn threshold +{thresholds.cost_warn * 100:.1f}%)",
            )

    # Cache-hit rate drops. A *positive* drop = the rate fell.
    if cache_delta is not None:
        drop = -cache_delta
        if drop >= thresholds.cache_fail:
            _bump(
                Verdict.FAIL,
                f"cache hit rate dropped by {drop * 100:.1f}pp "
                f"(fail threshold -{thresholds.cache_fail * 100:.1f}pp)",
            )
        elif drop >= thresholds.cache_warn:
            _bump(
                Verdict.WARN,
                f"cache hit rate dropped by {drop * 100:.1f}pp "
                f"(warn threshold -{thresholds.cache_warn * 100:.1f}pp)",
            )

    # Outcome (success) rate drops. Use `>=` to match the cost / cache
    # boundary convention (Finding #8); explicitly require `drop > 0`
    # so a 0pp drop with outcome_warn=0.0 stays OK.
    if success_delta is not None:
        drop = -success_delta
        if drop >= thresholds.outcome_fail:
            _bump(
                Verdict.FAIL,
                f"success rate dropped by {drop * 100:.1f}pp "
                f"(fail threshold -{thresholds.outcome_fail * 100:.1f}pp)",
            )
        elif drop > 0 and drop >= thresholds.outcome_warn:
            _bump(
                Verdict.WARN,
                f"success rate dropped by {drop * 100:.1f}pp",
            )

    # New critical smells -> automatic fail.
    crit = set(thresholds.critical_smells)
    for sd in smell_deltas:
        if sd.status == "appeared" and sd.id in crit:
            _bump(
                Verdict.FAIL,
                f"critical smell {sd.id!r} appeared "
                f"({sd.baseline_count} -> {sd.current_count})",
            )
        elif sd.status == "appeared":
            _bump(
                Verdict.WARN,
                f"smell {sd.id!r} appeared "
                f"({sd.baseline_count} -> {sd.current_count})",
            )

    if not reasons:
        reasons.append("within thresholds")
    return verdict, tuple(reasons)
