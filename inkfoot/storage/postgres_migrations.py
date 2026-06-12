"""Forward-only DDL list for the Postgres storage layer.

Same logical shape as the SQLite schema — ``runs``, ``events``,
``event_contents`` with identical column names — so the row dicts a
backend hands to callers are byte-for-byte interchangeable. Type
mapping: SQLite ``INTEGER`` timestamps/counters become ``BIGINT``,
``REAL`` becomes ``DOUBLE PRECISION``; 0/1 flags stay ``INTEGER`` so
readers see the same values on both backends.

Two deliberate deviations from the SQLite DDL:

* ``runs.parent_run_id``'s self-referencing foreign key is
  ``DEFERRABLE INITIALLY DEFERRED`` so a bulk copy (the SQLite →
  Postgres migration) can insert parents and children in one
  transaction without ordering games.
* ``aggregator_heartbeat`` is a Postgres-only single-row table the
  out-of-process aggregator worker updates after each sweep; the
  ``--health`` liveness probe reads it.

``apply_migrations`` serialises concurrent appliers with a
transaction-scoped advisory lock — two processes calling
``connect()`` at the same instant must not race ``CREATE TABLE``.

v1 = the full initial Postgres schema.
Future migrations append entries — they never edit v1.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # pragma: no cover
    import psycopg


def advisory_lock_key(name: str) -> int:
    """Derive a stable signed 64-bit advisory-lock key from a name.

    Python's builtin ``hash()`` is salted per process, so two
    processes would disagree on the key; sha256 gives every process
    (and every future release) the same value for the same name.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


_MIGRATIONS_LOCK_KEY = advisory_lock_key("inkfoot_migrations")


# Each migration: (version, description, statements). version is
# monotonically increasing; description is a 1-line human label for
# diagnostics; statements is a tuple of single SQL statements executed
# in order inside one transaction (psycopg sends parameterless
# statements one at a time — no fragile semicolon splitting).
_MIGRATIONS: Sequence[tuple[int, str, tuple[str, ...]]] = (
    (
        1,
        "initial schema: runs + events + event_contents + aggregator_heartbeat",
        (
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                task TEXT,
                agent_kind TEXT,
                parent_run_id TEXT REFERENCES runs (id)
                    DEFERRABLE INITIALLY DEFERRED,
                run_kind TEXT NOT NULL DEFAULT 'root',
                divergence_flag INTEGER,
                started_at BIGINT NOT NULL,
                ended_at BIGINT,
                status TEXT NOT NULL DEFAULT 'running',
                outcome TEXT,
                quality_score DOUBLE PRECISION,
                total_input_tokens BIGINT NOT NULL DEFAULT 0,
                total_output_tokens BIGINT NOT NULL DEFAULT 0,
                total_cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                total_cache_creation_tokens BIGINT NOT NULL DEFAULT 0,
                total_nanodollars BIGINT NOT NULL DEFAULT 0,
                aggregates_dirty INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs (id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL,
                occurred_at BIGINT NOT NULL,
                payload_json TEXT,
                sequence BIGINT NOT NULL,
                capture_mode TEXT NOT NULL DEFAULT 'metadata'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS event_contents (
                event_id TEXT PRIMARY KEY REFERENCES events (id)
                    ON DELETE CASCADE,
                request_json TEXT,
                response_json TEXT,
                tool_result_json TEXT,
                content_redacted INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS aggregator_heartbeat (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                last_sweep_at BIGINT NOT NULL,
                runs_swept BIGINT NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX IF NOT EXISTS events_run_seq"
            " ON events (run_id, sequence)",
            "CREATE INDEX IF NOT EXISTS runs_started"
            " ON runs (started_at DESC)",
            "CREATE INDEX IF NOT EXISTS runs_task_started"
            " ON runs (task, started_at DESC)",
            "CREATE INDEX IF NOT EXISTS runs_dirty"
            " ON runs (aggregates_dirty) WHERE aggregates_dirty = 1",
            "CREATE INDEX IF NOT EXISTS runs_parent"
            " ON runs (parent_run_id) WHERE parent_run_id IS NOT NULL",
        ),
    ),
)


_BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at BIGINT NOT NULL
)
"""


def apply_migrations(conn: "psycopg.Connection[Any]") -> list[int]:
    """Apply every pending migration in order; return the versions
    that were actually applied this call (empty list when up to date).

    The whole apply runs in one transaction holding a
    transaction-scoped advisory lock: concurrent appliers queue on
    the lock, and the late one sees the bookkeeping rows the first
    one committed — so re-applying is a no-op, never a ``CREATE``
    race. Postgres DDL is transactional, so a failure mid-list rolls
    back cleanly and the next ``connect()`` retries from the same
    point.
    """
    # Local import: psycopg is an optional dependency and this module
    # must stay importable without it (the unit tests assert on the
    # migration list shape).
    from psycopg.rows import tuple_row  # noqa: PLC0415

    newly_applied: list[int] = []
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_MIGRATIONS_LOCK_KEY,)
        )
        conn.execute(_BOOKKEEPING_DDL)
        # Pin a tuple row factory: the caller's connection may be
        # configured for dict rows (the storage pool is).
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute("SELECT version FROM applied_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for version, description, statements in _MIGRATIONS:
            if version in applied:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO applied_migrations (version, description,"
                " applied_at) VALUES (%s, %s,"
                " (extract(epoch from now()) * 1000)::bigint)",
                (version, description),
            )
            newly_applied.append(version)
    return newly_applied


def current_schema_version(conn: "psycopg.Connection[Any]") -> int:
    """Return the highest applied migration version, or 0 if none."""
    from psycopg.rows import tuple_row  # noqa: PLC0415

    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT to_regclass('applied_migrations') IS NOT NULL")
        row = cur.fetchone()
        if row is None or not row[0]:
            return 0
        cur.execute("SELECT MAX(version) FROM applied_migrations")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
