"""Contract enforcer hot-path benchmark.

Asserts the enforcement perf budget: ``ContractEnforcer.before_call``
p95 < 50 µs. The pre-call decision sits on every governed LLM call —
in front of the network round-trip — so it must stay micro-scale even
though it runs the full real path: budget resolution, the pessimistic
cost estimate, and the degrade-ladder scan.

The measured state is the common steady state: a run registered under
a contract with a three-rung ladder, spend below the first rung, and
a warm per-task output average feeding the estimator. Run locally
with::

    pytest tests/benchmarks/ --benchmark-only
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from inkfoot.contracts.enforcer import ContractEnforcer
from inkfoot.contracts.loader import load_contract

_ROUNDS = 10_000
# The budget is asserted on p95; the median is asserted at the same
# constant purely as an outlier-robust backstop (shared CI runners
# produce occasional multi-ms scheduler blips that p95 alone would
# have to absorb).
_P95_BUDGET_S = 0.000_050  # 50 µs
_MEDIAN_BUDGET_S = 0.000_050

_CONTRACT_YAML = """
schema_version: 1
task: perf-triage
cheap_model: claude-haiku-4-5
budget:
  max_nanodollars: 50_000_000
  max_llm_calls: 1000
degrade:
  - at_percent: 80
    action: warn
  - at_percent: 90
    action: switch_to_cheap_model
  - at_percent: 100
    action: block
"""

_REQUEST_KWARGS = {
    "model": "claude-haiku-4-5",
    "max_tokens": 1024,
    "messages": [
        {
            "role": "user",
            "content": (
                "Customer reports that the export job started failing "
                "after the last deploy. Classify the ticket, decide "
                "whether it needs engineering escalation, and draft a "
                "first response that asks for the failing job id."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "This looks like a regression in the export pipeline. "
                "I'll check the recent deploy notes before answering."
            ),
        },
        {"role": "user", "content": "Any update on the export failures?"},
    ],
}


@pytest.fixture()
def warm_enforcer(tmp_path: Path) -> ContractEnforcer:
    contract_path = tmp_path / "perf-triage.yaml"
    contract_path.write_text(
        textwrap.dedent(_CONTRACT_YAML), encoding="utf-8"
    )
    contract = load_contract(contract_path)
    enforcer = ContractEnforcer(contracts={"perf-triage": contract})
    enforcer.register_run("run-perf", "perf-triage")
    # Warm the per-task output moving average so the estimator takes
    # its real branch instead of the cold-start default.
    enforcer.record_call(
        run_id="run-perf",
        nanodollars=1_000_000,
        output_tokens=300,
        task="perf-triage",
    )
    return enforcer


def test_before_call_p95_under_fifty_microseconds(
    benchmark, warm_enforcer: ContractEnforcer
) -> None:
    def one_decision():
        return warm_enforcer.before_call(
            run_id="run-perf",
            task="perf-triage",
            provider="anthropic",
            model="claude-haiku-4-5",
            request_kwargs=_REQUEST_KWARGS,
        )

    # Sanity: the steady state under measurement is the allow path —
    # spend is far below the first ladder rung.
    assert one_decision().action == "allow"

    benchmark.pedantic(one_decision, rounds=_ROUNDS, iterations=1)

    stats = benchmark.stats.stats
    assert stats.median < _MEDIAN_BUDGET_S, (
        f"median before_call {stats.median * 1e6:.1f} µs exceeded "
        f"{_MEDIAN_BUDGET_S * 1e6:.1f} µs budget"
    )
    sample = sorted(stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _P95_BUDGET_S, (
        f"p95 before_call {p95 * 1e6:.1f} µs exceeded "
        f"{_P95_BUDGET_S * 1e6:.1f} µs budget"
    )
