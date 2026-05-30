"""SQLite storage tests.

Covers:
- Schema migration applies cleanly + is idempotent.
- All required indexes + the ``event_contents`` table exist post-migration.
- ``capture_mode`` defaults to ``'metadata'`` on the events table.
- Two-tier writes flip ``aggregates_dirty`` atomically.
- Foreign-key cascade from ``events`` to ``event_contents`` is enforced.
- The dirty queue + the claim-and-project lost-update guarantee
  (``claim_clean`` / ``write_totals`` / composite ``update_aggregates``).
- Cross-thread connection cleanup via ``close()``.
- ``kill -9`` recovery via a subprocess (WAL durability per ADR-0-5).
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
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
    import re

    conn = file_storage._conn()
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'runs_dirty'"
    ).fetchone()
    normalised = re.sub(r"\s+", " ", row["sql"].lower())
    assert "aggregates_dirty = 1" in normalised


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
# claim_clean / write_totals (the lost-update fix)
# ----------------------------------------------------------------------


def test_claim_clean_returns_true_on_dirty_row(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        payload_json="{}",
    )
    assert memory_storage.claim_clean("run-1") is True
    assert memory_storage.get_run("run-1")["aggregates_dirty"] == 0


def test_claim_clean_returns_false_on_clean_row(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    # start_run leaves dirty=0; nothing has flipped it.
    assert memory_storage.claim_clean("run-1") is False


def test_write_totals_updates_columns_without_touching_dirty(
    memory_storage: SQLiteStorage,
) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="e1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        payload_json="{}",
    )
    # dirty=1 after insert_event; write_totals must leave it alone.
    memory_storage.write_totals(
        run_id="run-1",
        totals={"total_input_tokens": 99, "total_output_tokens": 7},
    )
    run = memory_storage.get_run("run-1")
    assert run["total_input_tokens"] == 99
    assert run["total_output_tokens"] == 7
    assert run["aggregates_dirty"] == 1


def test_write_totals_rejects_unknown_keys(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.write_totals(run_id="run-1", totals={"sneaky": 1})


def test_write_totals_requires_at_least_one_total(memory_storage: SQLiteStorage) -> None:
    _seed_run(memory_storage)
    with pytest.raises(ValueError):
        memory_storage.write_totals(run_id="run-1", totals={})


def test_update_aggregates_composite_claims_then_writes(
    memory_storage: SQLiteStorage,
) -> None:
    """``update_aggregates`` is the legacy convenience wrapper:
    composite claim_clean + write_totals."""
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
    ok = memory_storage.update_aggregates(
        run_id="run-1",
        totals={"total_input_tokens": 5},
    )
    assert ok is False
    # Confirm write_totals did NOT fire (the composite short-circuits
    # when claim_clean returns False).
    assert memory_storage.get_run("run-1")["total_input_tokens"] == 0


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

    def worker(idx: int) -> None:
        try:
            # Use the explicit per-worker index for both event_id and
            # sequence. ``threading.get_ident()`` previously fed both
            # of these but Linux can reuse POSIX TIDs once a thread
            # exits — the short-lived workers below were hitting that
            # reuse on busy CI runners and producing UNIQUE-constraint
            # collisions on events.id (review follow-up).
            s.insert_event(
                event_id=f"e-{idx}",
                run_id="run-1",
                kind="llm_call",
                occurred_at=1,
                sequence=idx + 1,
                payload_json="{}",
            )
            seen.append(1)
        except BaseException as exc:  # pragma: no cover — defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.close()

    assert not errors, errors
    assert sum(seen) == 4


def test_close_closes_connections_opened_on_other_threads(tmp_path: Path) -> None:
    """The pre-fix close() only closed the caller's thread-local
    connection, leaking per-thread connections on every worker
    thread that touched storage. After the fix every connection
    created across every thread is closed."""
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    _seed_run(s)

    # Force connections to be opened on three worker threads. Use an
    # explicit per-worker index for event_id / sequence so two
    # quickly-cycling threads with the same recycled POSIX TID can't
    # collide on the UNIQUE events.id constraint (same flake as the
    # sibling thread-isolation test above).
    def open_conn(idx: int) -> None:
        s.insert_event(
            event_id=f"e-{idx}",
            run_id="run-1",
            kind="llm_call",
            occurred_at=1,
            sequence=idx + 1,
            payload_json="{}",
        )

    threads = [
        threading.Thread(target=open_conn, args=(i,)) for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At this point the storage tracks four connections (main + 3
    # workers). After close, every one of them should be unusable.
    tracked = list(s._connections)
    assert len(tracked) >= 4
    s.close()

    for conn in tracked:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1").fetchone()


def test_close_is_idempotent_after_cross_thread_close(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    s.close()
    s.close()  # second call must not raise


# ----------------------------------------------------------------------
# Storage Protocol signature alignment (Finding #2)
# ----------------------------------------------------------------------


def test_storage_protocol_insert_event_signature_includes_replay_kwargs() -> None:
    """The shim's emit pipeline passes ``request_json`` /
    ``response_json`` / ``tool_result_json`` / ``content_redacted``.
    The Protocol must declare them so a future Postgres backend
    that implements only the Protocol surface accepts the call
    without raising ``TypeError: unexpected keyword argument``.
    """
    import inspect

    from inkfoot.storage import Storage

    sig = inspect.signature(Storage.insert_event)
    for kwarg in (
        "request_json",
        "response_json",
        "tool_result_json",
        "content_redacted",
    ):
        assert kwarg in sig.parameters, (
            f"Storage.insert_event Protocol is missing {kwarg!r} kwarg"
        )


def test_sqlite_storage_signature_matches_protocol() -> None:
    """Cross-check: the concrete SQLite impl agrees with the
    Protocol. If the two drift again, the shim's call site is
    silently wrong and only blows up at runtime against a fresh
    backend."""
    import inspect

    from inkfoot.storage import Storage
    from inkfoot.storage.sqlite import SQLiteStorage

    proto_params = inspect.signature(Storage.insert_event).parameters
    impl_params = inspect.signature(SQLiteStorage.insert_event).parameters
    for name in proto_params:
        assert name in impl_params, (
            f"SQLiteStorage.insert_event missing Protocol param {name!r}"
        )


def test_replay_mode_with_no_content_does_not_write_event_contents(
    memory_storage: SQLiteStorage,
) -> None:
    """Finding #3 regression at the storage layer: when the call site
    passes ``capture_mode='replay'`` but doesn't pass any content
    kwargs, no event_contents row is written. Policy events take
    this path."""
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="policy-event-1",
        run_id="run-1",
        kind="budget_warning",
        occurred_at=1,
        sequence=1,
        payload_json='{"reason": "over budget"}',
        capture_mode="replay",
        # No request_json / response_json / tool_result_json.
    )
    conn = memory_storage._conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM event_contents WHERE event_id = ?",
        ("policy-event-1",),
    ).fetchone()[0]
    assert count == 0


def test_replay_mode_with_request_json_writes_content_row(
    memory_storage: SQLiteStorage,
) -> None:
    _seed_run(memory_storage)
    memory_storage.insert_event(
        event_id="llm-1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        payload_json="{}",
        capture_mode="replay",
        request_json='{"messages": []}',
        response_json='{"usage": {}}',
    )
    conn = memory_storage._conn()
    row = conn.execute(
        "SELECT request_json, response_json FROM event_contents WHERE event_id = ?",
        ("llm-1",),
    ).fetchone()
    assert row is not None
    assert row["request_json"] == '{"messages": []}'
    assert row["response_json"] == '{"usage": {}}'


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
