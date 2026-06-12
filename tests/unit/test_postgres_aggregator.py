"""Unit tests for the advisory-lock aggregator — no server needed.

The lock primitives themselves need a real Postgres (covered in
``tests/integration/test_postgres_aggregator.py``); here the lock is
stubbed out and the tests pin down the sweep orchestration: lock →
drain → heartbeat → unlock, timeout/stop behavior, and the
``--health`` probe's verdicts.
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Iterable, Optional

import pytest

from inkfoot.cli import aggregator_worker
from inkfoot.storage.postgres_aggregator import (
    _AGGREGATOR_LOCK_KEY,
    AGGREGATOR_LOCK_NAME,
    PostgresAggregator,
)
from inkfoot.storage.postgres_migrations import advisory_lock_key


class _FakeStorage:
    """In-memory stand-in for PostgresStorage: one dirty run, plus a
    recording heartbeat."""

    dsn = "postgresql://unused"

    def __init__(self, dirty: Optional[list[str]] = None) -> None:
        self._dirty = list(dirty or [])
        self.totals_written: dict[str, dict[str, Any]] = {}
        self.heartbeats: list[tuple[int, int]] = []

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        batch, self._dirty = self._dirty[:limit], self._dirty[limit:]
        return batch

    def claim_clean(self, run_id: str) -> bool:
        return True

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        return iter(())

    def write_totals(
        self, *, run_id: str, totals: dict[str, Any]
    ) -> None:
        self.totals_written[run_id] = totals

    def write_heartbeat(self, *, swept_at: int, runs_swept: int) -> None:
        self.heartbeats.append((swept_at, runs_swept))

    def read_heartbeat(self) -> Optional[dict[str, int]]:
        if not self.heartbeats:
            return None
        swept_at, runs_swept = self.heartbeats[-1]
        return {"last_sweep_at": swept_at, "runs_swept": runs_swept}


def _locked_open(
    aggregator: PostgresAggregator, *, acquired: bool = True
) -> list[str]:
    """Stub the advisory-lock primitives; return the call journal."""
    journal: list[str] = []

    def fake_try_lock() -> bool:
        journal.append("try_lock")
        return acquired

    def fake_unlock() -> None:
        journal.append("unlock")

    aggregator._try_lock = fake_try_lock  # type: ignore[method-assign]
    aggregator._unlock = fake_unlock  # type: ignore[method-assign]
    return journal


# ----------------------------------------------------------------------
# Lock identity + construction
# ----------------------------------------------------------------------


def test_lock_key_derives_from_the_published_name() -> None:
    assert _AGGREGATOR_LOCK_KEY == advisory_lock_key(AGGREGATOR_LOCK_NAME)
    assert AGGREGATOR_LOCK_NAME == "inkfoot_aggregator"


@pytest.mark.parametrize("bad_poll", [0, -0.1])
def test_non_positive_lock_poll_raises(bad_poll: float) -> None:
    with pytest.raises(ValueError, match="lock_poll_seconds"):
        PostgresAggregator(_FakeStorage(), lock_poll_seconds=bad_poll)


def test_try_lock_requires_connect() -> None:
    aggregator = PostgresAggregator(_FakeStorage())
    with pytest.raises(RuntimeError, match="not connected"):
        aggregator._try_lock()


# ----------------------------------------------------------------------
# sweep_once orchestration
# ----------------------------------------------------------------------


def test_sweep_drains_and_writes_heartbeat_then_unlocks() -> None:
    storage = _FakeStorage(dirty=["run-1", "run-2"])
    aggregator = PostgresAggregator(storage, interval_seconds=0.01)
    journal = _locked_open(aggregator)

    processed = aggregator.sweep_once()

    assert processed == 2
    assert set(storage.totals_written) == {"run-1", "run-2"}
    assert len(storage.heartbeats) == 1
    assert storage.heartbeats[0][1] == 2
    assert journal == ["try_lock", "unlock"]


def test_sweep_with_empty_queue_still_heartbeats() -> None:
    """A zero-work sweep proves liveness too — the health probe must
    not report a healthy-but-idle worker as stale."""
    storage = _FakeStorage(dirty=[])
    aggregator = PostgresAggregator(storage, interval_seconds=0.01)
    _locked_open(aggregator)

    assert aggregator.sweep_once() == 0
    assert len(storage.heartbeats) == 1
    assert storage.heartbeats[0][1] == 0


def test_sweep_unlocks_even_when_drain_raises() -> None:
    storage = _FakeStorage(dirty=["run-1"])
    aggregator = PostgresAggregator(storage, interval_seconds=0.01)
    journal = _locked_open(aggregator)

    def boom() -> int:
        raise RuntimeError("storage went away")

    aggregator._worker.drain_once = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="storage went away"):
        aggregator.sweep_once()
    assert journal[-1] == "unlock"


def test_sweep_times_out_when_lock_is_held_elsewhere() -> None:
    storage = _FakeStorage()
    aggregator = PostgresAggregator(
        storage, interval_seconds=0.01, lock_poll_seconds=0.01
    )
    _locked_open(aggregator, acquired=False)

    started = time.monotonic()
    assert aggregator.sweep_once(timeout=0.05) is None
    assert time.monotonic() - started < 2.0
    assert storage.heartbeats == []


def test_sweep_returns_none_when_stopped_while_waiting() -> None:
    storage = _FakeStorage()
    aggregator = PostgresAggregator(
        storage, interval_seconds=0.01, lock_poll_seconds=0.01
    )
    _locked_open(aggregator, acquired=False)
    aggregator.stop()
    assert aggregator.sweep_once() is None


def test_run_forever_exits_on_stop() -> None:
    storage = _FakeStorage()
    aggregator = PostgresAggregator(
        storage, interval_seconds=0.01, lock_poll_seconds=0.01
    )
    _locked_open(aggregator)
    aggregator.connect = lambda: None  # type: ignore[method-assign]

    sweeps = []
    original = aggregator.sweep_once

    def counting_sweep(**kwargs: Any) -> Optional[int]:
        sweeps.append(1)
        if len(sweeps) >= 3:
            aggregator.stop()
        return original(**kwargs)

    aggregator.sweep_once = counting_sweep  # type: ignore[method-assign]
    aggregator.run_forever()  # must return rather than spin forever
    assert len(sweeps) >= 3


def test_run_forever_returns_immediately_when_pre_stopped() -> None:
    aggregator = PostgresAggregator(_FakeStorage(), interval_seconds=0.01)
    aggregator.stop()
    aggregator.run_forever()  # no connect attempt, no exception


# ----------------------------------------------------------------------
# --health probe
# ----------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_health_without_heartbeat_is_unhealthy(capsys) -> None:
    exit_code = aggregator_worker._health(_FakeStorage(), max_age_s=60)
    assert exit_code == 1
    assert "no heartbeat" in capsys.readouterr().err


def test_health_with_recent_heartbeat_is_healthy(capsys) -> None:
    storage = _FakeStorage()
    storage.write_heartbeat(swept_at=_now_ms(), runs_swept=7)
    exit_code = aggregator_worker._health(storage, max_age_s=60)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "last sweep at" in captured.out
    assert "7 runs swept" in captured.out


def test_health_with_stale_heartbeat_is_unhealthy(capsys) -> None:
    storage = _FakeStorage()
    storage.write_heartbeat(
        swept_at=_now_ms() - 3_600_000, runs_swept=1
    )
    exit_code = aggregator_worker._health(storage, max_age_s=60)
    assert exit_code == 1
    assert "STALE" in capsys.readouterr().out


def test_format_ts_renders_utc_iso() -> None:
    assert aggregator_worker._format_ts(0) == "1970-01-01T00:00:00.000Z"


def test_cli_exits_2_when_no_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("INKFOOT_PG_DSN", raising=False)
    args = argparse.Namespace(
        dsn=None,
        interval_ms=None,
        once=False,
        health=False,
        max_age_s=60.0,
    )
    assert aggregator_worker.run(args) == 2
    assert "INKFOOT_PG_DSN" in capsys.readouterr().err
