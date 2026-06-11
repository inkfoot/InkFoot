"""Report renderer hot-path benchmark.

Budget: ``inkfoot report --run <id>`` end-to-end completes in
under 200 ms for a 50-event run. The renderer is a pure function of
``(run, ledger_totals, smells)`` so we exercise the slice CI sees:
storage → per-category roll-up → smell engine → render → ``join``.

Asserts on **median** and **p95** rather than mean — shared CI
runners produce outlier ms spikes that drag arithmetic means above
the budget without representing a real regression.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from inkfoot.cli.report import (
    _aggregate_ledger_totals,
    render,
)
from inkfoot.smells import DEFAULT_SMELLS
from inkfoot.smells.engine import SmellEngine
from inkfoot.storage.sqlite import SQLiteStorage
from ulid import ULID


_REPORT_MEDIAN_BUDGET_S = 0.100  # 100 ms median — half the p95 cap
_REPORT_P95_BUDGET_S = 0.200  # 200 ms p95 — the perf cap


def _seed_50_event_run(tmp_path: Path) -> tuple[SQLiteStorage, str]:
    """Build a realistic 50-event run with a populated ledger so the
    renderer has something representative to draw. Returns
    ``(storage, run_id)``."""
    db = tmp_path / "perf.db"
    storage = SQLiteStorage(path=db)
    storage.connect()
    run_id = "run-report-perf"
    storage.start_run(
        run_id=run_id,
        task="perf-bench",
        agent_kind="bench",
        started_at=1_700_000_000_000,
    )

    # 50 llm_call events with non-trivial payloads.
    for i in range(50):
        payload = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "sequence": i + 1,
            "started_at": 1_700_000_000_000 + i,
            "ended_at": 1_700_000_000_000 + i + 1,
            "ledger": {
                "system_static_tokens": 100,
                "system_dynamic_tokens": 20,
                "user_input_tokens": 30,
                "tool_schema_tokens": 15,
                "tool_result_tokens": 50,
                "retrieved_context_tokens": 0,
                "memory_tokens": 40,
                "retry_overhead_tokens": 0,
                "summariser_tokens": 0,
                "reasoning_tokens": 0,
                "guardrail_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 80 if i > 0 else 0,
                "output_tokens": 25,
            },
            "estimated_nanodollars": 750,
            "tools_offered": ["search"],
            "tools_called": ["search"] if i % 3 == 0 else [],
            "cache_status": "hit" if i > 0 else "n/a",
        }
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=1_700_000_000_000 + i + 1,
            sequence=i + 1,
            payload_json=json.dumps(payload),
        )
    storage.end_run(
        run_id=run_id, ended_at=1_700_000_000_100, status="complete"
    )
    return storage, run_id


@pytest.fixture()
def primed(tmp_path: Path):
    storage, run_id = _seed_50_event_run(tmp_path)
    yield storage, run_id
    storage.close()


def test_report_renders_under_two_hundred_ms_for_fifty_event_run(
    benchmark, primed
) -> None:
    """End-to-end report render against a 50-event run sits under
    the perf budget."""
    storage, run_id = primed
    run_row = storage.get_run(run_id)
    events = list(storage.iter_events(run_id))

    engine = SmellEngine(list(DEFAULT_SMELLS))

    def one_render() -> None:
        # Mirror what the CLI does: roll up totals, evaluate smells,
        # render. Hot path on each invocation re-reads events to
        # match the production load.
        ledger_totals = _aggregate_ledger_totals(events)
        smells = engine.evaluate(run_row, events)
        render(
            run=run_row, ledger_totals=ledger_totals, smells=smells
        )

    # Warm-up — first call is JIT-y and module-import-heavy.
    one_render()
    benchmark.pedantic(one_render, rounds=30, iterations=1)

    median = benchmark.stats.stats.median
    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert median < _REPORT_MEDIAN_BUDGET_S, (
        f"report render median {median * 1000:.1f} ms exceeded "
        f"{_REPORT_MEDIAN_BUDGET_S * 1000:.0f} ms"
    )
    assert p95 < _REPORT_P95_BUDGET_S, (
        f"report render p95 {p95 * 1000:.1f} ms exceeded "
        f"{_REPORT_P95_BUDGET_S * 1000:.0f} ms"
    )
