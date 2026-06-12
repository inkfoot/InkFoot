"""``inkfoot migrate`` subcommand — copy a SQLite database to Postgres.

One-way, resumable cutover tool. The intended flow:

1. Stop (or pause) the agents writing to the SQLite database.
2. ``inkfoot migrate --to postgres --dsn postgresql://…``
3. Point the agents at the Postgres DSN and start the
   ``inkfoot aggregator-worker`` process.

The copy is deliberately boring and idempotent:

* The Postgres schema is created first (same migration DDL that
  ``PostgresStorage.connect()`` applies — running this tool against
  an already-initialised database is fine).
* ``runs`` is copied in one transaction. The parent-run foreign key
  is deferred to commit time, so parents and children can land in
  any order within that transaction; an interrupt rolls the whole
  phase back, leaving no half-copied graph.
* ``events`` and ``event_contents`` are copied in batches, one
  transaction per batch, so an interrupt loses at most one batch of
  progress.
* Every phase resumes from ``MAX(id)`` on the Postgres side and
  inserts with ``ON CONFLICT DO NOTHING`` — re-running after an
  interrupt (or after success) never duplicates and never errors.
  This resume strategy assumes the Postgres tables are not receiving
  *other* inkfoot writes while the migration runs — hence step 1/3
  ordering above.
* After the copy, row counts are verified, ``VACUUM ANALYZE``
  refreshes planner statistics, and the source SQLite file is
  renamed to ``<name>.migrated`` (never deleted) so a stale
  configuration can't silently keep writing to the old file.

Progress goes to stderr (one line per batch); the final row-count
summary goes to stdout.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, Sequence

from inkfoot.errors import StorageError

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

DEFAULT_RUNS_BATCH = 1000
DEFAULT_EVENTS_BATCH = 10000

_MIGRATED_SUFFIX = ".migrated"

# Explicit column lists shared by the SQLite SELECT and the Postgres
# INSERT — same names on both sides by design. Order matters: rows
# are passed positionally.
_RUNS_COLUMNS = (
    "id",
    "task",
    "agent_kind",
    "parent_run_id",
    "run_kind",
    "divergence_flag",
    "started_at",
    "ended_at",
    "status",
    "outcome",
    "quality_score",
    "total_input_tokens",
    "total_output_tokens",
    "total_cache_read_tokens",
    "total_cache_creation_tokens",
    "total_nanodollars",
    "aggregates_dirty",
    "metadata_json",
)
_EVENTS_COLUMNS = (
    "id",
    "run_id",
    "kind",
    "occurred_at",
    "payload_json",
    "sequence",
    "capture_mode",
)
_CONTENTS_COLUMNS = (
    "event_id",
    "request_json",
    "response_json",
    "tool_result_json",
    "content_redacted",
)


@dataclass
class MigrationReport:
    """What the migration did, table by table."""

    source_path: Path
    runs_in_source: int = 0
    events_in_source: int = 0
    contents_in_source: int = 0
    runs_copied: int = 0
    events_copied: int = 0
    contents_copied: int = 0
    renamed_to: Optional[Path] = None
    elapsed_seconds: float = 0.0
    extra_renamed: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "migration complete in "
            f"{self.elapsed_seconds:.1f}s:",
            f"  runs:           {self.runs_copied} copied "
            f"({self.runs_in_source} in source)",
            f"  events:         {self.events_copied} copied "
            f"({self.events_in_source} in source)",
            f"  event_contents: {self.contents_copied} copied "
            f"({self.contents_in_source} in source)",
        ]
        if self.renamed_to is not None:
            lines.append(f"  source renamed: {self.renamed_to}")
        return "\n".join(lines)


def _stderr_progress(message: str) -> None:
    print(message, file=sys.stderr)


def _batched_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    key_column: str,
    start_after: Optional[str],
    batch_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    """Keyset-paginated read of ``table`` in ``key_column`` order.

    Keyset (``WHERE key > last``) rather than OFFSET so the read cost
    stays linear in the table size. IDs are ULIDs, so lexicographic
    order is also chronological order.
    """
    column_sql = ", ".join(columns)
    cursor: Optional[str] = start_after
    while True:
        if cursor is None:
            rows = conn.execute(
                f"SELECT {column_sql} FROM {table}"
                f" ORDER BY {key_column} LIMIT ?",
                (batch_size,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {column_sql} FROM {table}"
                f" WHERE {key_column} > ?"
                f" ORDER BY {key_column} LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
        if not rows:
            return
        key_index = list(columns).index(key_column)
        cursor = rows[-1][key_index]
        yield [tuple(row) for row in rows]


def _insert_sql(table: str, columns: Sequence[str], key_column: str) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    return (
        f"INSERT INTO {table} ({', '.join(columns)})"
        f" VALUES ({placeholders})"
        f" ON CONFLICT ({key_column}) DO NOTHING"
    )


def _pg_max_id(
    conn: "psycopg.Connection[Any]", table: str, key_column: str
) -> Optional[str]:
    # Table/column names interpolated here come from module-level
    # constants, never caller input.
    row = conn.execute(
        f"SELECT MAX({key_column}) FROM {table}"
    ).fetchone()
    return row[0] if row else None


def _pg_count(conn: "psycopg.Connection[Any]", table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def migrate_sqlite_to_postgres(
    *,
    sqlite_path: Path,
    dsn: str,
    runs_batch: int = DEFAULT_RUNS_BATCH,
    events_batch: int = DEFAULT_EVENTS_BATCH,
    progress: Callable[[str], None] = _stderr_progress,
) -> MigrationReport:
    """Copy every row from the SQLite database into Postgres, verify,
    and rename the source file out of the way.

    Raises :class:`~inkfoot.errors.StorageError` when the source is
    missing, the psycopg stack isn't installed, or post-copy
    verification fails. Safe to re-run after any failure or
    interrupt — completed work is skipped via the resume cursors.
    """
    if runs_batch < 1 or events_batch < 1:
        raise ValueError("batch sizes must be >= 1")
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise StorageError(f"SQLite database not found: {sqlite_path}")

    try:
        import psycopg  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — exercised via msg
        raise StorageError(
            "the Postgres migration requires the psycopg stack — "
            "install it with: pip install 'inkfoot[postgres]'"
        ) from exc
    from inkfoot.storage.postgres_migrations import (  # noqa: PLC0415
        apply_migrations,
    )

    started = time.monotonic()
    report = MigrationReport(source_path=sqlite_path)

    src = sqlite3.connect(
        f"file:{sqlite_path}?mode=ro", uri=True, timeout=30.0
    )
    try:
        report.runs_in_source = _sqlite_count(src, "runs")
        report.events_in_source = _sqlite_count(src, "events")
        report.contents_in_source = _sqlite_count(src, "event_contents")
        progress(
            f"source: {report.runs_in_source} runs, "
            f"{report.events_in_source} events, "
            f"{report.contents_in_source} event_contents"
        )

        # autocommit, so each ``with pg.transaction():`` below is a
        # real top-level transaction. On a non-autocommit connection
        # the first statement opens an *implicit* outer transaction
        # and every transaction() block degrades to a savepoint —
        # an interrupt would then roll back already-"committed"
        # batches and break resumability.
        with psycopg.connect(dsn, autocommit=True) as pg:
            apply_migrations(pg)

            # ---- runs: one transaction, deferred FK checked at commit
            resume = _pg_max_id(pg, "runs", "id")
            if resume is not None:
                progress(f"runs: resuming after id {resume}")
            insert = _insert_sql("runs", _RUNS_COLUMNS, "id")
            with pg.transaction():
                for batch in _batched_rows(
                    src,
                    table="runs",
                    columns=_RUNS_COLUMNS,
                    key_column="id",
                    start_after=resume,
                    batch_size=runs_batch,
                ):
                    with pg.cursor() as cur:
                        cur.executemany(insert, batch)
                    report.runs_copied += len(batch)
                    progress(
                        f"runs: {report.runs_copied} copied"
                        f" (of {report.runs_in_source})"
                    )

            # ---- events: one transaction per batch (resume granularity)
            resume = _pg_max_id(pg, "events", "id")
            if resume is not None:
                progress(f"events: resuming after id {resume}")
            insert = _insert_sql("events", _EVENTS_COLUMNS, "id")
            for batch in _batched_rows(
                src,
                table="events",
                columns=_EVENTS_COLUMNS,
                key_column="id",
                start_after=resume,
                batch_size=events_batch,
            ):
                with pg.transaction():
                    with pg.cursor() as cur:
                        cur.executemany(insert, batch)
                report.events_copied += len(batch)
                progress(
                    f"events: {report.events_copied} copied"
                    f" (of {report.events_in_source})"
                )

            # ---- event_contents: after events so the FK is satisfied
            resume = _pg_max_id(pg, "event_contents", "event_id")
            if resume is not None:
                progress(f"event_contents: resuming after id {resume}")
            insert = _insert_sql(
                "event_contents", _CONTENTS_COLUMNS, "event_id"
            )
            for batch in _batched_rows(
                src,
                table="event_contents",
                columns=_CONTENTS_COLUMNS,
                key_column="event_id",
                start_after=resume,
                batch_size=events_batch,
            ):
                with pg.transaction():
                    with pg.cursor() as cur:
                        cur.executemany(insert, batch)
                report.contents_copied += len(batch)
                progress(
                    f"event_contents: {report.contents_copied} copied"
                    f" (of {report.contents_in_source})"
                )

            # ---- verify before touching the source file
            mismatches = []
            for table, expected in (
                ("runs", report.runs_in_source),
                ("events", report.events_in_source),
                ("event_contents", report.contents_in_source),
            ):
                actual = _pg_count(pg, table)
                if actual < expected:
                    mismatches.append(
                        f"{table}: {actual} rows in Postgres,"
                        f" {expected} in source"
                    )
            if mismatches:
                raise StorageError(
                    "post-copy verification failed — source NOT renamed;"
                    " re-run to resume: " + "; ".join(mismatches)
                )

        # VACUUM ANALYZE cannot run inside a transaction block, so it
        # gets its own autocommit connection.
        with psycopg.connect(dsn, autocommit=True) as pg:
            for table in ("runs", "events", "event_contents"):
                progress(f"VACUUM ANALYZE {table}")
                pg.execute(f"VACUUM ANALYZE {table}")
    finally:
        src.close()

    # ---- rename the source out of the way (never delete)
    target = sqlite_path.with_name(sqlite_path.name + _MIGRATED_SUFFIX)
    sqlite_path.rename(target)
    report.renamed_to = target
    # A cleanly-closed database leaves no WAL sidecars behind, but a
    # crashed writer might have; move them with the main file so a
    # later re-open can still recover them.
    for suffix in ("-wal", "-shm"):
        sidecar = sqlite_path.with_name(sqlite_path.name + suffix)
        if sidecar.exists():
            sidecar_target = sidecar.with_name(
                sidecar.name + _MIGRATED_SUFFIX
            )
            sidecar.rename(sidecar_target)
            report.extra_renamed.append(sidecar_target)

    report.elapsed_seconds = time.monotonic() - started
    return report


def run(args: argparse.Namespace) -> int:
    if args.to != "postgres":  # pragma: no cover — argparse enforces
        print(f"migrate: unknown target {args.to!r}", file=sys.stderr)
        return 2

    from inkfoot.storage.sqlite import _default_db_path  # noqa: PLC0415

    sqlite_path = (
        Path(args.db) if args.db is not None else _default_db_path()
    )

    # Friendly no-op when the migration already ran: the source is
    # gone and the renamed file sits next to where it used to be.
    migrated_marker = sqlite_path.with_name(
        sqlite_path.name + _MIGRATED_SUFFIX
    )
    if not sqlite_path.exists() and migrated_marker.exists():
        print(
            f"migrate: nothing to do — {sqlite_path} was already "
            f"migrated (found {migrated_marker})"
        )
        return 0

    dsn = args.dsn or os.environ.get("INKFOOT_PG_DSN")
    if not dsn:
        print(
            "migrate: no Postgres DSN — pass --dsn or set the "
            "INKFOOT_PG_DSN environment variable",
            file=sys.stderr,
        )
        return 2

    try:
        report = migrate_sqlite_to_postgres(
            sqlite_path=sqlite_path,
            dsn=dsn,
            runs_batch=args.runs_batch,
            events_batch=args.events_batch,
        )
    except (StorageError, ValueError) as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "migrate: interrupted — completed batches are kept; "
            "re-run the same command to resume",
            file=sys.stderr,
        )
        return 130

    print(report.summary())
    return 0
