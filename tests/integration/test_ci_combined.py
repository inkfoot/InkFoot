"""``inkfoot diff --contracts`` composes the cost diff and the contract
check into a single PR comment / JSON document.
"""

from __future__ import annotations

import json
from pathlib import Path

from inkfoot.benchmark.schema import BENCHMARK_SCHEMA_VERSION
from inkfoot.cli.main import main


def _artifact_dict(p95: int, cache: float = 0.8, calls: float = 1.0) -> dict:
    return {
        "inkfoot_version": "1.0.0",
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "captured_at": "2026-05-25T12:00:00Z",
        "scenarios": [
            {
                "task": "demo",
                "runs": 4,
                "successes": 4,
                "p50_nanodollars": p95 // 2,
                "p95_nanodollars": p95,
                "mean_llm_calls": calls,
                "mean_cache_hit_rate": cache,
                "smells_seen": [],
            }
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


_CONTRACT = """
schema_version: 1
task: demo
budget:
  max_nanodollars: {ceiling}
  cache_hit_rate_min: 0.70
outcome:
  required_success_rate: 0.95
"""


def _setup(tmp_path: Path, *, p95: int, ceiling: int) -> tuple[Path, Path, Path]:
    base = tmp_path / "baseline.json"
    cur = tmp_path / "current.json"
    _write_json(base, _artifact_dict(p95=p95))
    _write_json(cur, _artifact_dict(p95=p95))
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "demo.yaml").write_text(
        _CONTRACT.format(ceiling=ceiling), encoding="utf-8"
    )
    return base, cur, contracts


def test_combined_markdown_has_both_sections(tmp_path, capsys):
    base, cur, contracts = _setup(tmp_path, p95=1_000, ceiling=10_000)
    exit_code = main(
        ["diff", str(base), str(cur), "--contracts", str(contracts)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    # Diff section + contract section both present in one comment.
    assert "Token Contract check" in out
    assert "demo" in out
    # Outcome clause shows as advisory.
    assert "advisory" in out


def test_combined_exit_code_reflects_contract_violation(tmp_path, capsys):
    # Cost diff is clean (identical artefacts) but the contract ceiling
    # is below observed p95 → combined exit must be the contract's 2.
    base, cur, contracts = _setup(tmp_path, p95=20_000, ceiling=10_000)
    exit_code = main(
        ["diff", str(base), str(cur), "--contracts", str(contracts)]
    )
    capsys.readouterr()
    assert exit_code == 2


def test_combined_json_nests_both_reports(tmp_path, capsys):
    base, cur, contracts = _setup(tmp_path, p95=1_000, ceiling=10_000)
    main(
        [
            "diff",
            str(base),
            str(cur),
            "--contracts",
            str(contracts),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert "diff" in payload
    assert payload["contract_check"]["kind"] == "contract_check"
