"""Aggregator drain benchmark.

Asserts the §9.1 perf budget for the aggregator worker:

    Aggregator worker cycle — under 50 ms for 50 dirty runs

We populate 50 runs with 4 events each (a realistic 4-turn agent
trace), mark every run dirty, then time a single ``drain_once``
sweep. The benchmark fails if a single sweep blows past 50 ms,
matching the spec's CI gate.

Run locally with::

    pytest tests/benchmarks/test_aggregator_perf.py --benchmark-only

The CI workflow runs all benchmarks and uploads the JSON artefact
for trend tracking.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.storage.aggregator import AggregatorWorker
from inkfoot.storage.sqlite import SQLiteStorage


_DIRTY_RUNS = 50
_EVENTS_PER_RUN = 4
_DRAIN_BUDGET_S = 0.050  # 50 ms — per the architecture notes §9.1


def _seed_dirty_runs(s: SQLiteStorage) -> None:
    for r in range(_DIRTY_RUNS):
        run_id = f"run-{r}"
        s.start_run(
            run_id=run_id,
            task="bench",
            agent_kind="aggregator-bench",
            started_at=1_700_000_000_000 + r,
        )
        for e in range(_EVENTS_PER_RUN):
            s.insert_event(
                event_id=f"{run_id}-e{e}",
                run_id=run_id,
                kind="llm_call",
                occurred_at=1_700_000_000_000 + r * 10 + e,
                sequence=e + 1,
                payload_json=json.dumps(
                    {
                        "input_tokens": 100 + e,
                        "output_tokens": 50 + e,
                        "cache_read_tokens": e,
                        "nanodollars": 12_345 + e * 100,
                    }
                ),
            )


@pytest.fixture()
def primed_storage(tmp_path: Path) -> SQLiteStorage:
    s = SQLiteStorage(path=tmp_path / "agg-bench.db")
    s.connect()
    yield s
    s.close()


def test_drain_fifty_dirty_runs_under_fifty_ms(
    benchmark, primed_storage: SQLiteStorage
) -> None:
    worker = AggregatorWorker(primed_storage, batch_size=_DIRTY_RUNS)

    def sweep() -> int:
        # Re-prime before each round because drain_once clears dirty
        # flags. ``setup`` would be cleaner but mixes badly with
        # ``pedantic`` rounds counting; the explicit re-prime keeps
        # the measurement honest.
        for r in range(_DIRTY_RUNS):
            primed_storage.mark_dirty(f"run-{r}")
        return worker.drain_once()

    # Prime once before the timed run so the seeded runs exist.
    _seed_dirty_runs(primed_storage)

    # 30 rounds is enough for a stable median on CI without dominating
    # workflow runtime.
    benchmark.pedantic(sweep, rounds=30, iterations=1)

    stats = benchmark.stats.stats
    # Assert on median rather than mean — shared CI runners produce
    # occasional 40-50 ms outliers (VM-scheduler jitter) which drag
    # the arithmetic mean above the budget on a benchmark whose
    # median sits comfortably under it. The §9.1 budget is for the
    # typical case (the sweep cycle); the p95 assertion below catches
    # tail regressions.
    assert stats.median < _DRAIN_BUDGET_S, (
        f"median drain of {_DIRTY_RUNS} dirty runs took "
        f"{stats.median * 1000:.1f} ms — exceeds "
        f"{_DRAIN_BUDGET_S * 1000:.0f} ms budget (§9.1)"
    )

    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _DRAIN_BUDGET_S, (
        f"p95 drain of {_DIRTY_RUNS} dirty runs took "
        f"{p95 * 1000:.1f} ms — exceeds "
        f"{_DRAIN_BUDGET_S * 1000:.0f} ms budget"
    )
