"""SQLite-backed implementation of the Storage Protocol.

Honours ADR-0-5 (WAL + per-connection pragmas), ADR-0-1 (two-tier
write semantics), and ADR-0-9 (capture-mode column + sibling
``event_contents`` table).

Connections are per-thread via :class:`threading.local` — SQLite
connections aren't thread-safe and the aggregator runs on its own
thread alongside the shim hot path. Each thread that touches the
storage object lazily gets its own connection; pragmas are applied
on creation.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from inkfoot.storage.migrations import apply_migrations, current_schema_version


_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 134217728",  # 128 MiB
    "PRAGMA foreign_keys = ON",
)


def _default_db_path() -> Path:
    """Inkfoot's default SQLite location is ``~/.inkfoot/runs.db``."""
    home = Path(os.environ.get("INKFOOT_HOME", Path.home() / ".inkfoot"))
    return home / "runs.db"


class SQLiteStorage:
    """Default Phase 0 storage backend.

    Pass a ``path`` of ``":memory:"`` for in-process tests; the
    per-thread cache pins a single in-memory DB so multiple connections
    on different threads see the *same* database (SQLite ordinarily
    gives each connection an isolated in-memory DB).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = _default_db_path()
        self._path: Path | str
        if isinstance(path, Path):
            self._path = path
        elif path == ":memory:":
            self._path = path
        else:
            self._path = Path(path)
        self._local = threading.local()
        # ``_shared_memory_conn`` backs the ``:memory:`` case so all
        # threads see the same database. For file-backed DBs each
        # thread gets its own connection.
        self._shared_memory_conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        if isinstance(self._path, Path):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._path),
                isolation_level=None,  # we manage transactions manually
                check_same_thread=False,
            )
        else:  # ":memory:" branch
            conn = sqlite3.connect(
                ":memory:",
                isolation_level=None,
                check_same_thread=False,
            )
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            try:
                conn.execute(pragma)
            except sqlite3.OperationalError:
                # journal_mode=WAL fails harmlessly on ":memory:";
                # other pragmas are best-effort too.
                pass
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("SQLiteStorage is closed")
        if isinstance(self._path, str) and self._path == ":memory:":
            # All threads share one in-memory connection.
            if self._shared_memory_conn is None:
                with self._lock:
                    if self._shared_memory_conn is None:
                        self._shared_memory_conn = self._new_connection()
            return self._shared_memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Apply pending migrations on the current thread's connection.
        Idempotent."""
        conn = self._conn()
        apply_migrations(conn)

    def close(self) -> None:
        """Close every cached connection. Idempotent."""
        if self._closed:
            return
        if self._shared_memory_conn is not None:
            try:
                self._shared_memory_conn.close()
            except sqlite3.Error:
                pass
            self._shared_memory_conn = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        self._closed = True

    def schema_version(self) -> int:
        return current_schema_version(self._conn())

    # ------------------------------------------------------------------
    # Run lifecycle (ADR-0-1 — synchronous status writes)
    # ------------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        task: Optional[str],
        agent_kind: Optional[str],
        started_at: int,
        parent_run_id: Optional[str] = None,
        run_kind: str = "root",
        metadata_json: Optional[str] = None,
    ) -> None:
        conn = self._conn()
        with conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO runs (
                    id, task, agent_kind, parent_run_id, run_kind,
                    started_at, status, aggregates_dirty, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', 0, ?)
                """,
                (
                    run_id,
                    task,
                    agent_kind,
                    parent_run_id,
                    run_kind,
                    started_at,
                    metadata_json,
                ),
            )

    def end_run(
        self,
        *,
        run_id: str,
        ended_at: int,
        status: str,
    ) -> None:
        if status not in {"complete", "error"}:
            raise ValueError(
                f"end_run status must be 'complete' or 'error', not {status!r}"
            )
        conn = self._conn()
        with conn:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE runs SET ended_at = ?, status = ? WHERE id = ?",
                (ended_at, status, run_id),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        cur = self._conn().execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Event write — the hot path
    # ------------------------------------------------------------------

    def insert_event(
        self,
        *,
        event_id: str,
        run_id: str,
        kind: str,
        occurred_at: int,
        sequence: int,
        payload_json: Optional[str] = None,
        capture_mode: str = "metadata",
    ) -> None:
        """Append an event row + set the parent run's ``aggregates_dirty``
        flag in a single transaction. Must complete under 1 ms p95 in
        WAL mode (§9.1 perf budget)."""
        if capture_mode not in {"metadata", "replay"}:
            raise ValueError(
                f"capture_mode must be 'metadata' or 'replay', not "
                f"{capture_mode!r}"
            )
        conn = self._conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO events (
                    id, run_id, kind, occurred_at, payload_json,
                    sequence, capture_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    kind,
                    occurred_at,
                    payload_json,
                    sequence,
                    capture_mode,
                ),
            )
            conn.execute(
                "UPDATE runs SET aggregates_dirty = 1 WHERE id = ?",
                (run_id,),
            )

    # ------------------------------------------------------------------
    # Dirty queue + aggregator interactions
    # ------------------------------------------------------------------

    def mark_dirty(self, run_id: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE runs SET aggregates_dirty = 1 WHERE id = ?",
                (run_id,),
            )

    def mark_all_dirty(self) -> int:
        """Set ``aggregates_dirty=1`` on every run. Used by
        ``inkfoot rebuild-aggregates``. Returns the number of rows
        marked."""
        conn = self._conn()
        with conn:
            conn.execute("BEGIN")
            cur = conn.execute("UPDATE runs SET aggregates_dirty = 1")
            return cur.rowcount

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        cur = self._conn().execute(
            """
            SELECT id FROM runs
            WHERE aggregates_dirty = 1
            ORDER BY started_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [row[0] for row in cur.fetchall()]

    def update_aggregates(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> bool:
        """Update projection columns *iff* the row is still dirty.
        The conditional ``WHERE id=? AND aggregates_dirty=1`` guard is
        what makes the aggregator safe under concurrent inserts —
        if a new event lands mid-sweep and re-dirties the row, the
        aggregator's UPDATE matches nothing and the row stays dirty
        for the next pass (no lost update)."""
        allowed = {
            "total_input_tokens",
            "total_output_tokens",
            "total_cache_read_tokens",
            "total_cache_creation_tokens",
            "total_nanodollars",
            "outcome",
            "quality_score",
        }
        unknown = set(totals) - allowed
        if unknown:
            raise ValueError(f"update_aggregates rejects unknown keys: {unknown}")
        if not totals:
            raise ValueError("update_aggregates requires at least one total")

        assignments = ", ".join(f"{k} = ?" for k in totals)
        params = list(totals.values()) + [run_id]
        sql = (
            f"UPDATE runs SET {assignments}, aggregates_dirty = 0 "
            f"WHERE id = ? AND aggregates_dirty = 1"
        )
        conn = self._conn()
        with conn:
            conn.execute("BEGIN")
            cur = conn.execute(sql, params)
            return cur.rowcount > 0

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        cur = self._conn().execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        )
        for row in cur:
            yield dict(row)
