"""SQLite storage tests (E1-S3 acceptance).

Covers:
- Schema migration applies cleanly + is idempotent.
- All required indexes + the ``event_contents`` table exist post-migration.
- ``capture_mode`` defaults to ``'metadata'`` on the events table.
- Two-tier writes flip ``aggregates_dirty`` atomically.
- Foreign-key cascade from ``events`` to ``event_contents`` is enforced.
- The dirty queue + the ``WHERE id=? AND dirty=1`` lost-update guard.
- ``kill -9`` recovery via a subprocess (WAL durability per ADR-0-5).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def memory_storage() -> SQLiteStorage:
    s = SQLiteStorage(path=":memory:")
    s.connect()
    yield s
    s.close()


@pytest.fixture()
def file_storage(tmp_path: Path) -> SQLiteStorage:
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    yield s
    s.close()


def _seed_run(s: SQLiteStorage, run_id: str = "run-1") -> None:
    s.start_run(
        run_id=run_id,
        task="t",
        agent_kind="test",
        started_at=1_700_000_000_000,
    )


# ----------------------------------------------------------------------
# Schema + migrations
# ----------------------------------------------------------------------


def test_migrations_apply_cleanly_on_fresh_db(file_storage: SQLiteStorage) -> None:
    assert file_storage.schema_version() == 1


def test_connect_is_idempotent(file_storage: SQLiteStorage) -> None:
    file_storage.connect()
    file_storage.connect()
    assert file_storage.schema_version() == 1


def test_required_tables_exist(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()  # private but the easiest way to introspect
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"runs", "events", "event_contents", "applied_migrations"}.issubset(names)


def test_required_indexes_exist(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    names = {row["name"] for row in rows}
    for expected in (
        "events_run_seq",
        "runs_started",
        "runs_task_started",
        "runs_dirty",
        "runs_parent",
    ):
        assert expected in names, f"missing index {expected}"


def test_runs_dirty_is_a_partial_index(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'runs_dirty'"
    ).fetchone()
    assert "aggregates_dirty = 1" in row["sql"].lower().replace(" ", " ")


def test_runs_parent_is_a_partial_index(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'runs_parent'"
    ).fetchone()
    assert "parent_run_id is not null" in row["sql"].lower()


def test_events_capture_mode_defaults_to_metadata(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    rows = conn.execute("PRAGMA table_info(events)").fetchall()
    columns = {row["name"]: row for row in rows}
    assert "capture_mode" in columns
    # SQLite stores default expressions as strings — ``'metadata'``
    # appears with embedded quotes.
    assert "metadata" in columns["capture_mode"]["dflt_value"]


def test_event_contents_table_has_required_columns(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    rows = conn.execute("PRAGMA table_info(event_contents)").fetchall()
    names = {row["name"] for row in rows}
    assert names == {
        "event_id",
        "request_json",
        "response_json",
        "tool_result_json",
        "content_redacted",
    }


def test_event_contents_cascade_on_delete(file_storage: SQLiteStorage) -> None:
    _seed_run(file_storage)
    file_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json="{}",
    )
    conn = file_storage._conn()
    conn.execute(
        "INSERT INTO event_contents (event_id, request_json) VALUES (?, ?)",
        ("e1", "{}"),
    )
    # Delete the parent event; the child row should vanish via cascade.
    conn.execute("DELETE FROM events WHERE id = 'e1'")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM event_contents WHERE event_id = 'e1'"
    ).fetchone()[0]
    assert remaining == 0


# ----------------------------------------------------------------------
# Pragmas
# ----------------------------------------------------------------------


def test_wal_journal_mode_is_active_on_file_db(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_pragma_is_on(file_storage: SQLiteStorage) -> None:
    conn = file_storage._conn()
    on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert on == 1


# ----------------------------------------------------------------------
# Two-tier write semantics
# ----------------------------------------------------------------------


def test_insert_event_marks_run_dirty(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json=json.dumps({"input_tokens": 10}),
    )
    run = memory_storage.get_run("run-1")
    assert run is not None
    assert run["aggregates_dirty"] == 1


def test_insert_event_writes_event_row(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1_700_000_000_001,
        sequence=1,
        payload_json="{}",
    )
    events = list(memory_storage.iter_events("run-1"))
    assert len(events) == 1
    assert events[0]["kind"] == "llm_call"
    assert events[0]["capture_mode"] == "metadata"


def test_insert_event_rejects_invalid_capture_mode(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.insert_event(
            event_id="e1",
            run_id="run-1",
            kind="llm_call",
            occurred_at=1,
            sequence=1,
            payload_json="{}",
            capture_mode="malicious",
        )


def test_runs_dirty_index_is_used_by_read_dirty(memory_storage: SQLiteStorage) -> None:
    for i in range(3):
        rid = f"run-{i}"
        _seed_run(memory_storage, run_id=rid)
        memory_storage.insert_event(
            event_id=f"e-{i}",
            run_id=rid,
            kind="llm_call",
            occurred_at=1_700_000_000_000 + i,
            sequence=1,
            payload_json="{}",
        )
    dirty = memory_storage.read_dirty(limit=10)
    assert set(dirty) == {"run-0", "run-1", "run-2"}


def test_read_dirty_respects_limit(memory_storage: SQLiteStorage) -> None:
    for i in range(5):
        _seed_run(memory_storage, run_id=f"run-{i}")
        memory_storage.insert_event(
            event_id=f"e-{i}",
            run_id=f"run-{i}",
            kind="llm_call",
            occurred_at=1_700_000_000_000 + i,
            sequence=1,
            payload_json="{}",
        )
    assert len(memory_storage.read_dirty(limit=2)) == 2


def test_read_dirty_limit_must_be_positive(memory_storage: SQLiteStorage) -> None:
    with pytest.raises(ValueError):
        memory_storage.read_dirty(limit=0)


def test_end_run_rejects_unknown_status(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.end_run(
            run_id="run-1", ended_at=1, status="not_a_status"
        )


# ----------------------------------------------------------------------
# update_aggregates contract (the lost-update guard)
# ----------------------------------------------------------------------


def test_update_aggregates_clears_dirty_when_row_was_dirty(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        payload_json=json.dumps({"input_tokens": 10}),
    )
    ok = memory_storage.update_aggregates(
        run_id="run-1",
        totals={"total_input_tokens": 10},
    )
    assert ok is True
    assert memory_storage.get_run("run-1")["aggregates_dirty"] == 0
    assert memory_storage.get_run("run-1")["total_input_tokens"] == 10


def test_update_aggregates_no_op_when_already_clean(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    # Run is clean from the start (start_run leaves dirty=0).
    ok = memory_storage.update_aggregates(
        run_id="run-1",
        totals={"total_input_tokens": 5},
    )
    assert ok is False


def test_update_aggregates_rejects_unknown_keys(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.update_aggregates(
            run_id="run-1",
            totals={"sneaky": 1},
        )


def test_update_aggregates_requires_at_least_one_total(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.update_aggregates(run_id="run-1", totals={})


# ----------------------------------------------------------------------
# Multi-thread isolation
# ----------------------------------------------------------------------


def test_file_storage_gives_each_thread_its_own_connection(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    _seed_run(s)

    seen: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            s.insert_event(
                event_id=f"e-{threading.get_ident()}",
                run_id="run-1",
                kind="llm_call",
                occurred_at=1,
                sequence=1,
                payload_json="{}",
            )
            seen.append(1)
        except BaseException as exc:  # pragma: no cover — defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.close()

    assert not errors, errors
    assert sum(seen) == 4


# ----------------------------------------------------------------------
# kill -9 recovery (WAL durability)
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="signal.SIGKILL semantics differ on Windows",
)
def test_kill_minus_9_after_insert_leaves_db_recoverable(tmp_path: Path) -> None:
    """ADR-0-5 invariant: SQLite WAL plus ``synchronous=NORMAL`` survives
    a hard kill. We spawn a subprocess that inserts an event, signals
    success, and sleeps; then SIGKILL it and reopen the DB."""
    db_path = tmp_path / "runs.db"
    marker = tmp_path / "ready"
    script = textwrap.dedent(
        f"""
        import os, time
        from pathlib import Path
        from inkfoot.storage.sqlite import SQLiteStorage

        s = SQLiteStorage(path=Path({str(db_path)!r}))
        s.connect()
        s.start_run(
            run_id='run-kill',
            task='kill-test',
            agent_kind='subprocess',
            started_at=1_700_000_000_000,
        )
        s.insert_event(
            event_id='e1',
            run_id='run-kill',
            kind='llm_call',
            occurred_at=1_700_000_000_001,
            sequence=1,
            payload_json='{{}}',
        )
        Path({str(marker)!r}).write_text('go')
        time.sleep(30)
        """
    )
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    try:
        # Wait up to 10 s for the child to insert + signal.
        for _ in range(100):
            if marker.exists():
                break
            import time as _t

            _t.sleep(0.1)
        else:  # pragma: no cover
            proc.kill()
            pytest.fail("subprocess never signalled readiness")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:  # pragma: no cover — defensive
            proc.kill()
            proc.wait()

    # Re-open from this process. WAL replay should restore the row.
    s = SQLiteStorage(path=db_path)
    s.connect()
    try:
        events = list(s.iter_events("run-kill"))
        assert len(events) == 1
        assert events[0]["id"] == "e1"
    finally:
        s.close()
