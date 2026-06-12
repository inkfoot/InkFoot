"""Cross-process aggregation coordinator for the Postgres backend.

With SQLite the aggregator is an in-process daemon thread — one
process, one database file, no coordination problem. Postgres turns
the database into shared infrastructure: many agent processes write
events concurrently, and if each of them also ran the in-process
worker they would contend on the same dirty queue. The claim-CAS in
``claim_clean`` keeps that *correct* (no lost updates, no double
projection) but it is wasteful — N workers all polling and N-1 of
them losing every claim.

So aggregation moves out of process. The Postgres backend sets
``external_aggregator = True`` which tells ``inkfoot.instrument()``
not to start the in-process thread, and one (or more, for
fail-over) dedicated ``inkfoot aggregator-worker`` processes run
:class:`PostgresAggregator` instead.

Coordination is a Postgres session-level advisory lock:

* Before each sweep the worker acquires
  ``pg_advisory_lock(advisory_lock_key('inkfoot_aggregator'))`` on a
  **dedicated** connection (never a pooled one — the lock's lifetime
  must be tied to this worker's session and nothing else).
* The lock is held for the duration of one sweep and released
  afterwards, so multiple healthy workers take turns rather than
  serialising forever behind whoever started first.
* Session-level semantics give crash fail-over for free: if the
  active worker is SIGKILLed, Postgres drops its session and
  releases the lock, and a standby worker's next acquire attempt
  succeeds — takeover within the standby's poll interval (100 ms by
  default), no operator action.

Acquisition uses ``pg_try_advisory_lock`` in a short poll loop
rather than the blocking ``pg_advisory_lock``: a blocking server-side
wait would leave the Python process stuck in a libpq call, unable to
notice a stop request. Polling keeps shutdown responsive and still
meets sub-second takeover.

After every sweep the worker upserts the single-row
``aggregator_heartbeat`` table (last sweep timestamp + runs swept).
``inkfoot aggregator-worker --health`` reads it back, which gives
deployments a liveness probe that works from any host that can
reach the database.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from inkfoot.storage.aggregator import (
    _DEFAULT_BATCH,
    AggregatorWorker,
    _interval_seconds,
)
from inkfoot.storage.postgres_migrations import advisory_lock_key

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

    from inkfoot.storage.postgres import PostgresStorage


_LOG = logging.getLogger("inkfoot.aggregator.postgres")


# Cluster-wide lock identity. Derived from a stable hash so every
# worker process — and every future release — computes the same key.
AGGREGATOR_LOCK_NAME = "inkfoot_aggregator"
_AGGREGATOR_LOCK_KEY = advisory_lock_key(AGGREGATOR_LOCK_NAME)

# How often a standby worker re-tries the advisory lock while
# another worker holds it. Bounds fail-over time: a standby notices
# a dead leader within one poll interval.
_DEFAULT_LOCK_POLL_SECONDS = 0.1


def _now_ms() -> int:
    return int(time.time() * 1000)


class PostgresAggregator:
    """Advisory-lock-coordinated aggregation sweeps against Postgres.

    Reuses :class:`AggregatorWorker`'s ``drain_once`` for the actual
    claim-and-project work (the algorithm is backend-agnostic — it
    only speaks the Storage Protocol); this class adds the
    cross-process mutual exclusion and the heartbeat.

    Not thread-safe: one instance belongs to one worker loop. The
    only cross-thread entry point is :meth:`stop`, which is safe to
    call from a signal handler.
    """

    def __init__(
        self,
        storage: "PostgresStorage",
        *,
        interval_seconds: Optional[float] = None,
        batch_size: int = _DEFAULT_BATCH,
        lock_poll_seconds: float = _DEFAULT_LOCK_POLL_SECONDS,
    ) -> None:
        if interval_seconds is None:
            interval_seconds = _interval_seconds()
        if lock_poll_seconds <= 0:
            raise ValueError(
                f"lock_poll_seconds must be > 0, got {lock_poll_seconds}"
            )
        self._storage = storage
        self._interval = interval_seconds
        self._lock_poll = lock_poll_seconds
        # The drain engine. Never .start()ed — we only borrow its
        # synchronous drain_once, exactly like `inkfoot
        # rebuild-aggregates` does.
        self._worker = AggregatorWorker(
            storage, interval_seconds=interval_seconds, batch_size=batch_size
        )
        self._stop = threading.Event()
        self._lock_conn: Optional["psycopg.Connection[Any]"] = None

    # ------------------------------------------------------------------
    # Lock-connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the dedicated advisory-lock session. Idempotent."""
        if self._lock_conn is not None and not self._lock_conn.closed:
            return
        import psycopg  # noqa: PLC0415

        # autocommit so lock calls execute as standalone statements —
        # session-level advisory locks outlive transactions anyway,
        # and autocommit means an error never leaves the session
        # stuck in an aborted transaction.
        self._lock_conn = psycopg.connect(
            self._storage.dsn, autocommit=True
        )

    def close(self) -> None:
        """Close the lock session, releasing any held lock. Idempotent."""
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # pragma: no cover — defensive
                _LOG.warning("lock-connection close raised", exc_info=True)

    def stop(self) -> None:
        """Request the worker loop to exit. Signal-handler safe."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Lock primitives (dedicated session)
    # ------------------------------------------------------------------

    def _try_lock(self) -> bool:
        if self._lock_conn is None or self._lock_conn.closed:
            raise RuntimeError(
                "PostgresAggregator is not connected — call connect() first"
            )
        # Default tuple row factory on the dedicated connection, so
        # plain positional indexing is correct here.
        cur = self._lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (_AGGREGATOR_LOCK_KEY,)
        )
        row = cur.fetchone()
        return bool(row and row[0])

    def _unlock(self) -> None:
        if self._lock_conn is None or self._lock_conn.closed:
            # Session already gone — Postgres released the lock with it.
            return
        try:
            self._lock_conn.execute(
                "SELECT pg_advisory_unlock(%s)", (_AGGREGATOR_LOCK_KEY,)
            )
        except Exception:  # pragma: no cover — defensive
            _LOG.warning("advisory unlock raised", exc_info=True)

    # ------------------------------------------------------------------
    # Sweeping
    # ------------------------------------------------------------------

    def sweep_once(self, *, timeout: Optional[float] = None) -> Optional[int]:
        """Acquire the cluster-wide lock, drain the dirty queue once,
        write the heartbeat, release the lock.

        Returns the number of runs projected, or ``None`` when the
        lock could not be acquired before ``timeout`` elapsed (or
        :meth:`stop` was requested while waiting). ``timeout=None``
        waits indefinitely — which is what a standby worker wants.
        """
        deadline = (
            None if timeout is None else time.monotonic() + timeout
        )
        while not self._try_lock():
            if self._stop.is_set():
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            self._stop.wait(self._lock_poll)
            if self._stop.is_set():
                return None
        try:
            processed = self._worker.drain_once()
            try:
                self._storage.write_heartbeat(
                    swept_at=_now_ms(), runs_swept=processed
                )
            except Exception:  # pragma: no cover — defensive
                # A missed heartbeat must not fail the sweep: totals
                # are already projected; the next sweep refreshes it.
                _LOG.warning("heartbeat write raised", exc_info=True)
            return processed
        finally:
            self._unlock()

    def run_forever(self) -> None:
        """Sweep on every interval tick until :meth:`stop` is called.

        Transient database failures (server restart, network blip)
        are logged and retried: the dedicated lock session is
        re-established on the next tick.
        """
        while not self._stop.is_set():
            try:
                self.connect()
                self.sweep_once()
            except Exception:  # pylint: disable=broad-except
                _LOG.exception(
                    "aggregator sweep failed; reconnecting on next tick"
                )
                self.close()
            self._stop.wait(self._interval)
