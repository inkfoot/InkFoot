"""Postgres-backed implementation of the Storage Protocol.

Same contract as the SQLite backend — two-tier writes, the
claim-and-project lost-update guarantee, and the replay-mode content
contract — on a server multiple processes can share. Connections come
from a thread-safe :class:`psycopg_pool.ConnectionPool`; every method
checks out a connection for exactly one transaction, so the storage
object is safe to call from the shim hot path and worker threads
alike.

Differences from SQLite worth knowing:

* **Aggregation is out of process.** ``external_aggregator = True``
  tells ``inkfoot.instrument()`` not to start the in-process
  aggregator thread; a multi-process deployment runs one
  ``inkfoot aggregator-worker`` next to the app instead, coordinated
  through a Postgres advisory lock (see
  :mod:`inkfoot.storage.postgres_aggregator`).
* **Optional dependency.** The ``psycopg`` driver ships in the
  ``postgres`` extra; constructing the class is dependency-free, and
  :meth:`connect` raises :class:`~inkfoot.errors.StorageError` with
  an install hint when the driver is missing.

Pool sizing is tunable via ``INKFOOT_PG_POOL_MIN`` /
``INKFOOT_PG_POOL_MAX`` (defaults 1 / 4); constructor arguments win
over the environment.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Optional

from inkfoot.errors import StorageError
from inkfoot.storage import PROJECTION_COLUMNS
from inkfoot.storage.postgres_migrations import (
    apply_migrations,
    current_schema_version,
)

if TYPE_CHECKING:  # pragma: no cover
    import psycopg
    import psycopg_pool

    from inkfoot.storage.redaction import RedactionHook


_LOG = logging.getLogger("inkfoot.storage.postgres")

_DEFAULT_POOL_MIN = 1
_DEFAULT_POOL_MAX = 4
_DSN_ENV = "INKFOOT_PG_DSN"
_POOL_MIN_ENV = "INKFOOT_PG_POOL_MIN"
_POOL_MAX_ENV = "INKFOOT_PG_POOL_MAX"

_INSTALL_HINT = (
    "PostgresStorage requires the optional Postgres driver — install "
    "it with: pip install 'inkfoot[postgres]'"
)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, warning and falling back on garbage
    (matching how the aggregator treats its interval env var)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOG.warning(
            "%s=%r is not an integer; falling back to %d",
            name,
            raw,
            default,
        )
        return default


def _resolve_pool_sizes(
    pool_min: Optional[int], pool_max: Optional[int]
) -> tuple[int, int]:
    """Constructor args win over env vars; explicit args are
    validated strictly, env values are sanitised leniently so a typo
    in a deployment manifest degrades to a warning, not a crash."""
    if pool_min is not None and pool_min < 1:
        raise ValueError(f"pool_min must be >= 1, got {pool_min}")
    if pool_max is not None and pool_max < 1:
        raise ValueError(f"pool_max must be >= 1, got {pool_max}")
    if (
        pool_min is not None
        and pool_max is not None
        and pool_min > pool_max
    ):
        raise ValueError(
            f"pool_min ({pool_min}) must be <= pool_max ({pool_max})"
        )

    resolved_min = (
        pool_min
        if pool_min is not None
        else max(1, _env_int(_POOL_MIN_ENV, _DEFAULT_POOL_MIN))
    )
    resolved_max = (
        pool_max
        if pool_max is not None
        else max(1, _env_int(_POOL_MAX_ENV, _DEFAULT_POOL_MAX))
    )
    if resolved_min > resolved_max:
        _LOG.warning(
            "pool min %d exceeds max %d; raising max to match",
            resolved_min,
            resolved_max,
        )
        resolved_max = resolved_min
    return resolved_min, resolved_max


class PostgresStorage:
    """Postgres storage backend.

    ``dsn`` is any libpq connection string or URL
    (``postgresql://user:pass@host:5432/dbname``); when omitted it is
    read from ``INKFOOT_PG_DSN``.
    """

    # Read by ``inkfoot.instrument()``: aggregation for this backend
    # happens in a separate ``inkfoot aggregator-worker`` process, so
    # the in-process aggregator thread must not start.
    external_aggregator = True

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        pool_min: Optional[int] = None,
        pool_max: Optional[int] = None,
        connect_timeout: float = 30.0,
        redaction_hook: Optional["RedactionHook"] = None,
    ) -> None:
        if dsn is None:
            dsn = os.environ.get(_DSN_ENV)
        if not dsn:
            raise ValueError(
                "PostgresStorage needs a DSN — pass dsn= or set "
                f"{_DSN_ENV}"
            )
        if connect_timeout <= 0:
            raise ValueError(
                f"connect_timeout must be > 0, got {connect_timeout}"
            )
        self._dsn = dsn
        self._pool_min, self._pool_max = _resolve_pool_sizes(
            pool_min, pool_max
        )
        self._connect_timeout = connect_timeout
        self._pool: Optional["psycopg_pool.ConnectionPool[Any]"] = None
        self._lock = threading.Lock()
        self._closed = False
        # Optional redaction hook run over replay content before it is
        # written to ``event_contents`` (see :meth:`set_redaction_hook`).
        self._redaction_hook: Optional["RedactionHook"] = redaction_hook

    @property
    def dsn(self) -> str:
        return self._dsn

    def set_redaction_hook(
        self, hook: Optional["RedactionHook"]
    ) -> None:
        """Install (or clear) the redaction hook applied to replay
        content. ``inkfoot.instrument()`` calls this when replay capture
        is enabled."""
        self._redaction_hook = hook

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the connection pool and apply pending migrations.
        Idempotent — a second call re-checks migrations (a no-op when
        up to date) on the already-open pool."""
        if self._closed:
            raise RuntimeError("PostgresStorage is closed")
        try:
            from psycopg.rows import dict_row  # noqa: PLC0415
            from psycopg_pool import ConnectionPool  # noqa: PLC0415
        except ImportError as exc:
            raise StorageError(_INSTALL_HINT) from exc

        with self._lock:
            if self._pool is None:
                pool = ConnectionPool(
                    conninfo=self._dsn,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    kwargs={"row_factory": dict_row},
                    open=False,
                    name="inkfoot-pg",
                )
                try:
                    pool.open(wait=True, timeout=self._connect_timeout)
                except Exception as exc:
                    pool.close()
                    raise StorageError(
                        f"could not connect to Postgres at the "
                        f"configured DSN: {exc}"
                    ) from exc
                self._pool = pool
        with self._connection() as conn:
            apply_migrations(conn)

    def close(self) -> None:
        """Close the pool. Idempotent."""
        if self._closed:
            return
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            self._closed = True

    @contextmanager
    def _connection(self) -> Iterator["psycopg.Connection[Any]"]:
        """Check a connection out of the pool for one transaction.
        Commits on clean exit, rolls back on exception (the pool's
        context-manager contract)."""
        if self._closed:
            raise RuntimeError("PostgresStorage is closed")
        if self._pool is None:
            raise RuntimeError(
                "PostgresStorage is not connected — call connect() first"
            )
        with self._pool.connection() as conn:
            yield conn

    def schema_version(self) -> int:
        with self._connection() as conn:
            return current_schema_version(conn)

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
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, task, agent_kind, parent_run_id, run_kind,
                    started_at, status, aggregates_dirty, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, 'running', 0, %s)
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
        with self._connection() as conn:
            conn.execute(
                "UPDATE runs SET ended_at = %s, status = %s WHERE id = %s",
                (ended_at, status, run_id),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT * FROM runs WHERE id = %s", (run_id,)
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
        ``aggregates_dirty`` flag in a single transaction. The
        replay-mode content contract matches SQLite: a sibling
        ``event_contents`` row is written only when
        ``capture_mode='replay'`` *and* some content kwarg is
        non-None. An installed redaction hook runs over the content
        before it is written; ``content_redacted`` records whether it
        changed anything."""
        if capture_mode not in {"metadata", "replay"}:
            raise ValueError(
                f"capture_mode must be 'metadata' or 'replay', not "
                f"{capture_mode!r}"
            )
        has_content = (
            request_json is not None
            or response_json is not None
            or tool_result_json is not None
        )
        if (
            capture_mode == "replay"
            and has_content
            and self._redaction_hook is not None
        ):
            from inkfoot.storage.redaction import (  # noqa: PLC0415
                RedactionContext,
                apply_to_content,
            )

            (
                request_json,
                response_json,
                tool_result_json,
                content_redacted,
            ) = apply_to_content(
                self._redaction_hook,
                ctx=RedactionContext(
                    run_id=run_id,
                    event_id=event_id,
                    kind=kind,
                    capture_mode=capture_mode,
                    sequence=sequence,
                ),
                request_json=request_json,
                response_json=response_json,
                tool_result_json=tool_result_json,
            )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    id, run_id, kind, occurred_at, payload_json,
                    sequence, capture_mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                "UPDATE runs SET aggregates_dirty = 1 WHERE id = %s",
                (run_id,),
            )
            if capture_mode == "replay" and has_content:
                conn.execute(
                    """
                    INSERT INTO event_contents (
                        event_id, request_json, response_json,
                        tool_result_json, content_redacted
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        event_id,
                        request_json,
                        response_json,
                        tool_result_json,
                        1 if content_redacted else 0,
                    ),
                )

    # ------------------------------------------------------------------
    # Dirty queue + claim-and-project (the lost-update fix)
    # ------------------------------------------------------------------

    def mark_dirty(self, run_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE runs SET aggregates_dirty = 1 WHERE id = %s",
                (run_id,),
            )

    def mark_all_dirty(self) -> int:
        with self._connection() as conn:
            cur = conn.execute("UPDATE runs SET aggregates_dirty = 1")
            return cur.rowcount

    def read_dirty(self, *, limit: int = 50) -> list[str]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        with self._connection() as conn:
            cur = conn.execute(
                """
                SELECT id FROM runs
                WHERE aggregates_dirty = 1
                ORDER BY started_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [row["id"] for row in cur.fetchall()]

    def claim_clean(self, run_id: str) -> bool:
        """Atomic CAS — see the SQLite implementation for the full
        claim-and-project explanation; the guarantee is identical
        here because the UPDATE's row lock serialises concurrent
        claimers."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE runs SET aggregates_dirty = 0 "
                "WHERE id = %s AND aggregates_dirty = 1",
                (run_id,),
            )
            return cur.rowcount > 0

    def write_totals(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> None:
        unknown = set(totals) - PROJECTION_COLUMNS
        if unknown:
            raise ValueError(f"write_totals rejects unknown keys: {unknown}")
        if not totals:
            raise ValueError("write_totals requires at least one total")

        # Column names come from the allow-list above, never from the
        # caller's strings directly — safe to interpolate.
        assignments = ", ".join(f"{k} = %s" for k in totals)
        params = list(totals.values()) + [run_id]
        with self._connection() as conn:
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = %s",
                params,
            )

    def update_aggregates(
        self,
        *,
        run_id: str,
        totals: dict[str, Any],
    ) -> bool:
        unknown = set(totals) - PROJECTION_COLUMNS
        if unknown:
            raise ValueError(
                f"update_aggregates rejects unknown keys: {unknown}"
            )
        if not totals:
            raise ValueError("update_aggregates requires at least one total")

        if not self.claim_clean(run_id):
            return False
        self.write_totals(run_id=run_id, totals=totals)
        return True

    def iter_events(self, run_id: str) -> Iterable[dict[str, Any]]:
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT * FROM events WHERE run_id = %s "
                "ORDER BY sequence ASC",
                (run_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield dict(row)

    def find_runs_with_status(self, status: str) -> list[str]:
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT id FROM runs WHERE status = %s", (status,)
            )
            return [row["id"] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Aggregator heartbeat (Postgres-only operational surface)
    # ------------------------------------------------------------------

    def write_heartbeat(self, *, swept_at: int, runs_swept: int) -> None:
        """Upsert the single ``aggregator_heartbeat`` row. The
        aggregator worker calls this after every sweep; the
        ``--health`` probe reads it back."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO aggregator_heartbeat (
                    id, last_sweep_at, runs_swept
                ) VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    last_sweep_at = EXCLUDED.last_sweep_at,
                    runs_swept = EXCLUDED.runs_swept
                """,
                (swept_at, runs_swept),
            )

    def read_heartbeat(self) -> Optional[dict[str, int]]:
        """The last sweep's timestamp (epoch ms) and processed-run
        count, or ``None`` when no sweep has ever completed."""
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT last_sweep_at, runs_swept "
                "FROM aggregator_heartbeat WHERE id = 1"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "last_sweep_at": int(row["last_sweep_at"]),
                "runs_swept": int(row["runs_swept"]),
            }
