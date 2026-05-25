"""Aggregator worker — drains the dirty queue and recomputes
projection columns on ``runs`` from the event log.

ADR-0-1: ``runs.total_*`` and ``runs.outcome`` are *projections*, not
primary facts. The shim hot path writes only events + the dirty flag
(synchronous, under 1 ms). This worker catches up asynchronously.

Idempotence comes from two places:

1. Aggregating an already-clean row is a no-op (we ``SELECT`` the
   dirty queue and only touch rows where ``aggregates_dirty=1``).
2. The ``UPDATE ... WHERE id=? AND aggregates_dirty=1`` pattern in
   :meth:`SQLiteStorage.update_aggregates` is the lost-update guard:
   if a new event lands mid-sweep and re-dirties the row, our UPDATE
   matches nothing and the row stays dirty for the next pass.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.storage.sqlite import SQLiteStorage


_LOG = logging.getLogger("inkfoot.aggregator")


_DEFAULT_INTERVAL_MS = 500
_DEFAULT_BATCH = 50
_AGG_FIELDS_FROM_PAYLOAD = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "nanodollars",
)


def _interval_seconds() -> float:
    """Read ``INKFOOT_AGGREGATOR_INTERVAL_MS`` from env, falling back
    to 500 ms. Caller-friendly clamp: under 10 ms is almost certainly
    a typo (e.g. ``50`` meaning seconds), so we clamp up to 10 ms with
    a warning rather than burn a CPU."""
    raw = os.environ.get("INKFOOT_AGGREGATOR_INTERVAL_MS")
    if raw is None:
        return _DEFAULT_INTERVAL_MS / 1000.0
    try:
        ms = int(raw)
    except ValueError:
        _LOG.warning(
            "INKFOOT_AGGREGATOR_INTERVAL_MS=%r is not an integer; "
            "falling back to %d ms",
            raw,
            _DEFAULT_INTERVAL_MS,
        )
        return _DEFAULT_INTERVAL_MS / 1000.0
    if ms < 10:
        _LOG.warning(
            "INKFOOT_AGGREGATOR_INTERVAL_MS=%d is below the 10 ms floor; "
            "clamping",
            ms,
        )
        ms = 10
    return ms / 1000.0


def project_run_totals(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure projection: collapse a run's event log into the totals
    that live on ``runs``.

    Phase 0's ``runs`` schema holds five totals
    (``total_input_tokens``, ``total_output_tokens``,
    ``total_cache_read_tokens``, ``total_cache_creation_tokens``,
    ``total_nanodollars``) plus ``outcome``. Per-category ledger
    breakdown stays in event payloads — see E2.

    Robustness notes:
    - Missing payload fields default to 0.
    - Non-integer values in payload fields are skipped with a debug
      log (the shim's job is to emit ints; we don't silently coerce).
    - ``kind='outcome'`` event's ``payload_json.outcome`` takes
      precedence over any other claim; if multiple outcome events
      exist, the *last* by sequence wins.
    """
    totals = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_nanodollars": 0,
        "outcome": None,
    }
    for ev in events:
        payload_raw = ev.get("payload_json")
        if not payload_raw:
            payload: dict[str, Any] = {}
        else:
            try:
                payload = json.loads(payload_raw)
            except (TypeError, ValueError):
                _LOG.debug(
                    "event %s has unparseable payload; skipping aggregation",
                    ev.get("id"),
                )
                continue

        for src in _AGG_FIELDS_FROM_PAYLOAD:
            value = payload.get(src)
            if isinstance(value, bool) or not isinstance(value, int):
                # bool is a subclass of int — reject explicitly.
                continue
            dest = f"total_{src}" if src != "nanodollars" else "total_nanodollars"
            totals[dest] += value

        if ev.get("kind") == "outcome":
            claimed = payload.get("outcome")
            if isinstance(claimed, str):
                totals["outcome"] = claimed

    return totals


class AggregatorWorker:
    """Background thread that drains the dirty queue on a poll
    interval. Start with :meth:`start`; the thread exits cleanly when
    :meth:`stop` is called.

    Safe to instantiate multiple times in tests against in-memory
    DBs — each worker owns its own thread; stopping one doesn't
    affect another.
    """

    def __init__(
        self,
        storage: "SQLiteStorage",
        *,
        interval_seconds: float | None = None,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        if interval_seconds is None:
            interval_seconds = _interval_seconds()
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self._storage = storage
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="inkfoot-aggregator", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the worker and join its thread. Drains one final
        pass before exit so tests don't race on the last sweep."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        # Drain one final time so any events that arrived just before
        # ``stop`` are reflected.
        self.drain_once()

    # ------------------------------------------------------------------
    # Pumping
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.drain_once()
            except Exception:  # pragma: no cover — defensive
                _LOG.exception("aggregator drain failed; will retry")
            # Use ``wait`` rather than ``sleep`` so ``stop`` is
            # responsive.
            self._stop_event.wait(self._interval)

    def drain_once(self) -> int:
        """One full sweep of the dirty queue. Returns the number of
        runs aggregated. Exposed publicly so tests can drive the
        worker deterministically without spinning a thread."""
        processed = 0
        while True:
            run_ids = self._storage.read_dirty(limit=self._batch_size)
            if not run_ids:
                break
            for run_id in run_ids:
                if self._aggregate_one(run_id):
                    processed += 1
            if len(run_ids) < self._batch_size:
                # No more pending in this sweep; come back next tick.
                break
        return processed

    def _aggregate_one(self, run_id: str) -> bool:
        events = list(self._storage.iter_events(run_id))
        totals = project_run_totals(events)
        return self._storage.update_aggregates(run_id=run_id, totals=totals)
