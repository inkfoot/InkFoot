"""Forward-only DDL list for the SQLite storage layer.

Each migration is an idempotent block of SQL executed in sequence on a
fresh database. ``applied_migrations`` records what's been run so
re-opening an existing database is a no-op.

v1 = the full initial SQLite schema.
Future migrations append entries — they never edit v1.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence


# Each migration: (version, description, sql). version is monotonically
# increasing; description is a 1-line human label for diagnostics; sql
# is the full DDL block executed inside a transaction.
_MIGRATIONS: Sequence[tuple[int, str, str]] = (
    (
        1,
        "initial schema: runs + events + event_contents (replay-mode storage contract)",
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task TEXT,
            agent_kind TEXT,
            parent_run_id TEXT,
            run_kind TEXT NOT NULL DEFAULT 'root',
            divergence_flag INTEGER,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            status TEXT NOT NULL DEFAULT 'running',
            outcome TEXT,
            quality_score REAL,
            total_input_tokens INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            total_nanodollars INTEGER NOT NULL DEFAULT 0,
            aggregates_dirty INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            FOREIGN KEY (parent_run_id) REFERENCES runs (id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            occurred_at INTEGER NOT NULL,
            payload_json TEXT,
            sequence INTEGER NOT NULL,
            capture_mode TEXT NOT NULL DEFAULT 'metadata',
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        );

        -- replay-mode storage contract: ship the sibling table now even though the current implementation
        -- never writes to it, so future Cloud code doesn't need a retroactive
        -- migration that strands earlier runs.
        CREATE TABLE IF NOT EXISTS event_contents (
            event_id TEXT PRIMARY KEY,
            request_json TEXT,
            response_json TEXT,
            tool_result_json TEXT,
            content_redacted INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS events_run_seq
            ON events (run_id, sequence);
        CREATE INDEX IF NOT EXISTS runs_started
            ON runs (started_at DESC);
        CREATE INDEX IF NOT EXISTS runs_task_started
            ON runs (task, started_at DESC);
        CREATE INDEX IF NOT EXISTS runs_dirty
            ON runs (aggregates_dirty) WHERE aggregates_dirty = 1;
        CREATE INDEX IF NOT EXISTS runs_parent
            ON runs (parent_run_id) WHERE parent_run_id IS NOT NULL;
        """,
    ),
)


_BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at INTEGER NOT NULL
);
"""


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order; return the versions
    that were actually applied this call (empty list when up to date).

    Migrations are applied each inside its own transaction. A failure
    mid-list rolls back only the failing migration; previously applied
    ones stay applied.
    """
    conn.executescript(_BOOKKEEPING_DDL)

    cur = conn.execute("SELECT version FROM applied_migrations")
    applied = {row[0] for row in cur.fetchall()}

    newly_applied: list[int] = []
    for version, description, sql in _MIGRATIONS:
        if version in applied:
            continue
        try:
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO applied_migrations (version, "
                    "description, applied_at) VALUES (?, ?, "
                    "strftime('%s','now') * 1000)",
                    (version, description),
                )
            newly_applied.append(version)
        except sqlite3.Error as exc:  # pragma: no cover — defensive
            raise RuntimeError(
                f"Migration v{version} ({description}) failed: {exc}"
            ) from exc
    return newly_applied


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    try:
        cur = conn.execute("SELECT MAX(version) FROM applied_migrations")
    except sqlite3.OperationalError:
        return 0
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0
