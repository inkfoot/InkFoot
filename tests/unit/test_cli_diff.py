"""End-to-end CLI tests for ``inkfoot benchmark`` + ``inkfoot diff``.

These tests drive ``inkfoot.cli.main.main`` directly with argv so the
parser + dispatcher are exercised the same way a shell invocation
would exercise them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.benchmark.schema import BENCHMARK_SCHEMA_VERSION, BenchmarkArtifact
from inkfoot.cli.main import main


def _baseline_dict(p50: int = 1_000, p95: int = 1_000) -> dict:
    return {
        "inkfoot_version": "1.0.0",
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "captured_at": "2026-05-25T12:00:00Z",
        "scenarios": [
            {
                "task": "demo",
                "runs": 4,
                "successes": 4,
                "p50_nanodollars": p50,
                "p95_nanodollars": p95,
                "mean_llm_calls": 1.0,
                "mean_cache_hit_rate": 0.5,
                "smells_seen": [],
            }
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_diff_exit_code_zero_when_unchanged(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _baseline_dict())
    _write_json(cur, _baseline_dict())
    exit_code = main(["diff", str(base), str(cur), "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["verdict"] == "ok"


def test_diff_exit_code_two_on_hard_regression(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _baseline_dict(p50=1_000, p95=1_000))
    _write_json(cur, _baseline_dict(p50=1_510, p95=1_510))  # +51%
    exit_code = main(["diff", str(base), str(cur), "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out)["verdict"] == "fail"


def test_diff_writes_markdown_to_output_path(tmp_path):
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _baseline_dict())
    _write_json(cur, _baseline_dict())
    out = tmp_path / "report.md"
    exit_code = main(
        [
            "diff",
            str(base),
            str(cur),
            "--format",
            "markdown",
            "--output",
            str(out),
        ]
    )
    assert exit_code == 0
    assert out.exists()
    assert "Inkfoot cost diff" in out.read_text()


def test_diff_thresholds_tight_promotes_to_warn(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _baseline_dict(p50=1_000, p95=1_000))
    _write_json(cur, _baseline_dict(p50=1_080, p95=1_080))  # +8%
    exit_code = main(
        [
            "diff",
            str(base),
            str(cur),
            "--format",
            "json",
            "--thresholds",
            "tight",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["verdict"] == "warn"


def test_diff_exit_two_on_missing_file(tmp_path, capsys):
    base = tmp_path / "missing.json"
    cur = tmp_path / "also-missing.json"
    exit_code = main(["diff", str(base), str(cur)])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "not found" in captured.err.lower() or "no such file" in captured.err.lower()


def test_diff_json_output_passes_through_jq_style_consumer(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _baseline_dict())
    _write_json(cur, _baseline_dict(p50=1_300))  # +30% on p50 only
    exit_code = main(["diff", str(base), str(cur), "--format", "json"])
    assert exit_code == 1
    parsed = json.loads(capsys.readouterr().out)
    # Mimic a real jq consumer: extract scenario-level verdicts.
    verdicts = [sc["verdict"] for sc in parsed["scenarios"]]
    assert verdicts == ["warn"]
