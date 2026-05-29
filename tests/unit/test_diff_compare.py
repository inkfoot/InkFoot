"""Unit tests for ``inkfoot.diff.compare`` (Phase 1 / E2-S2)."""

from __future__ import annotations

from typing import Iterable

import pytest

from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    ScenarioResult,
    SmellCount,
)
from inkfoot.diff.compare import Verdict, compare_artifacts
from inkfoot.diff.thresholds import THRESHOLD_PRESETS, Thresholds


def _scenario(
    task: str = "demo",
    *,
    runs: int = 4,
    successes: int = 4,
    p50: int = 1_000_000,
    p95: int = 2_000_000,
    mean_calls: float = 2.0,
    cache_rate: float = 0.5,
    smells: Iterable[SmellCount] = (),
) -> ScenarioResult:
    return ScenarioResult(
        task=task,
        runs=runs,
        successes=successes,
        p50_nanodollars=p50,
        p95_nanodollars=p95,
        mean_llm_calls=mean_calls,
        mean_cache_hit_rate=cache_rate,
        smells_seen=tuple(smells),
    )


def _artifact(*scenarios: ScenarioResult, version: str = "1.0.0") -> BenchmarkArtifact:
    return BenchmarkArtifact(
        inkfoot_version=version,
        schema_version=BENCHMARK_SCHEMA_VERSION,
        captured_at="2026-05-25T12:00:00Z",
        scenarios=tuple(scenarios),
    )


def test_unchanged_artifacts_verdict_is_ok():
    art = _artifact(_scenario())
    report = compare_artifacts(art, art)
    assert report.verdict is Verdict.OK
    assert report.exit_code == 0
    assert all(sd.verdict is Verdict.OK for sd in report.scenario_diffs)


def test_cost_regression_above_fail_threshold_promotes_to_fail():
    baseline = _artifact(_scenario(p50=1_000, p95=1_000))
    current = _artifact(_scenario(p50=1_510, p95=1_510))  # +51%
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL
    assert report.exit_code == 2


def test_cost_regression_above_warn_below_fail_is_warn():
    baseline = _artifact(_scenario(p50=1_000, p95=1_000))
    current = _artifact(_scenario(p50=1_300, p95=1_300))  # +30%
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.WARN
    assert report.exit_code == 1


def test_cost_drop_does_not_regress():
    baseline = _artifact(_scenario(p50=10_000, p95=10_000))
    current = _artifact(_scenario(p50=1_000, p95=1_000))
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.OK


def test_cache_hit_drop_above_fail_threshold_promotes_to_fail():
    baseline = _artifact(_scenario(cache_rate=0.90))
    current = _artifact(_scenario(cache_rate=0.60))  # -30pp
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL


def test_new_scenario_is_ok_with_no_baseline_reference():
    baseline = _artifact()
    current = _artifact(_scenario(task="brand-new"))
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.OK
    new_diff = report.scenario_diffs[0]
    assert new_diff.is_new
    assert new_diff.baseline is None


def test_removed_scenario_warns():
    baseline = _artifact(_scenario(task="legacy"))
    current = _artifact()
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.WARN
    assert report.scenario_diffs[0].is_removed


def test_critical_smell_appearance_fails_the_diff():
    baseline = _artifact(_scenario())
    current = _artifact(
        _scenario(
            smells=[SmellCount(id="runaway-retry-loop", count=2)],
        )
    )
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL
    reasons = report.scenario_diffs[0].reasons
    assert any("runaway-retry-loop" in r for r in reasons)


def test_non_critical_new_smell_warns_but_does_not_fail():
    baseline = _artifact(_scenario())
    current = _artifact(
        _scenario(
            smells=[SmellCount(id="unstable-prompt-prefix", count=1)],
        )
    )
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.WARN
    assert report.scenario_diffs[0].verdict is Verdict.WARN


def test_thresholds_preset_tight_catches_smaller_regressions():
    baseline = _artifact(_scenario(p50=1_000, p95=1_000))
    current = _artifact(_scenario(p50=1_080, p95=1_080))  # +8%
    default_report = compare_artifacts(baseline, current)
    tight_report = compare_artifacts(
        baseline, current, thresholds=THRESHOLD_PRESETS["tight"]
    )
    assert default_report.verdict is Verdict.OK
    assert tight_report.verdict is Verdict.WARN


def test_schema_version_mismatch_raises():
    baseline = BenchmarkArtifact(
        inkfoot_version="1.0.0",
        schema_version="2",
        captured_at="2026-05-25T12:00:00Z",
        scenarios=(),
    )
    current = _artifact()
    with pytest.raises(ValueError, match="schema version mismatch"):
        compare_artifacts(baseline, current)


def test_outcome_drop_above_fail_threshold_fails():
    baseline = _artifact(_scenario(runs=10, successes=10))
    current = _artifact(_scenario(runs=10, successes=8))  # -20pp
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL


def test_new_scenario_with_critical_smell_fails_not_oks():
    # Finding #5: a brand-new scenario that ships with a critical
    # smell already firing must NOT sail through as OK.
    baseline = _artifact()
    current = _artifact(
        _scenario(
            task="brand-new-but-broken",
            smells=[SmellCount(id="runaway-retry-loop", count=3)],
        )
    )
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL
    diff = report.scenario_diffs[0]
    assert diff.is_new
    assert any("runaway-retry-loop" in r for r in diff.reasons)


def test_new_scenario_with_non_critical_smell_warns():
    baseline = _artifact()
    current = _artifact(
        _scenario(
            task="brand-new",
            smells=[SmellCount(id="unstable-prompt-prefix", count=1)],
        )
    )
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.WARN


def test_zero_baseline_cost_to_positive_current_promotes_to_fail():
    # Finding #6: baseline p50/p95 == 0 (no LLM calls; smoke test) and
    # current > 0 must NOT render as missing data.
    baseline = _artifact(_scenario(p50=0, p95=0))
    current = _artifact(_scenario(p50=12_345, p95=12_345))
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.FAIL
    reasons = report.scenario_diffs[0].reasons
    assert any("cost appeared" in r for r in reasons)


def test_zero_to_zero_cost_stays_ok():
    baseline = _artifact(_scenario(p50=0, p95=0))
    current = _artifact(_scenario(p50=0, p95=0))
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.OK


def test_outcome_warn_threshold_boundary_is_inclusive():
    # Finding #8: with a custom outcome_warn=0.05, a drop of exactly
    # 5pp should warn (matching cost / cache `>=` convention).
    custom = Thresholds(
        name="custom",
        cost_warn=0.20,
        cost_fail=0.50,
        cache_warn=0.10,
        cache_fail=0.25,
        outcome_warn=0.05,
        outcome_fail=0.20,
    )
    baseline = _artifact(_scenario(runs=20, successes=20))
    current = _artifact(_scenario(runs=20, successes=19))  # -5pp exactly
    report = compare_artifacts(baseline, current, thresholds=custom)
    assert report.verdict is Verdict.WARN


def test_outcome_zero_drop_with_default_thresholds_stays_ok():
    baseline = _artifact(_scenario(runs=4, successes=3))
    current = _artifact(_scenario(runs=4, successes=3))
    report = compare_artifacts(baseline, current)
    assert report.verdict is Verdict.OK


def test_unchanged_smell_count_still_emits_unchanged_status():
    # Status="unchanged" is reachable when both counts are non-zero
    # and equal. Verify the SmellDelta is produced and rendered.
    smell = SmellCount(id="unstable-prompt-prefix", count=2)
    baseline = _artifact(_scenario(smells=[smell]))
    current = _artifact(_scenario(smells=[smell]))
    report = compare_artifacts(baseline, current)
    smell_deltas = report.scenario_diffs[0].smell_deltas
    assert len(smell_deltas) == 1
    assert smell_deltas[0].status == "unchanged"
    assert report.verdict is Verdict.OK


def test_diff_report_serialises_to_dict():
    baseline = _artifact(_scenario(p50=1_000, p95=1_000))
    current = _artifact(_scenario(p50=1_510, p95=1_510))
    report = compare_artifacts(baseline, current)
    raw = report.to_dict()
    assert raw["verdict"] == "fail"
    assert raw["scenarios"][0]["verdict"] == "fail"
    assert raw["thresholds"] == "default"
