"""Unit tests for ``inkfoot migrate`` — no Postgres server needed.

The full copy + resume behavior runs against a real server in
``tests/integration/test_migrate_to_postgres.py``; these tests cover
the SQLite-side reading, SQL construction, argument/exit-code
handling, and the already-migrated no-op.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from inkfoot.cli.migrate import (
    DEFAULT_EVENTS_BATCH,
    DEFAULT_RUNS_BATCH,
    MigrationReport,
    _batched_rows,
    _insert_sql,
    migrate_sqlite_to_postgres,
    run,
)
from inkfoot.errors import StorageError
from inkfoot.storage.sqlite import SQLiteStorage


def _make_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "to": "postgres",
        "db": None,
        "dsn": None,
        "runs_batch": DEFAULT_RUNS_BATCH,
        "events_batch": DEFAULT_EVENTS_BATCH,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _seeded_db(path: Path, *, runs: int = 3, events_per_run: int = 2) -> None:
    """Create a real inkfoot SQLite DB with a few runs + events."""
    storage = SQLiteStorage(path=path)
    storage.connect()
    try:
        sequence = 0
        for r in range(runs):
            run_id = f"run-{r:04d}"
            storage.start_run(
                run_id=run_id,
                task="demo",
                agent_kind="test",
                started_at=1000 + r,
            )
            for e in range(events_per_run):
                sequence += 1
                storage.insert_event(
                    event_id=f"evt-{r:04d}-{e:04d}",
                    run_id=run_id,
                    kind="llm_call",
                    occurred_at=1000 + r,
                    sequence=sequence,
                    payload_json='{"input_tokens": 10}',
                )
    finally:
        storage.close()


# ----------------------------------------------------------------------
# SQL construction + keyset reads
# ----------------------------------------------------------------------


def test_insert_sql_has_on_conflict_do_nothing() -> None:
    sql = _insert_sql("events", ("id", "run_id"), "id")
    assert sql.startswith("INSERT INTO events (id, run_id)")
    assert sql.count("%s") == 2
    assert "ON CONFLICT (id) DO NOTHING" in sql


def test_batched_rows_pages_in_key_order(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seeded_db(db, runs=5, events_per_run=0)
    conn = sqlite3.connect(db)
    try:
        batches = list(
            _batched_rows(
                conn,
                table="runs",
                columns=("id", "task"),
                key_column="id",
                start_after=None,
                batch_size=2,
            )
        )
    finally:
        conn.close()
    assert [len(b) for b in batches] == [2, 2, 1]
    ids = [row[0] for batch in batches for row in batch]
    assert ids == sorted(ids)
    assert ids == [f"run-{r:04d}" for r in range(5)]


def test_batched_rows_resumes_after_cursor(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seeded_db(db, runs=5, events_per_run=0)
    conn = sqlite3.connect(db)
    try:
        batches = list(
            _batched_rows(
                conn,
                table="runs",
                columns=("id",),
                key_column="id",
                start_after="run-0002",
                batch_size=10,
            )
        )
    finally:
        conn.close()
    ids = [row[0] for batch in batches for row in batch]
    assert ids == ["run-0003", "run-0004"]


def test_batched_rows_empty_table_yields_nothing(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    _seeded_db(db, runs=0)
    conn = sqlite3.connect(db)
    try:
        assert (
            list(
                _batched_rows(
                    conn,
                    table="runs",
                    columns=("id",),
                    key_column="id",
                    start_after=None,
                    batch_size=10,
                )
            )
            == []
        )
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Validation before any server contact
# ----------------------------------------------------------------------


def test_missing_source_raises_storage_error(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="not found"):
        migrate_sqlite_to_postgres(
            sqlite_path=tmp_path / "absent.db",
            dsn="postgresql://unused",
        )


@pytest.mark.parametrize(
    "kwargs",
    [{"runs_batch": 0}, {"events_batch": 0}, {"runs_batch": -5}],
)
def test_non_positive_batch_sizes_raise(
    tmp_path: Path, kwargs: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="batch sizes"):
        migrate_sqlite_to_postgres(
            sqlite_path=tmp_path / "absent.db",
            dsn="postgresql://unused",
            **kwargs,
        )


# ----------------------------------------------------------------------
# CLI exit codes
# ----------------------------------------------------------------------


def test_run_exits_2_without_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("INKFOOT_PG_DSN", raising=False)
    db = tmp_path / "runs.db"
    _seeded_db(db)
    assert run(_make_args(db=str(db))) == 2
    assert "INKFOOT_PG_DSN" in capsys.readouterr().err


def test_run_exits_1_when_source_missing(
    tmp_path: Path, capsys
) -> None:
    assert (
        run(
            _make_args(
                db=str(tmp_path / "absent.db"),
                dsn="postgresql://unused",
            )
        )
        == 1
    )
    assert "not found" in capsys.readouterr().err


def test_run_is_a_no_op_when_already_migrated(
    tmp_path: Path, capsys
) -> None:
    """Source gone + ``<name>.migrated`` present = the migration
    already happened; re-running reports that and exits 0 so cutover
    scripts can be idempotent."""
    marker = tmp_path / "runs.db.migrated"
    marker.write_bytes(b"")
    exit_code = run(
        _make_args(
            db=str(tmp_path / "runs.db"), dsn="postgresql://unused"
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "already" in captured.out
    assert "runs.db.migrated" in captured.out


def test_run_missing_source_without_marker_is_an_error(
    tmp_path: Path, capsys
) -> None:
    """No source and no .migrated marker is NOT the friendly no-op —
    it's a misconfiguration the user needs to see."""
    assert (
        run(
            _make_args(
                db=str(tmp_path / "never-existed.db"),
                dsn="postgresql://unused",
            )
        )
        == 1
    )
    assert "not found" in capsys.readouterr().err


# ----------------------------------------------------------------------
# Report rendering
# ----------------------------------------------------------------------


def test_report_summary_includes_counts_and_rename(tmp_path: Path) -> None:
    report = MigrationReport(
        source_path=tmp_path / "runs.db",
        runs_in_source=10,
        events_in_source=200,
        contents_in_source=3,
        runs_copied=10,
        events_copied=200,
        contents_copied=3,
        renamed_to=tmp_path / "runs.db.migrated",
        elapsed_seconds=1.25,
    )
    text = report.summary()
    assert "10 copied" in text
    assert "200 copied" in text
    assert "runs.db.migrated" in text
    assert "1.2s" in text or "1.3s" in text
