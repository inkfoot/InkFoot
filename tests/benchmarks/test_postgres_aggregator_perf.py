"""Postgres aggregator sweep benchmark.

Asserts the worker perf budget: one ``sweep_once`` over 50 dirty runs
completes in < 200 ms against a real Postgres server. The sweep
cadence defaults to 500 ms, so a sweep that can't clear a 50-run
backlog well inside one interval would fall behind a busy fleet and
leave ``runs.total_*`` permanently stale.

Each round re-seeds 50 dirty runs (two events apiece) in the untimed
pedantic ``setup`` hook — a sweep cleans the dirty flags, so reusing
state would measure an empty sweep from round two onwards.

Opt-in via ``INKFOOT_TEST_PG_DSN``; without it the benchmark skips.
Run locally with::

    INKFOOT_TEST_PG_DSN=postgresql://... \
        pytest tests/benchmarks/ --benchmark-only -m postgres
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("INKFOOT_TEST_PG_DSN"),
        reason="INKFOOT_TEST_PG_DSN not set",
    ),
]

psycopg = pytest.importorskip("psycopg")

from inkfoot.storage.postgres_aggregator import PostgresAggregator  # noqa: E402

_DIRTY_RUNS_PER_SWEEP = 50
_EVENTS_PER_RUN = 2
_ROUNDS = 10
# Budget asserted on p95; median asserted at the same constant as an
# outlier-robust backstop. With ten rounds the p95 sample is the
# slowest sweep, so the gate is effectively "every sweep under
# budget".
_P95_BUDGET_S = 0.200  # 200 ms
_MEDIAN_BUDGET_S = 0.200

_PAYLOAD = json.dumps({"input_tokens": 42, "output_tokens": 7})


@pytest.fixture()
def aggregator(pg_storage):
    instance = PostgresAggregator(
        pg_storage, interval_seconds=0.05, lock_poll_seconds=0.05
    )
    instance.connect()
    try:
        yield instance
    finally:
        instance.close()


def test_sweep_of_fifty_dirty_runs_under_two_hundred_ms(
    benchmark, pg_storage, aggregator
) -> None:
    round_counter = {"i": 0}
    swept: list = []

    def seed_dirty_runs():
        round_counter["i"] += 1
        r = round_counter["i"]
        for n in range(_DIRTY_RUNS_PER_SWEEP):
            run_id = f"run-{r}-{n}"
            pg_storage.start_run(
                run_id=run_id,
                task="perf",
                agent_kind="bench",
                started_at=1_700_000_000_000,
            )
            for e in range(_EVENTS_PER_RUN):
                pg_storage.insert_event(
                    event_id=f"{run_id}-e{e}",
                    run_id=run_id,
                    kind="llm_call",
                    occurred_at=1_700_000_000_000 + e,
                    sequence=e + 1,
                    payload_json=_PAYLOAD,
                )
        return (), {}

    def one_sweep():
        swept.append(aggregator.sweep_once(timeout=5.0))

    benchmark.pedantic(
        one_sweep, setup=seed_dirty_runs, rounds=_ROUNDS, iterations=1
    )

    # Sanity: every measured sweep actually projected the full batch —
    # a sweep that timed out on the advisory lock (None) or found
    # nothing dirty would make the timing meaningless.
    assert swept == [_DIRTY_RUNS_PER_SWEEP] * _ROUNDS

    stats = benchmark.stats.stats
    assert stats.median < _MEDIAN_BUDGET_S, (
        f"median sweep {stats.median * 1000:.1f} ms exceeded "
        f"{_MEDIAN_BUDGET_S * 1000:.1f} ms budget"
    )
    sample = sorted(stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _P95_BUDGET_S, (
        f"p95 sweep {p95 * 1000:.1f} ms exceeded "
        f"{_P95_BUDGET_S * 1000:.1f} ms budget"
    )
