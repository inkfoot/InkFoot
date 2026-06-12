"""``inkfoot aggregator-worker`` subcommand.

The out-of-process aggregation daemon for the Postgres backend.
SQLite deployments never need this — their aggregator runs as an
in-process thread. With Postgres, ``inkfoot.instrument()`` skips the
in-process worker and this command picks up the job: one or more
worker processes coordinate through a session-level advisory lock so
exactly one sweeps at a time, and any standby takes over within its
lock-poll interval if the active worker dies.

Modes:

* default — run forever, sweeping on every interval tick. SIGTERM /
  SIGINT request a graceful stop (the in-flight sweep finishes and
  the lock is released).
* ``--once`` — acquire the lock, run a single sweep, exit. Useful
  for cron-style deployments and smoke tests.
* ``--health`` — read the shared heartbeat row and report when the
  last sweep happened. Exits 0 when a sweep ran within
  ``--max-age-s`` seconds, 1 otherwise — shaped for use as a
  container/systemd liveness probe.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

DEFAULT_HEALTH_MAX_AGE_S = 60.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _format_ts(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _health(storage: Any, *, max_age_s: float) -> int:
    heartbeat = storage.read_heartbeat()
    if heartbeat is None:
        print(
            "aggregator-worker: no heartbeat recorded — no worker has "
            "completed a sweep yet",
            file=sys.stderr,
        )
        return 1
    age_s = max(0.0, (_now_ms() - heartbeat["last_sweep_at"]) / 1000.0)
    line = (
        f"aggregator-worker: last sweep at "
        f"{_format_ts(heartbeat['last_sweep_at'])} "
        f"({age_s:.1f}s ago), {heartbeat['runs_swept']} runs swept"
    )
    if age_s > max_age_s:
        print(f"{line} — STALE (older than {max_age_s:.0f}s)")
        return 1
    print(line)
    return 0


def run(args: argparse.Namespace) -> int:
    # Local imports keep `inkfoot --help` importable without the
    # postgres extra installed.
    from inkfoot.storage.postgres import PostgresStorage  # noqa: PLC0415
    from inkfoot.storage.postgres_aggregator import (  # noqa: PLC0415
        PostgresAggregator,
    )

    try:
        storage = PostgresStorage(dsn=args.dsn)
    except ValueError as exc:
        print(f"aggregator-worker: {exc}", file=sys.stderr)
        return 2
    storage.connect()
    try:
        if args.health:
            return _health(storage, max_age_s=args.max_age_s)

        interval_seconds = (
            args.interval_ms / 1000.0
            if args.interval_ms is not None
            else None
        )
        aggregator = PostgresAggregator(
            storage, interval_seconds=interval_seconds
        )
        aggregator.connect()
        try:
            if args.once:
                processed = aggregator.sweep_once()
                print(
                    f"aggregator-worker: swept {processed or 0} runs",
                    file=sys.stderr,
                )
                return 0

            # Graceful shutdown: first signal requests a stop; the
            # in-flight sweep completes and the loop exits.
            def _request_stop(signum: int, _frame: Any) -> None:
                print(
                    "aggregator-worker: received "
                    f"{signal.Signals(signum).name}, stopping after the "
                    "current sweep",
                    file=sys.stderr,
                )
                aggregator.stop()

            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _request_stop)

            print(
                "aggregator-worker: started (advisory-lock coordinated); "
                "Ctrl-C to stop",
                file=sys.stderr,
            )
            aggregator.run_forever()
            print("aggregator-worker: stopped", file=sys.stderr)
            return 0
        finally:
            aggregator.close()
    finally:
        storage.close()
