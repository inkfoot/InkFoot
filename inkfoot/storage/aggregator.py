"""Aggregator worker — drains the dirty queue and recomputes
projection columns on ``runs`` from the event log.

Two-tier write contract: ``runs.total_*`` and ``runs.outcome`` are
*projections*, not primary facts. The shim hot path writes only events + the dirty flag
(synchronous, under 1 ms). This worker catches up asynchronously.

**The claim-and-project pattern.** Each row is aggregated by:

1. :meth:`SQLiteStorage.claim_clean` — atomic CAS, dirty 1 → 0.
2. :meth:`SQLiteStorage.iter_events` — read the log.
3. :meth:`SQLiteStorage.write_totals` — unconditional UPDATE of
   the projection columns.

The crucial property is that the claim happens *before* the read. If
a new :meth:`SQLiteStorage.insert_event` lands between (1) and (3),
it flips ``aggregates_dirty`` back to 1 and the next aggregator pass
picks the row up. Events are never lost; totals may be momentarily
behind by one pass; the next pass always converges. The event log is
the source of truth — the projection is recomputable.

Pre-fix history: an earlier draft used a single
``UPDATE ... SET dirty=0 WHERE id=? AND dirty=1`` with totals
computed from a snapshot read taken *before* the WHERE-clause check,
which had a race where a late insert could leave the projection
permanently stale. The fix is structural — claim_clean / iter /
write_totals — not a tighter SQL predicate.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Mapping

from inkfoot.ledger import ledger_from_payload

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.storage import Storage


_LOG = logging.getLogger("inkfoot.aggregator")


_DEFAULT_INTERVAL_MS = 500
_DEFAULT_BATCH = 50


def _coerce_int(value: Any) -> int:
    """Token/cost fields must be plain ints. ``bool`` is an ``int``
    subclass but never a real count, so reject it; anything else
    non-int counts as 0 rather than corrupting a total."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _billed_from_payload(payload: Mapping[str, Any]) -> dict[str, int]:
    """Pull one ``llm_call`` payload's run-level token + cost aggregates.

    ``emit_llm_call`` serialises a :class:`~inkfoot.normalise.NeutralCall`
    with ``dataclasses.asdict``, so the production payload nests the token
    counts under ``ledger`` — input is the sum of the structural
    categories, cache fields are billing overlays — and carries the cost
    as the top-level ``estimated_nanodollars``. Read that shape via the
    shared :func:`~inkfoot.ledger.ledger_from_payload` reader (so the
    ledger field names stay tied to the dataclass); fall back to flat
    top-level fields (and the ``nanodollars`` cost alias) for events
    written before the causal ledger landed.
    """
    ledger = payload.get("ledger")
    if isinstance(ledger, Mapping):
        led = ledger_from_payload(payload)
        input_tokens = led.input_total
        output_tokens = led.output_tokens
        cache_read = led.cache_read_tokens
        cache_creation = led.cache_creation_tokens
    else:
        input_tokens = _coerce_int(payload.get("input_tokens"))
        output_tokens = _coerce_int(payload.get("output_tokens"))
        cache_read = _coerce_int(payload.get("cache_read_tokens"))
        cache_creation = _coerce_int(payload.get("cache_creation_tokens"))

    # Cost: production uses ``estimated_nanodollars``; the flat legacy /
    # synthetic shape uses ``nanodollars``. Prefer the former when it is
    # a valid int (including 0), else fall back to the alias.
    cost = payload.get("estimated_nanodollars")
    if isinstance(cost, bool) or not isinstance(cost, int):
        cost = payload.get("nanodollars")

    return {
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_cache_read_tokens": cache_read,
        "total_cache_creation_tokens": cache_creation,
        "total_nanodollars": _coerce_int(cost),
    }


def _interval_seconds() -> float:
    """Read ``INKFOOT_AGGREGATOR_INTERVAL_MS`` from env, falling back
    to 500 ms. Caller-friendly clamp: under 10 ms is almost certainly
    a typo (e.g. ``50`` meaning seconds), so we clamp up to 10 ms
    with a warning rather than burn a CPU."""
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

    The current implementation ``runs`` schema holds five totals
    (``total_input_tokens``, ``total_output_tokens``,
    ``total_cache_read_tokens``, ``total_cache_creation_tokens``,
    ``total_nanodollars``) plus ``outcome``. Per-category ledger
    breakdown stays in event payloads — see the ledger.

    Robustness notes:
    - Token counts are read from the nested ``ledger`` the shim writes;
      see :func:`_billed_from_payload` for the shape and the legacy
      flat-payload fallback.
    - Missing payload fields default to 0; non-int / bool values count
      as 0 rather than corrupting a total.
    - ``kind='outcome'`` event's ``payload_json.outcome`` takes
      precedence over any other claim; if multiple outcome events
      exist, the *last* by sequence wins.
    """
    totals: dict[str, Any] = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_nanodollars": 0,
        "outcome": None,
    }
    for ev in events:
        if ev.get("kind") == "embedding_call":
            # Embeddings are accounted separately and must never fold
            # into the run's token/cost totals — their payload reuses
            # the ``input_tokens`` key, which would otherwise be summed
            # below.
            continue
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

        for key, value in _billed_from_payload(payload).items():
            totals[key] += value

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
        storage: "Storage",
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
        # Tracks whether the worker was ever started — used by
        # :meth:`stop` to avoid the final-drain side-effect when no
        # work could possibly have queued up.
        self._ever_started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ever_started = True
        self._thread = threading.Thread(
            target=self._run, name="inkfoot-aggregator", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the worker, join its thread, and drain one final
        pass so events that arrived just before ``stop`` are
        reflected.

        Resilient to two failure modes:

        * **Never started.** If the worker was never started (e.g. a
          unit test instantiates one and bails), the final drain is
          skipped — there is nothing to flush.
        * **Storage already closed.** If an ``atexit`` ordering
          closes the storage before the worker stops, the final
          drain logs a warning and returns instead of raising. The
          worker thread is already joined at that point so callers
          don't see a partial shutdown.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if not self._ever_started:
            return
        try:
            self.drain_once()
        except Exception:
            # The most likely cause is the storage having been closed
            # already (backends raise RuntimeError once closed). Log
            # once at WARNING and proceed — the daemon thread is
            # already joined so there's nothing to clean up.
            _LOG.warning(
                "final drain after stop failed; the storage may have "
                "been closed before the worker",
                exc_info=True,
            )

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
        """Claim-and-project. Returns ``True`` when this call wrote
        new totals; ``False`` when another worker beat us or the row
        was already clean."""
        if not self._storage.claim_clean(run_id):
            return False
        events = list(self._storage.iter_events(run_id))
        totals = project_run_totals(events)
        self._storage.write_totals(run_id=run_id, totals=totals)
        return True
