"""Postgres storage hot-path benchmark.

Asserts the network-backend perf budget: ``insert_event`` p95 < 5 ms
against a real Postgres server. The event write is the only storage
operation on an agent's request path once a fleet moves off SQLite,
so it carries its own budget — wider than the SQLite one because each
write is a network round-trip plus the ``aggregates_dirty`` flag
update in the same transaction.

Opt-in via ``INKFOOT_TEST_PG_DSN`` (the same variable the integration
suite uses); without it the benchmark skips. Run locally with::

    INKFOOT_TEST_PG_DSN=postgresql://... \
        pytest tests/benchmarks/ --benchmark-only -m postgres
"""

from __future__ import annotations

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

_ROUNDS = 1_000
# Budget asserted on p95; median asserted at the same constant as an
# outlier-robust backstop (a single autovacuum or checkpoint blip
# shouldn't be able to fail the gate on its own, but a regressed
# write path must).
_P95_BUDGET_S = 0.005  # 5 ms
_MEDIAN_BUDGET_S = 0.005


@pytest.fixture()
def warm_pg_storage(pg_storage):
    pg_storage.start_run(
        run_id="run-perf",
        task="perf",
        agent_kind="bench",
        started_at=1_700_000_000_000,
    )
    return pg_storage


def test_insert_event_p95_under_five_ms(benchmark, warm_pg_storage) -> None:
    counter = {"i": 0}

    def one_insert() -> None:
        counter["i"] += 1
        warm_pg_storage.insert_event(
            event_id=f"e-{counter['i']}",
            run_id="run-perf",
            kind="llm_call",
            occurred_at=1_700_000_000_000 + counter["i"],
            sequence=counter["i"],
            payload_json='{"input_tokens": 10, "output_tokens": 5}',
        )

    benchmark.pedantic(one_insert, rounds=_ROUNDS, iterations=1)

    stats = benchmark.stats.stats
    assert stats.median < _MEDIAN_BUDGET_S, (
        f"median insert_event {stats.median * 1000:.3f} ms exceeded "
        f"{_MEDIAN_BUDGET_S * 1000:.3f} ms budget"
    )
    sample = sorted(stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _P95_BUDGET_S, (
        f"p95 insert_event {p95 * 1000:.3f} ms exceeded "
        f"{_P95_BUDGET_S * 1000:.3f} ms budget"
    )
