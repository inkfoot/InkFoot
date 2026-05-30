"""Unit tests for ``inkfoot.benchmark.schema``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    BenchmarkSchemaError,
    ScenarioResult,
    SmellCount,
    percentile,
)


def _artifact_dict(**overrides):
    base = {
        "inkfoot_version": "1.0.0",
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "captured_at": "2026-05-25T12:00:00Z",
        "scenarios": [
            {
                "task": "customer-support-triage",
                "runs": 8,
                "successes": 8,
                "p50_nanodollars": 41_000_000,
                "p95_nanodollars": 83_000_000,
                "mean_llm_calls": 4.1,
                "mean_cache_hit_rate": 0.82,
                "smells_seen": [
                    {"id": "unstable-prompt-prefix", "count": 0},
                    {"id": "oversized-tool-result-recycled", "count": 1},
                ],
            }
        ],
    }
    base.update(overrides)
    return base


def test_artifact_roundtrips_through_dict():
    raw = _artifact_dict()
    art = BenchmarkArtifact.from_dict(raw)
    assert art.scenarios[0].task == "customer-support-triage"
    assert art.scenarios[0].smells_seen[1].count == 1
    assert art.to_dict() == raw


def test_artifact_persists_to_disk_and_reloads(tmp_path: Path):
    raw = _artifact_dict()
    art = BenchmarkArtifact.from_dict(raw)
    out = tmp_path / "bench" / "out.json"
    art.write(out)
    assert out.exists()
    loaded = BenchmarkArtifact.load(out)
    assert loaded == art
    # The file must end with a trailing newline (friendlier to diffs).
    assert out.read_text(encoding="utf-8").endswith("\n")


def test_artifact_load_raises_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        BenchmarkArtifact.load(tmp_path / "does-not-exist.json")


def test_artifact_load_rejects_invalid_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all {")
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.load(bad)


def test_artifact_rejects_unknown_schema_version():
    raw = _artifact_dict(schema_version="999")
    with pytest.raises(BenchmarkSchemaError) as exc:
        BenchmarkArtifact.from_dict(raw)
    assert "schema_version" in str(exc.value)


def test_scenario_result_rejects_negative_runs():
    raw = _artifact_dict()
    raw["scenarios"][0]["runs"] = -1
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.from_dict(raw)


def test_scenario_result_rejects_successes_exceeding_runs():
    raw = _artifact_dict()
    raw["scenarios"][0]["successes"] = 100
    raw["scenarios"][0]["runs"] = 4
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.from_dict(raw)


def test_scenario_result_rejects_cache_rate_outside_unit_interval():
    raw = _artifact_dict()
    raw["scenarios"][0]["mean_cache_hit_rate"] = 1.5
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.from_dict(raw)


def test_scenario_result_rejects_bool_smuggled_as_int():
    raw = _artifact_dict()
    raw["scenarios"][0]["runs"] = True
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.from_dict(raw)


def test_artifact_rejects_duplicate_task_names():
    raw = _artifact_dict()
    raw["scenarios"].append(dict(raw["scenarios"][0]))
    with pytest.raises(BenchmarkSchemaError):
        BenchmarkArtifact.from_dict(raw)


def test_smell_count_rejects_negative_count():
    with pytest.raises(BenchmarkSchemaError):
        SmellCount.from_dict({"id": "x", "count": -1})


def test_smell_count_rejects_empty_id():
    with pytest.raises(BenchmarkSchemaError):
        SmellCount.from_dict({"id": "", "count": 0})


def test_percentile_handles_empty_input():
    assert percentile([], 50) == 0.0


def test_percentile_single_element_returns_that_element():
    assert percentile([42], 50) == 42.0
    assert percentile([42], 95) == 42.0


def test_percentile_two_elements_interpolates_p50():
    # numpy's linear default would give 1.5 for p50 of [1, 2].
    assert percentile([1, 2], 50) == 1.5


def test_percentile_clamps_pct_to_unit_range():
    assert percentile([1, 2, 3, 4, 5], 110) == 5.0
    assert percentile([1, 2, 3, 4, 5], -5) == 1.0


def test_artifact_to_json_is_parseable_by_json_module():
    raw = _artifact_dict()
    art = BenchmarkArtifact.from_dict(raw)
    parsed = json.loads(art.to_json())
    assert parsed == raw
