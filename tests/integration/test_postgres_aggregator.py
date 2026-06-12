"""Advisory-lock aggregator tests against a real Postgres server.

Opt-in via ``INKFOOT_TEST_PG_DSN``. Covers the cross-process
contract: mutual exclusion between workers, sub-second takeover when
the active worker is SIGKILLed (session death releases the lock),
and the heartbeat-backed ``--health`` probe.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("INKFOOT_TEST_PG_DSN"),
        reason="INKFOOT_TEST_PG_DSN not set",
    ),
]

psycopg = pytest.importorskip("psycopg")

from inkfoot.cli import aggregator_worker  # noqa: E402
from inkfoot.storage.postgres_aggregator import (  # noqa: E402
    _AGGREGATOR_LOCK_KEY,
    PostgresAggregator,
)


def _seed_dirty_run(storage, run_id: str = "run-1") -> None:
    storage.start_run(
        run_id=run_id, task="demo", agent_kind=None, started_at=1000
    )
    storage.insert_event(
        event_id=f"{run_id}-evt",
        run_id=run_id,
        kind="llm_call",
        occurred_at=1001,
        sequence=1,
        payload_json=json.dumps({"input_tokens": 42, "output_tokens": 7}),
    )


@pytest.fixture
def aggregator(pg_storage):
    instance = PostgresAggregator(
        pg_storage, interval_seconds=0.05, lock_poll_seconds=0.05
    )
    instance.connect()
    try:
        yield instance
    finally:
        instance.close()


# ----------------------------------------------------------------------
# Sweeps
# ----------------------------------------------------------------------


def test_sweep_once_projects_and_heartbeats(pg_storage, aggregator) -> None:
    _seed_dirty_run(pg_storage)

    assert aggregator.sweep_once(timeout=5.0) == 1

    row = pg_storage.get_run("run-1")
    assert row["total_input_tokens"] == 42
    assert row["total_output_tokens"] == 7
    assert row["aggregates_dirty"] == 0
    heartbeat = pg_storage.read_heartbeat()
    assert heartbeat is not None
    assert heartbeat["runs_swept"] == 1


def test_lock_is_released_between_sweeps(pg_storage, aggregator) -> None:
    """Two consecutive sweeps on the same worker must both acquire —
    i.e. the per-sweep lock isn't leaked."""
    assert aggregator.sweep_once(timeout=5.0) == 0
    assert aggregator.sweep_once(timeout=5.0) == 0


# ----------------------------------------------------------------------
# Mutual exclusion + takeover
# ----------------------------------------------------------------------


def test_sweep_waits_while_another_session_holds_the_lock(
    pg_dsn, pg_storage, aggregator
) -> None:
    holder = psycopg.connect(pg_dsn, autocommit=True)
    try:
        holder.execute(
            "SELECT pg_advisory_lock(%s)", (_AGGREGATOR_LOCK_KEY,)
        )
        # Worker must give up at the timeout, not sweep.
        assert aggregator.sweep_once(timeout=0.3) is None
        assert pg_storage.read_heartbeat() is None
    finally:
        holder.close()
    # Lock released with the holder's session — now the sweep runs.
    assert aggregator.sweep_once(timeout=5.0) is not None


_HOLDER_SCRIPT = """
import sys
import time

import psycopg

conn = psycopg.connect(sys.argv[1], autocommit=True)
conn.execute("SELECT pg_advisory_lock(%s)", (int(sys.argv[2]),))
print("LOCKED", flush=True)
time.sleep(120)
"""


def test_standby_takes_over_within_a_second_of_leader_death(
    pg_dsn, pg_storage, aggregator
) -> None:
    """Kill -9 the lock-holding process: Postgres drops its session,
    releasing the advisory lock, and the standby's next poll wins.
    The whole takeover must complete within one second."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, pg_dsn,
         str(_AGGREGATOR_LOCK_KEY)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED"
        # Sanity: while the holder lives, the standby cannot sweep.
        assert aggregator.sweep_once(timeout=0.3) is None

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)

        started = time.monotonic()
        result = aggregator.sweep_once(timeout=5.0)
        elapsed = time.monotonic() - started
        assert result is not None
        assert elapsed <= 1.0, f"takeover took {elapsed:.2f}s"
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)


# ----------------------------------------------------------------------
# Worker CLI
# ----------------------------------------------------------------------


def _worker_args(pg_dsn: str, **overrides) -> argparse.Namespace:
    base = {
        "dsn": pg_dsn,
        "interval_ms": 50,
        "once": False,
        "health": False,
        "max_age_s": 60.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_once_sweeps_and_exits_zero(pg_dsn, pg_storage, capsys) -> None:
    _seed_dirty_run(pg_storage)
    exit_code = aggregator_worker.run(_worker_args(pg_dsn, once=True))
    assert exit_code == 0
    assert "swept 1 runs" in capsys.readouterr().err
    assert pg_storage.get_run("run-1")["aggregates_dirty"] == 0


def test_cli_health_reflects_heartbeat_state(pg_dsn, pg_storage, capsys) -> None:
    # No sweep yet → unhealthy.
    assert aggregator_worker.run(_worker_args(pg_dsn, health=True)) == 1
    assert "no heartbeat" in capsys.readouterr().err

    # After a sweep → healthy.
    assert aggregator_worker.run(_worker_args(pg_dsn, once=True)) == 0
    capsys.readouterr()
    assert aggregator_worker.run(_worker_args(pg_dsn, health=True)) == 0
    assert "last sweep at" in capsys.readouterr().out

    # An old heartbeat → stale.
    pg_storage.write_heartbeat(
        swept_at=int(time.time() * 1000) - 3_600_000, runs_swept=1
    )
    assert aggregator_worker.run(_worker_args(pg_dsn, health=True)) == 1
    assert "STALE" in capsys.readouterr().out
