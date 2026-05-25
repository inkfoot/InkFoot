"""Aggregator worker tests (E1-S4 acceptance).

Covers:
- Pure ``project_run_totals`` projection over the event log.
- ``drain_once`` aggregates dirty rows + clears the dirty flag.
- ``WHERE id=? AND dirty=1`` guard: a new event landing mid-sweep
  leaves the row dirty for the next pass (no lost update).
- ``inkfoot rebuild-aggregates`` repairs a manually-corrupted total.
- Background thread starts + stops cleanly.
- Configuration via ``INKFOOT_AGGREGATOR_INTERVAL_MS``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from inkfoot.cli import rebuild_aggregates as cli_rebuild
from inkfoot.storage.aggregator import (
    AggregatorWorker,
    _interval_seconds,
    project_run_totals,
)
from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Path) -> SQLiteStorage:
    s = SQLiteStorage(path=tmp_path / "runs.db")
    s.connect()
    yield s
    s.close()


def _seed_run_with_events(s: SQLiteStorage, run_id: str = "run-1") -> None:
    s.start_run(
        run_id=run_id,
        task="t",
        agent_kind="test",
        started_at=1_700_000_000_000,
    )
    s.insert_event(
        event_id=f"{run_id}-e1",
        run_id=run_id,
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json=json.dumps(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 5,
                "nanodollars": 12_345,
            }
        ),
    )
    s.insert_event(
        event_id=f"{run_id}-e2",
        run_id=run_id,
        kind="llm_call",
        occurred_at=1_700_000_000_002,
        sequence=2,
        payload_json=json.dumps(
            {
                "input_tokens": 200,
                "output_tokens": 80,
                "nanodollars": 67_890,
            }
        ),
    )


# ----------------------------------------------------------------------
# project_run_totals — the pure projection
# ----------------------------------------------------------------------


def test_project_run_totals_sums_known_fields() -> None:
    events = [
        {
            "kind": "llm_call",
            "payload_json": json.dumps(
                {"input_tokens": 10, "output_tokens": 5, "nanodollars": 100}
            ),
        },
        {
            "kind": "llm_call",
            "payload_json": json.dumps(
                {"input_tokens": 7, "cache_read_tokens": 3}
            ),
        },
    ]
    totals = project_run_totals(events)
    assert totals["total_input_tokens"] == 17
    assert totals["total_output_tokens"] == 5
    assert totals["total_cache_read_tokens"] == 3
    assert totals["total_nanodollars"] == 100
    assert totals["outcome"] is None


def test_project_run_totals_handles_missing_payload() -> None:
    events = [{"kind": "llm_call"}, {"kind": "llm_call", "payload_json": None}]
    totals = project_run_totals(events)
    assert totals["total_input_tokens"] == 0


def test_project_run_totals_skips_unparseable_payload() -> None:
    events = [{"kind": "llm_call", "payload_json": "not json {"}]
    totals = project_run_totals(events)
    assert totals["total_input_tokens"] == 0


def test_project_run_totals_skips_non_int_values() -> None:
    events = [
        {
            "kind": "llm_call",
            "payload_json": json.dumps({"input_tokens": "ten"}),
        }
    ]
    assert project_run_totals(events)["total_input_tokens"] == 0


def test_project_run_totals_rejects_bool_as_int() -> None:
    # ``True`` is technically an int subclass; we don't want it to leak
    # into totals as ``1``.
    events = [
        {
            "kind": "llm_call",
            "payload_json": json.dumps({"input_tokens": True}),
        }
    ]
    assert project_run_totals(events)["total_input_tokens"] == 0


def test_project_run_totals_extracts_outcome_from_outcome_event() -> None:
    events = [
        {"kind": "llm_call", "payload_json": json.dumps({"input_tokens": 1})},
        {"kind": "outcome", "payload_json": json.dumps({"outcome": "success"})},
    ]
    assert project_run_totals(events)["outcome"] == "success"


def test_project_run_totals_last_outcome_wins() -> None:
    events = [
        {"kind": "outcome", "payload_json": json.dumps({"outcome": "failure"})},
        {"kind": "outcome", "payload_json": json.dumps({"outcome": "success"})},
    ]
    assert project_run_totals(events)["outcome"] == "success"


# ----------------------------------------------------------------------
# drain_once
# ----------------------------------------------------------------------


def test_drain_once_aggregates_dirty_runs(storage: SQLiteStorage) -> None:
    _seed_run_with_events(storage)
    worker = AggregatorWorker(storage)
    n = worker.drain_once()
    assert n == 1
    run = storage.get_run("run-1")
    assert run["total_input_tokens"] == 300
    assert run["total_output_tokens"] == 130
    assert run["total_cache_read_tokens"] == 10
    assert run["total_cache_creation_tokens"] == 5
    assert run["total_nanodollars"] == 12_345 + 67_890
    assert run["aggregates_dirty"] == 0


def test_drain_once_is_a_noop_when_clean(storage: SQLiteStorage) -> None:
    _seed_run_with_events(storage)
    AggregatorWorker(storage).drain_once()
    n = AggregatorWorker(storage).drain_once()
    assert n == 0


def test_drain_once_batches_in_groups(storage: SQLiteStorage) -> None:
    for i in range(7):
        _seed_run_with_events(storage, run_id=f"run-{i}")
    worker = AggregatorWorker(storage, batch_size=3)
    n = worker.drain_once()
    # All 7 should be drained, even though batches of 3 → 3 → 1.
    assert n == 7


# ----------------------------------------------------------------------
# Lost-update guard — the claim-and-project pattern
# ----------------------------------------------------------------------


def test_claim_clean_is_atomic_compare_and_swap(storage: SQLiteStorage) -> None:
    """claim_clean returns True iff the row was dirty (1 → 0). On a
    clean row it's a no-op returning False. This is the building
    block the aggregator uses to atomically take ownership of a
    projection."""
    _seed_run_with_events(storage)
    assert storage.get_run("run-1")["aggregates_dirty"] == 1

    first = storage.claim_clean("run-1")
    assert first is True
    assert storage.get_run("run-1")["aggregates_dirty"] == 0

    second = storage.claim_clean("run-1")
    assert second is False  # already clean


def test_write_totals_does_not_touch_dirty_flag(storage: SQLiteStorage) -> None:
    """write_totals is unconditional: it updates only the projection
    columns. Touching aggregates_dirty is claim_clean's job and
    insert_event's natural consequence. Verified by re-dirtying the
    row first and confirming write_totals does NOT clear it."""
    _seed_run_with_events(storage)
    storage.write_totals(
        run_id="run-1",
        totals={"total_input_tokens": 42},
    )
    run = storage.get_run("run-1")
    assert run["total_input_tokens"] == 42
    assert run["aggregates_dirty"] == 1  # untouched


def test_late_event_landing_after_claim_is_not_lost(
    storage: SQLiteStorage,
) -> None:
    """The T0→T1→T2 race the reviewer described:

    * T0: aggregator reads events (only the two seeded so far).
    * T1: shim writes a late event via insert_event → dirty=1 again.
    * T2: aggregator writes totals from the T0 snapshot.

    Under the pre-fix one-statement UPDATE-with-WHERE guard, the
    late event would be permanently dropped from totals because
    the WHERE clause would still match (dirty was 1 the whole
    time) and clear the flag. Under claim-and-project, the
    aggregator clears dirty *before* the read, so the late
    insert_event flips dirty back to 1 and the next pass picks the
    row up.
    """
    _seed_run_with_events(storage)  # 2 events, totals input=300

    # T0: claim the row + read its event log.
    assert storage.claim_clean("run-1") is True
    snapshot_events = list(storage.iter_events("run-1"))
    assert len(snapshot_events) == 2

    # T1: a late event arrives during the projection window.
    storage.insert_event(
        event_id="late",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1_700_000_000_500,
        sequence=99,
        payload_json=json.dumps({"input_tokens": 17}),
    )

    # T2: aggregator writes totals from the T0 snapshot.
    totals = project_run_totals(snapshot_events)
    storage.write_totals(run_id="run-1", totals=totals)

    # After T2: stored totals reflect the snapshot (300, not 317),
    # but the row is dirty=1 because of T1's insert_event. The late
    # event is *not* lost — it sits in the events table and will be
    # picked up on the next aggregator pass.
    run = storage.get_run("run-1")
    assert run["total_input_tokens"] == 300
    assert run["aggregates_dirty"] == 1

    # Next pass — full claim/project/write.
    n = AggregatorWorker(storage).drain_once()
    assert n == 1
    final = storage.get_run("run-1")
    assert final["total_input_tokens"] == 317  # includes the late event
    assert final["aggregates_dirty"] == 0


def test_insert_event_landing_after_drain_re_dirties_the_row(
    storage: SQLiteStorage,
) -> None:
    """After drain clears the row, a subsequent insert must re-dirty
    it so the next sweep aggregates the new event."""
    _seed_run_with_events(storage)
    AggregatorWorker(storage).drain_once()
    assert storage.get_run("run-1")["aggregates_dirty"] == 0

    storage.insert_event(
        event_id="post-drain",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1_700_000_000_900,
        sequence=99,
        payload_json=json.dumps({"input_tokens": 5}),
    )
    assert "run-1" in storage.read_dirty(limit=10)


def test_update_aggregates_composite_returns_true_when_dirty(
    storage: SQLiteStorage,
) -> None:
    """update_aggregates is the legacy convenience wrapper —
    equivalent to claim_clean + write_totals. Returns True on a
    dirty row."""
    _seed_run_with_events(storage)
    ok = storage.update_aggregates(
        run_id="run-1",
        totals={"total_input_tokens": 10},
    )
    assert ok is True
    assert storage.get_run("run-1")["total_input_tokens"] == 10
    assert storage.get_run("run-1")["aggregates_dirty"] == 0


def test_update_aggregates_composite_returns_false_when_already_clean(
    storage: SQLiteStorage,
) -> None:
    _seed_run_with_events(storage)
    AggregatorWorker(storage).drain_once()
    ok = storage.update_aggregates(
        run_id="run-1", totals={"total_input_tokens": 999}
    )
    assert ok is False
    # The totals from the prior drain still stand.
    assert storage.get_run("run-1")["total_input_tokens"] == 300


def test_concurrent_insert_during_drain_does_not_lose_event(
    storage: SQLiteStorage,
) -> None:
    """End-to-end race driven by a monkey-patched iter_events that
    side-effects an insert_event between claim and write. Verifies
    that the late event ends up in totals after one extra pass."""
    _seed_run_with_events(storage)
    original_iter = storage.iter_events
    fired = {"yes": False}

    def racy_iter(run_id: str):
        events = list(original_iter(run_id))
        if not fired["yes"]:
            fired["yes"] = True
            # Simulate the shim writing a late event between the
            # aggregator's claim and write_totals.
            storage.insert_event(
                event_id="racy-late",
                run_id=run_id,
                kind="llm_call",
                occurred_at=1_700_000_000_777,
                sequence=88,
                payload_json=json.dumps({"input_tokens": 23}),
            )
        return iter(events)

    storage.iter_events = racy_iter  # type: ignore[method-assign]
    try:
        worker = AggregatorWorker(storage)
        worker.drain_once()  # writes T0 snapshot
        # The late event re-dirtied the row — the next pass should
        # find it and project the full set.
        assert "run-1" in storage.read_dirty(limit=10)
        worker.drain_once()
    finally:
        storage.iter_events = original_iter  # type: ignore[method-assign]

    final = storage.get_run("run-1")
    assert final["total_input_tokens"] == 300 + 23
    assert final["aggregates_dirty"] == 0


# ----------------------------------------------------------------------
# rebuild-aggregates CLI
# ----------------------------------------------------------------------


def test_rebuild_aggregates_repairs_corrupted_total(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    _seed_run_with_events(s)
    AggregatorWorker(s).drain_once()
    assert s.get_run("run-1")["total_input_tokens"] == 300

    # Corrupt the projection directly — pretend a bad migration or
    # manual SQL edit.
    s._conn().execute(
        "UPDATE runs SET total_input_tokens = -999 WHERE id = 'run-1'"
    )
    assert s.get_run("run-1")["total_input_tokens"] == -999

    s.close()

    # Now run the CLI command against the same DB.
    args = SimpleNamespace(db=str(db))
    rc = cli_rebuild.run(args)
    assert rc == 0

    # Reopen and confirm restoration.
    s2 = SQLiteStorage(path=db)
    s2.connect()
    try:
        assert s2.get_run("run-1")["total_input_tokens"] == 300
    finally:
        s2.close()


# ----------------------------------------------------------------------
# Background thread lifecycle
# ----------------------------------------------------------------------


def test_worker_thread_drains_runs_within_poll_interval(storage: SQLiteStorage) -> None:
    _seed_run_with_events(storage)
    worker = AggregatorWorker(storage, interval_seconds=0.05)
    worker.start()
    try:
        # Give it up to 2 s to catch up.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if storage.get_run("run-1")["aggregates_dirty"] == 0:
                break
            time.sleep(0.05)
    finally:
        worker.stop(timeout=2.0)
    assert storage.get_run("run-1")["aggregates_dirty"] == 0


def test_worker_stop_is_idempotent(storage: SQLiteStorage) -> None:
    worker = AggregatorWorker(storage, interval_seconds=0.05)
    worker.start()
    worker.stop()
    worker.stop()  # second call should not raise


def test_worker_start_is_idempotent(storage: SQLiteStorage) -> None:
    worker = AggregatorWorker(storage, interval_seconds=0.05)
    worker.start()
    worker.start()
    worker.stop()


def test_worker_stop_on_never_started_worker_is_safe(
    storage: SQLiteStorage,
) -> None:
    """Constructing a worker and immediately calling stop() (without
    start()) must not drain — there is nothing to flush."""
    worker = AggregatorWorker(storage, interval_seconds=0.05)
    worker.stop()  # must not raise + must not call drain_once


def test_worker_stop_when_storage_already_closed_warns(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """atexit ordering: storage closes before the worker. ``stop``
    must log a warning and return — the daemon thread is already
    joined so there's nothing else to clean up."""
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    worker = AggregatorWorker(s, interval_seconds=0.05)
    worker.start()
    # Pretend something else closed the storage out from under the
    # worker (the atexit-ordering bug).
    s.close()
    with caplog.at_level("WARNING"):
        worker.stop()
    assert any(
        "final drain" in r.message.lower() for r in caplog.records
    ), "expected a warning about the failed final drain"


def test_batch_size_must_be_positive(storage: SQLiteStorage) -> None:
    with pytest.raises(ValueError):
        AggregatorWorker(storage, batch_size=0)


# ----------------------------------------------------------------------
# Env-var configuration
# ----------------------------------------------------------------------


def test_interval_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFOOT_AGGREGATOR_INTERVAL_MS", "1000")
    assert _interval_seconds() == 1.0


def test_interval_env_var_clamps_below_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INKFOOT_AGGREGATOR_INTERVAL_MS", "1")
    with caplog.at_level("WARNING"):
        v = _interval_seconds()
    assert v == 0.010
    assert any("clamping" in r.message for r in caplog.records)


def test_interval_env_var_invalid_string_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INKFOOT_AGGREGATOR_INTERVAL_MS", "not-a-number")
    with caplog.at_level("WARNING"):
        v = _interval_seconds()
    assert v == 0.5
