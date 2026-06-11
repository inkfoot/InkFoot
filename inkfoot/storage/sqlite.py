"""SQLite-backed implementation of the Storage Protocol.

Guarantees: WAL journaling + per-connection pragmas; two-tier write
semantics with the claim-and-project guarantee against lost updates;
and the replay-mode content contract (capture-mode column + sibling
``event_contents`` table).

Connections are per-thread via :class:`threading.local` — SQLite
connections aren't thread-safe and the aggregator runs on its own
thread alongside the shim hot path. Each thread that touches the
storage object lazily gets its own connection; pragmas are applied on
creation. ``close()`` tears down *every* connection created across
every thread, not just the caller's.

For ``":memory:"`` the storage falls back to a single shared
connection guarded by a process-level lock — the in-memory case
exists primarily for tests, where deterministic visibility across
threads matters more than per-thread isolation.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from inkfoot.storage.migrations import apply_migrations, current_schema_version


_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 134217728",  # 128 MiB
    "PRAGMA foreign_keys = ON",
)


# Subset of ``runs.*`` columns the projection layer is allowed to set.
# Centralised so :meth:`write_totals` and the legacy
# :meth:`update_aggregates` share one allow-list.
_PROJECTION_COLUMNS = frozenset(
    {
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_cache_creation_tokens",
        "total_nanodollars",
        "outcome",
        "quality_score",
    }
)


def _default_db_path() -> Path:
    """Inkfoot's default SQLite location is ``~/.inkfoot/runs.db``."""
    home = Path(os.environ.get("INKFOOT_HOME", Path.home() / ".inkfoot"))
    return home / "runs.db"


class SQLiteStorage:
    """Default SQLite storage backend.

    Pass a ``path`` of ``":memory:"`` for in-process tests; the
    shared in-memory connection lets multiple threads see the *same*
    database (SQLite ordinarily gives each connection an isolated
    in-memory DB).
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
        self._is_memory = isinstance(self._path, str) and self._path == ":memory:"
        self._local = threading.local()
        # Backs the ``:memory:`` case so all threads see the same DB.
        # For file-backed DBs we instead populate ``_connections``
        # with one entry per thread for close()-time cleanup.
        self._shared_memory_conn: Optional[sqlite3.Connection] = None
        self._connections: list[sqlite3.Connection] = []
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
        if self._is_memory:
            # All threads share one in-memory connection.
            if self._shared_memory_conn is None:
                with self._lock:
                    if self._shared_memory_conn is None:
                        self._shared_memory_conn = self._new_connection()
                        # Tracked for close()-time cleanup.
                        self._connections.append(self._shared_memory_conn)
            return self._shared_memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
            # Register cross-thread so close() can iterate and clean
            # up connections created on threads that have since exited.
            with self._lock:
                self._connections.append(conn)
        return conn

    @contextmanager
    def _locked_conn(self) -> Iterator[sqlite3.Connection]:
        """Yield this thread's connection, holding ``self._lock`` for
        the duration of the call iff the storage is in shared-memory
        mode. File-backed storage uses per-thread connections so no
        lock is needed."""
        conn = self._conn()
        if self._is_memory:
            with self._lock:
                yield conn
        else:
            yield conn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Apply pending migrations on the current thread's connection.
        Idempotent."""
        conn = self._conn()
        if self._is_memory:
            with self._lock:
                apply_migrations(conn)
        else:
            apply_migrations(conn)

    def close(self) -> None:
        """Close every cached connection, including connections opened
        on threads other than the caller's. Idempotent."""
        if self._closed:
            return
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
            self._shared_memory_conn = None
        # Clear this thread's local reference so a future _conn() on
        # a re-opened storage doesn't reuse the closed handle.
        if getattr(self._local, "conn", None) is not None:
            self._local.conn = None
        self._closed = True

    def schema_version(self) -> int:
        return current_schema_version(self._conn())

    # ------------------------------------------------------------------
    # Run lifecycle (synchronous status writes)
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
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

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
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, status = ? WHERE id = ?",
                    (ended_at, status, run_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._locked_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            )
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
        request_json: Optional[str] = None,
        response_json: Optional[str] = None,
        tool_result_json: Optional[str] = None,
        content_redacted: bool = False,
    ) -> None:
        """Append an event row + set the parent run's
        ``aggregates_dirty`` flag in a single transaction. Must
        complete under 1 ms p95 in WAL mode (the shim perf budget).

        Replay-mode content write: when
        ``capture_mode='replay'``, an ``event_contents`` row is
        written *in the same transaction* with the serialised
        request/response/tool-result bodies. When
        ``capture_mode='metadata'`` (default), the content kwargs
        are silently ignored — the row is suppressed here at the
        storage layer rather than in the shim, so both shims share
        one write path.
        """
        if capture_mode not in {"metadata", "replay"}:
            raise ValueError(
                f"capture_mode must be 'metadata' or 'replay', not "
                f"{capture_mode!r}"
            )
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
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
                # Replay-mode content row:
                # we only write when there's actual content to record.
                # The shim's policy-event branch doesn't carry content
                # — writing a row of all-NULLs would pollute the
                # event_contents table and confuse readers who join
                # to it expecting "this is a replayable LLM call".
                has_content = (
                    request_json is not None
                    or response_json is not None
                    or tool_result_json is not None
                )
                if capture_mode == "replay" and has_content:
                    conn.execute(
                        """
                        INSERT INTO event_contents (
                            event_id, request_json, response_json,
                            tool_result_json, content_redacted
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            request_json,
                            response_json,
                            tool_result_json,
                            1 if content_redacted else 0,
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Dirty queue + claim-and-project (the lost-update fix)
    # ------------------------------------------------------------------

    def mark_dirty(self, run_id: str) -> None:
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE runs SET aggregates_dirty = 1 WHERE id = ?",
                    (run_id,),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def mark_all_dirty(self) -> int:
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("UPDATE runs SET aggregates_dirty = 1")
                count = cur.rowcount
                conn.execute("COMMIT")
                return count
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        with self._locked_conn() as conn:
            cur = conn.execute(
                """
                SELECT id FROM runs
                WHERE aggregates_dirty = 1
                ORDER BY started_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]

    def claim_clean(self, run_id: str) -> bool:
        """Atomically clear the dirty flag for ``run_id``. Returns
        ``True`` if the row *was* dirty (and we therefore own the
        responsibility to project its events into the totals).
        Returns ``False`` if the row was already clean — another
        worker beat us, or there's nothing to do.

        The claim-and-project pattern this enables:

        1. ``claim_clean(run_id)`` — atomic CAS, dirty 1 → 0.
        2. ``iter_events(run_id)`` — read the log.
        3. ``write_totals(run_id, totals)`` — unconditional UPDATE.

        Any :meth:`insert_event` that lands between (1) and (3) flips
        dirty back to 1 and the next aggregator pass picks the row
        up. The event log is the source of truth; the projection is
        recomputable. Lost updates are impossible by construction.
        """
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE runs SET aggregates_dirty = 0 "
                    "WHERE id = ? AND aggregates_dirty = 1",
                    (run_id,),
                )
                claimed = cur.rowcount > 0
                conn.execute("COMMIT")
                return claimed
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def write_totals(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> None:
        """Unconditionally write the projection columns for ``run_id``.

        Does *not* touch :sql:`aggregates_dirty`. The caller is
        expected to have called :meth:`claim_clean` first; any new
        events landing after this call will re-dirty the row via
        :meth:`insert_event` and the next aggregator pass picks them
        up.
        """
        unknown = set(totals) - _PROJECTION_COLUMNS
        if unknown:
            raise ValueError(f"write_totals rejects unknown keys: {unknown}")
        if not totals:
            raise ValueError("write_totals requires at least one total")

        assignments = ", ".join(f"{k} = ?" for k in totals)
        params = list(totals.values()) + [run_id]
        with self._locked_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"UPDATE runs SET {assignments} WHERE id = ?",
                    params,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def update_aggregates(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> bool:
        """Composite claim-and-project: equivalent to
        :meth:`claim_clean` followed by :meth:`write_totals` in one
        call. Kept on the Protocol surface as part of
        the storage contract. Returns ``True`` if the row was dirty (and the
        totals were written), ``False`` otherwise.

        Prefer the explicit :meth:`claim_clean` + read events +
        :meth:`write_totals` sequence in code that does the projection
        itself, so the read happens *between* the claim and the write
        and never-lost-update is enforced by construction. This
        composite is for callers that already have totals on hand.
        """
        unknown = set(totals) - _PROJECTION_COLUMNS
        if unknown:
            raise ValueError(f"update_aggregates rejects unknown keys: {unknown}")
        if not totals:
            raise ValueError("update_aggregates requires at least one total")

        if not self.claim_clean(run_id):
            return False
        self.write_totals(run_id=run_id, totals=totals)
        return True

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        with self._locked_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield dict(row)
