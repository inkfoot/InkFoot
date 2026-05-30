"""Storage hot-path benchmark.

Asserts the §9.1 perf budget: ``insert_event`` p95 < 1 ms. CI fails
when the budget is missed. The benchmark runs against a tempfile DB
(not ``:memory:``) so WAL pragmas are actually exercised.

The benchmark is parameterised at 10 000 events because that's the
floor for stable p95 statistics on shared CI runners. Run locally
with::

    pytest tests/benchmarks/ --benchmark-only

To regenerate the JSON artefact CI uploads::

    pytest tests/benchmarks/ --benchmark-only \
        --benchmark-json=benchmark.json
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkfoot.storage.sqlite import SQLiteStorage


_EVENTS_TO_INSERT = 10_000
_P95_BUDGET_S = 0.001  # 1 ms — §9.1 spec budget for the SQLite event insert
# Median guard. Tight on a dev box (<25 µs) and on a quiet CI
# runner (~30 µs). Set well below the 1 ms p95 budget so a real
# regression in the SQL/transaction path lights up immediately,
# but well *above* typical noise so a busy shared runner doesn't
# go red without a code change.
_MEDIAN_BUDGET_S = 0.0005  # 500 µs


@pytest.fixture()
def warm_storage(tmp_path: Path) -> SQLiteStorage:
    s = SQLiteStorage(path=tmp_path / "perf.db")
    s.connect()
    s.start_run(
        run_id="run-perf",
        task="perf",
        agent_kind="bench",
        started_at=1_700_000_000_000,
    )
    yield s
    s.close()


def test_insert_event_p95_under_one_ms(
    benchmark, warm_storage: SQLiteStorage
) -> None:
    counter = {"i": 0}

    def one_insert() -> None:
        counter["i"] += 1
        warm_storage.insert_event(
            event_id=f"e-{counter['i']}",
            run_id="run-perf",
            kind="llm_call",
            occurred_at=1_700_000_000_000 + counter["i"],
            sequence=counter["i"],
            payload_json='{"input_tokens": 10, "output_tokens": 5}',
        )

    # pytest-benchmark picks rounds/iterations adaptively. We give it a
    # sensible starting point so the run completes in seconds and the
    # statistic is stable.
    benchmark.pedantic(one_insert, rounds=_EVENTS_TO_INSERT, iterations=1)

    stats = benchmark.stats.stats

    # We deliberately do NOT assert on ``stats.mean``: shared CI
    # runners produce occasional 100-700 ms outliers (VM scheduler
    # blips, neighbour tenants) which drag the arithmetic mean
    # orders of magnitude above the actual hot path. The §9.1 spec
    # budgets p95, not mean — and the median is naturally robust to
    # outliers. We assert both.
    assert stats.median < _MEDIAN_BUDGET_S, (
        f"median insert_event {stats.median * 1000:.3f} ms exceeded "
        f"{_MEDIAN_BUDGET_S * 1000:.3f} ms budget"
    )

    # pytest-benchmark exposes 'iqr' and median but not p95 directly;
    # we approximate p95 from the sorted sample.
    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _P95_BUDGET_S, (
        f"p95 insert_event {p95 * 1000:.3f} ms exceeded "
        f"{_P95_BUDGET_S * 1000:.3f} ms budget"
    )
