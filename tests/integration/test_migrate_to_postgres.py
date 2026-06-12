"""End-to-end tests for ``inkfoot migrate`` against a real Postgres.

Opt-in via ``INKFOOT_TEST_PG_DSN``. Covers the cutover contract: a
faithful full copy (including the deferred parent FK and replay
contents), resume after an interrupt, the idempotent re-run, and the
source rename. The volume/timing case is additionally marked
``slow``.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("INKFOOT_TEST_PG_DSN"),
        reason="INKFOOT_TEST_PG_DSN not set",
    ),
]

psycopg = pytest.importorskip("psycopg")

from inkfoot.cli import migrate as migrate_mod  # noqa: E402
from inkfoot.cli.migrate import migrate_sqlite_to_postgres  # noqa: E402
from inkfoot.storage.sqlite import SQLiteStorage  # noqa: E402


def _seed_source(path: Path) -> None:
    """A small but representative source DB: a parent/child pair
    whose *child id sorts first* (exercises the deferred FK), replay
    contents, and projected totals."""
    storage = SQLiteStorage(path=path)
    storage.connect()
    try:
        # Child id ("run-a…") sorts before parent id ("run-z…"), so
        # the Postgres copy inserts the child row first — only legal
        # because the FK is deferred to commit.
        storage.start_run(
            run_id="run-z-parent",
            task="parent-task",
            agent_kind="root-agent",
            started_at=1000,
            metadata_json='{"origin": "test"}',
        )
        storage.start_run(
            run_id="run-a-child",
            task="child-task",
            agent_kind=None,
            started_at=1001,
            parent_run_id="run-z-parent",
            run_kind="subagent",
        )
        storage.insert_event(
            event_id="evt-1",
            run_id="run-z-parent",
            kind="llm_call",
            occurred_at=1002,
            sequence=1,
            payload_json='{"input_tokens": 10, "output_tokens": 3}',
        )
        storage.insert_event(
            event_id="evt-2",
            run_id="run-z-parent",
            kind="llm_call",
            occurred_at=1003,
            sequence=2,
            capture_mode="replay",
            request_json='{"messages": ["hi"]}',
            response_json='{"content": "hello"}',
        )
        storage.end_run(
            run_id="run-z-parent", ended_at=1010, status="complete"
        )
        storage.write_totals(
            run_id="run-z-parent",
            totals={"total_input_tokens": 10, "total_output_tokens": 3},
        )
    finally:
        storage.close()


def _pg_count(dsn: str, table: str) -> int:
    with psycopg.connect(dsn) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _quiet(_message: str) -> None:
    pass


# ----------------------------------------------------------------------
# The happy path
# ----------------------------------------------------------------------


def test_full_migration_copies_everything_and_renames(
    tmp_path: Path, pg_dsn: str
) -> None:
    source = tmp_path / "runs.db"
    _seed_source(source)

    report = migrate_sqlite_to_postgres(
        sqlite_path=source, dsn=pg_dsn, progress=_quiet
    )

    assert report.runs_copied == 2
    assert report.events_copied == 2
    assert report.contents_copied == 1
    assert _pg_count(pg_dsn, "runs") == 2
    assert _pg_count(pg_dsn, "events") == 2
    assert _pg_count(pg_dsn, "event_contents") == 1

    # Source renamed, never deleted.
    assert not source.exists()
    assert report.renamed_to == tmp_path / "runs.db.migrated"
    assert report.renamed_to.exists()

    # Spot-check copied values through the Postgres backend itself.
    from inkfoot.storage.postgres import PostgresStorage

    storage = PostgresStorage(dsn=pg_dsn, pool_min=1, pool_max=1)
    storage.connect()
    try:
        parent = storage.get_run("run-z-parent")
        assert parent["task"] == "parent-task"
        assert parent["status"] == "complete"
        assert parent["ended_at"] == 1010
        assert parent["total_input_tokens"] == 10
        assert parent["metadata_json"] == '{"origin": "test"}'
        child = storage.get_run("run-a-child")
        assert child["parent_run_id"] == "run-z-parent"
        assert child["run_kind"] == "subagent"
        events = list(storage.iter_events("run-z-parent"))
        assert [e["id"] for e in events] == ["evt-1", "evt-2"]
    finally:
        storage.close()

    with psycopg.connect(pg_dsn) as conn:
        content = conn.execute(
            "SELECT request_json, response_json FROM event_contents"
            " WHERE event_id = %s",
            ("evt-2",),
        ).fetchone()
    assert content == ('{"messages": ["hi"]}', '{"content": "hello"}')


def test_wal_sidecars_are_renamed_too(tmp_path: Path, pg_dsn: str) -> None:
    source = tmp_path / "runs.db"
    _seed_source(source)
    # Simulate a crashed writer that left sidecars behind.
    (tmp_path / "runs.db-wal").write_bytes(b"")
    (tmp_path / "runs.db-shm").write_bytes(b"")

    report = migrate_sqlite_to_postgres(
        sqlite_path=source, dsn=pg_dsn, progress=_quiet
    )

    assert (tmp_path / "runs.db-wal.migrated").exists()
    assert (tmp_path / "runs.db-shm.migrated").exists()
    assert not (tmp_path / "runs.db-wal").exists()
    assert len(report.extra_renamed) == 2


# ----------------------------------------------------------------------
# Resume + idempotency
# ----------------------------------------------------------------------


def _seed_bulk(path: Path, *, runs: int, events_per_run: int) -> None:
    """Seed via raw SQL so large volumes stay fast; the schema comes
    from the real storage layer."""
    storage = SQLiteStorage(path=path)
    storage.connect()
    storage.close()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO runs (id, task, started_at) VALUES (?, ?, ?)",
                [(f"run-{r:06d}", "bulk", 1000 + r) for r in range(runs)],
            )
            conn.executemany(
                "INSERT INTO events (id, run_id, kind, occurred_at,"
                " sequence) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        f"evt-{r:06d}-{e:06d}",
                        f"run-{r:06d}",
                        "llm_call",
                        1000 + e,
                        e + 1,
                    )
                    for r in range(runs)
                    for e in range(events_per_run)
                ],
            )
    finally:
        conn.close()


def test_interrupted_migration_resumes_without_duplicates(
    tmp_path: Path, pg_dsn: str
) -> None:
    source = tmp_path / "runs.db"
    _seed_bulk(source, runs=4, events_per_run=10)  # 40 events

    class _Interrupt(Exception):
        pass

    events_batches_seen = 0

    def interrupting_progress(message: str) -> None:
        nonlocal events_batches_seen
        if message.startswith("events:") and "copied" in message:
            events_batches_seen += 1
            if events_batches_seen == 2:
                raise _Interrupt()

    with pytest.raises(_Interrupt):
        migrate_sqlite_to_postgres(
            sqlite_path=source,
            dsn=pg_dsn,
            events_batch=10,
            progress=interrupting_progress,
        )

    # Two committed batches survived the interrupt; the source file
    # is untouched.
    assert _pg_count(pg_dsn, "events") == 20
    assert source.exists()

    report = migrate_sqlite_to_postgres(
        sqlite_path=source, dsn=pg_dsn, events_batch=10, progress=_quiet
    )

    # The re-run copied only the remainder.
    assert report.events_copied == 20
    assert _pg_count(pg_dsn, "events") == 40
    assert _pg_count(pg_dsn, "runs") == 4
    assert not source.exists()


def test_cli_rerun_after_success_is_a_no_op(
    tmp_path: Path, pg_dsn: str, capsys
) -> None:
    source = tmp_path / "runs.db"
    _seed_source(source)
    args = argparse.Namespace(
        to="postgres",
        db=str(source),
        dsn=pg_dsn,
        runs_batch=migrate_mod.DEFAULT_RUNS_BATCH,
        events_batch=migrate_mod.DEFAULT_EVENTS_BATCH,
    )

    assert migrate_mod.run(args) == 0
    first = capsys.readouterr()
    assert "migration complete" in first.out

    assert migrate_mod.run(args) == 0
    second = capsys.readouterr()
    assert "already" in second.out
    assert _pg_count(pg_dsn, "runs") == 2


# ----------------------------------------------------------------------
# Volume / timing
# ----------------------------------------------------------------------


@pytest.mark.slow
def test_hundred_thousand_events_migrate_under_a_minute(
    tmp_path: Path, pg_dsn: str
) -> None:
    source = tmp_path / "runs.db"
    _seed_bulk(source, runs=100, events_per_run=1000)  # 100k events

    started = time.monotonic()
    report = migrate_sqlite_to_postgres(
        sqlite_path=source, dsn=pg_dsn, progress=_quiet
    )
    elapsed = time.monotonic() - started

    assert report.events_copied == 100_000
    assert _pg_count(pg_dsn, "events") == 100_000
    assert elapsed < 60, f"migration took {elapsed:.1f}s"
