"""Release-readiness checks for the governance feature set.

One test per headline capability the release notes promise: Token
Contracts drive both the runtime ladder and the CI verdict, the
contract-drafting CLI works on a realistic history, every framework
adapter is on the roster, unsupported policies fail loudly at
instrument time, cost-per-success is the headline report metric, the
Postgres backend ships with its operational assets, and the CI
workflow carries every required gate.

Each check is intentionally shallow — deep behaviour lives in the
dedicated suites — but together they pin the release bar in one
place: if a headline capability regresses or a required CI gate is
dropped from the workflow, this file fails even when the dedicated
suite that covered it was reorganised away.

External-adoption signals (reference integrations, user reports)
are judged by humans at release review; they have no automatable
assertion here.
"""

from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot.benchmark.runner import run_benchmark
from inkfoot.benchmark.schema import BENCHMARK_SCHEMA_VERSION
from inkfoot.cli.main import main
from inkfoot.contracts.check import check_contracts
from inkfoot.contracts.enforcer import ContractEnforcer
from inkfoot.contracts.loader import load_contract, load_contracts
from inkfoot.errors import PolicyNotSupported
from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.reports.cost_per_success import BucketRow, render_aggregate_table
from inkfoot.storage.sqlite import SQLiteStorage
from tests.contract.test_framework_adapter_contract import SPECS
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_CI_GATE_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "ci_gate"


@pytest.fixture(autouse=True)
def clean_instrumentation_state():
    from inkfoot._run_context import _clear_current_run

    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()
    yield
    _clear_current_run()
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()


# ----------------------------------------------------------------------
# Token Contracts: YAML → runtime enforcement → CI verdict
# ----------------------------------------------------------------------

_BLOCKING_CONTRACT = """
schema_version: 1
task: triage
budget:
  max_nanodollars: 1000
  max_llm_calls: 50
degrade:
  - at_percent: 100
    action: block
"""


def test_contract_yaml_drives_a_runtime_block(tmp_path: Path) -> None:
    contract_path = tmp_path / "triage.yaml"
    contract_path.write_text(
        textwrap.dedent(_BLOCKING_CONTRACT), encoding="utf-8"
    )
    contract = load_contract(contract_path)

    enforcer = ContractEnforcer(contracts={"triage": contract})
    enforcer.register_run("r1", "triage")
    # Spend past the whole budget; the next call must be refused.
    enforcer.record_call(
        run_id="r1", nanodollars=1_500, output_tokens=10, task="triage"
    )

    outcome = enforcer.before_call(
        run_id="r1",
        task="triage",
        provider="anthropic",
        model="claude-haiku-4-5",
        request_kwargs={"messages": [{"role": "user", "content": "next step"}]},
    )
    assert outcome.action == "block"
    assert outcome.violations and outcome.violations[0].action == "block"


def _artifact(tmp_path: Path, name: str, *, p95: int) -> Path:
    payload = {
        "inkfoot_version": "1.0.0",
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "captured_at": "2026-06-01T00:00:00Z",
        "scenarios": [
            {
                "task": "triage",
                "runs": 4,
                "successes": 4,
                "p50_nanodollars": p95 // 2,
                "p95_nanodollars": p95,
                "mean_llm_calls": 1.0,
                "mean_cache_hit_rate": 0.8,
                "smells_seen": [],
            }
        ],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_contract_check_cli_gates_on_budget(tmp_path: Path, capsys) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "triage.yaml").write_text(
        textwrap.dedent(
            """
            schema_version: 1
            task: triage
            budget:
              max_nanodollars: 10000
            """
        ),
        encoding="utf-8",
    )

    within = _artifact(tmp_path, "within.json", p95=1_000)
    blown = _artifact(tmp_path, "blown.json", p95=20_000)

    assert main(
        ["contract", "check", str(contracts), "--against", str(within)]
    ) == 0
    capsys.readouterr()
    assert main(
        ["contract", "check", str(contracts), "--against", str(blown)]
    ) == 2
    capsys.readouterr()


def test_ci_gate_fixtures_stay_valid() -> None:
    # The CI job runs `inkfoot benchmark` over these scenarios and
    # `inkfoot contract check` over these contracts; replaying the
    # same pipeline in-process pins the committed fixtures to a
    # passing verdict, so a drive-by edit can't silently turn the
    # required gate red (or, worse, vacuous).
    artefact = run_benchmark(_CI_GATE_FIXTURES / "scenarios")
    by_task = {s.task: s for s in artefact.scenarios}
    smoke = by_task["pipeline-smoke"]
    assert smoke.runs == 1
    assert smoke.successes == 1
    assert smoke.mean_llm_calls == 2.0
    assert smoke.p95_nanodollars == 3_000_000

    contracts = load_contracts([str(_CI_GATE_FIXTURES / "contracts")])
    report = check_contracts(contracts, artefact)
    assert report.exit_code == 0, [r.to_dict() for r in report.results]


def test_contract_draft_round_trips_on_a_seeded_history(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "runs.db"
    storage = SQLiteStorage(path=db_path)
    storage.connect()
    conn = storage._conn()
    now_ms = int(time.time() * 1000)
    for i in range(120):
        run_id = f"r{i:03d}"
        conn.execute(
            """
            INSERT INTO runs (
                id, task, started_at, status, outcome,
                total_input_tokens, total_cache_read_tokens,
                total_cache_creation_tokens, total_nanodollars
            ) VALUES (?, 'triage', ?, 'complete', ?, 1000, 700, 0, ?)
            """,
            [
                run_id,
                now_ms - i * 60_000,
                "failure" if i % 10 == 9 else "success",
                1_000_000 + i * 10_000,
            ],
        )
        for seq in range(3):
            conn.execute(
                """
                INSERT INTO events (id, run_id, kind, occurred_at, sequence)
                VALUES (?, ?, 'llm_call', ?, ?)
                """,
                [f"{run_id}-e{seq}", run_id, now_ms - i * 60_000, seq],
            )
    conn.commit()
    storage.close()

    out_path = tmp_path / "triage.yaml"
    exit_code = main(
        [
            "contract",
            "draft",
            "--task",
            "triage",
            "--db",
            str(db_path),
            "--output",
            str(out_path),
        ]
    )
    capsys.readouterr()
    assert exit_code == 0

    drafted = load_contract(out_path)
    assert drafted.task == "triage"
    assert drafted.budget is not None
    assert drafted.budget.max_nanodollars and drafted.budget.max_nanodollars > 0
    assert drafted.budget.max_llm_calls and drafted.budget.max_llm_calls >= 1


# ----------------------------------------------------------------------
# Framework adapters + policy surface
# ----------------------------------------------------------------------


def test_framework_adapter_roster_is_complete() -> None:
    by_name = {spec.name: spec for spec in SPECS}
    assert set(by_name) == {
        "langgraph",
        "openai_agents",
        "anthropic_agent",
        "pydantic_ai",
        "crewai",
    }
    assert by_name["crewai"].observation_only is True
    for name in set(by_name) - {"crewai"}:
        assert by_name[name].observation_only is False, name


def test_unsupported_policy_raises_at_instrument_time(tmp_path: Path) -> None:
    class PatternCOnly(Policy):
        NAME = "PatternCOnly"
        SUPPORTED_PATTERNS = {IntegrationPattern.C}

        def before_call(self, ctx):
            return PolicyDecision()

        def after_call(self, ctx, response):
            return None

    install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    with pytest.raises(PolicyNotSupported, match="PatternCOnly"):
        inkfoot.instrument(storage=storage, policies=[PatternCOnly()])
    assert instrument_mod.is_instrumented() is False


# ----------------------------------------------------------------------
# Reporting: cost-per-success is the headline
# ----------------------------------------------------------------------


def test_cost_per_success_is_the_headline_report_metric() -> None:
    row = BucketRow(
        bucket="triage",
        n_runs=4,
        total_nanodollars=8_000_000,
        avg_nanodollars=2_000_000,
        p95_nanodollars=3_000_000,
        n_success=4,
        n_accepted_answer=2,
    )
    rendered = render_aggregate_table(
        [row], window_label="30d", group_label="task"
    )
    header = next(
        line for line in rendered.splitlines() if "cost/success" in line
    )
    assert (
        header.index("runs")
        < header.index("cost/success")
        < header.index("avg_$")
        < header.index("p95_$")
    )
    assert (_REPO_ROOT / "docs" / "concepts" / "cost-per-success.md").exists()


# ----------------------------------------------------------------------
# Operational assets + required CI gates
# ----------------------------------------------------------------------


def test_postgres_release_assets_are_present() -> None:
    runbook = _REPO_ROOT / "docs" / "operations" / "postgres-migration.md"
    assert runbook.exists()

    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "postgres:16" in workflow
    assert "-m postgres" in workflow

    # The corpus-scale migration test must stay in the tree; it runs
    # in the postgres-backed CI job.
    migration_suite = (
        _REPO_ROOT / "tests" / "integration" / "test_migrate_to_postgres.py"
    ).read_text(encoding="utf-8")
    assert (
        "test_hundred_thousand_events_migrate_under_a_minute"
        in migration_suite
    )


def test_ci_workflow_carries_every_required_gate() -> None:
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    # Contract gate: benchmark artefact production + contract check.
    assert "inkfoot benchmark tests/fixtures/ci_gate/scenarios" in workflow
    assert "inkfoot contract check tests/fixtures/ci_gate/contracts" in workflow
    # Aggregate coverage floor — and this suite itself must be inside
    # the gated run, or every pin in this file is a dead letter.
    assert "--cov-fail-under=80" in workflow
    assert "pytest tests/unit tests/contract tests/integration" in workflow
    # Per-package floors on the governance and integration surfaces.
    for package in ("contracts", "policy", "providers", "adapters"):
        assert f'--include="inkfoot/{package}/*" --fail-under=80' in workflow
    # Perf budgets.
    assert "--benchmark-only" in workflow
    # Attribution accuracy harness.
    assert "validate_attribution.py" in workflow
