#!/usr/bin/env python3
"""Daily fixture extractor — exports recent runs as JSON fixtures.

Per E6-S1 T4: a cron-friendly script the team runs nightly on the
production DB to harvest the prior day's runs as JSON fixtures.
The fixtures land alongside ``tests/fixtures/internal/`` (default)
or wherever ``--output`` points. Each fixture is the same shape the
validation harness consumes:

::

    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "request": { ... },
      "response": { ... }
    }

For runs recorded under ``capture_mode='metadata'`` (the Phase 0
default), only request/response **metadata** is exported — the
``event_contents`` sibling table doesn't get queried, so prompts +
responses are never written to disk. Set ``--include-content`` to
opt into replay-mode content export when the team has explicitly
captured it.

Usage::

    python scripts/extract_run_fixtures.py
    python scripts/extract_run_fixtures.py --since 2026-05-25 --output ./out/
    python scripts/extract_run_fixtures.py --since 24h --include-content
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path  # noqa: E402


_LOG = logging.getLogger("inkfoot.extract_fixtures")


def _parse_since(s: str) -> int:
    """Parse a ``--since`` argument into a wall-clock ms cutoff.

    Accepted shapes:
      * ``24h``, ``7d``, ``30m`` — relative duration from now
      * ``2026-05-25`` — absolute ISO date (UTC midnight)

    Returns: Unix epoch ms.
    """
    m = re.match(r"^(\d+)([smhd])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return int(time.time() * 1000) - seconds * 1000

    # ISO date (UTC midnight).
    import datetime as _dt

    try:
        date = _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"--since expects '24h' / '7d' / '30m' / 'YYYY-MM-DD', got {s!r}"
        ) from exc
    return int(
        _dt.datetime(
            date.year, date.month, date.day, tzinfo=_dt.timezone.utc
        ).timestamp()
        * 1000
    )


def _extract_call_payload(
    payload: dict[str, Any],
    *,
    include_content: bool,
    request_json: Optional[str],
    response_json: Optional[str],
) -> dict[str, Any]:
    """Build the fixture JSON for one llm_call event.

    Without ``--include-content`` the fixture carries the
    translator-derived ``ledger`` + the provider/model fields, but
    the request/response slots are left empty (privacy-first
    default). Replay-mode runs that *did* capture content can opt
    in.
    """
    provider = payload.get("provider", "unknown")
    model = payload.get("model", "")
    fixture: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "request": {},
        "response": {},
        "ledger_snapshot": payload.get("ledger", {}),
        "tools_offered": payload.get("tools_offered", []),
        "tools_called": payload.get("tools_called", []),
        "cache_status": payload.get("cache_status", "n/a"),
        "estimated_nanodollars": payload.get("estimated_nanodollars"),
    }
    if include_content:
        if request_json:
            try:
                fixture["request"] = json.loads(request_json)
            except (TypeError, ValueError):
                pass
        if response_json:
            try:
                fixture["response"] = json.loads(response_json)
            except (TypeError, ValueError):
                pass
    return fixture


def extract(
    *,
    db_path: Path,
    output_dir: Path,
    since_ms: int,
    include_content: bool = False,
    complete_only: bool = False,
) -> int:
    """Export every llm_call event recorded since ``since_ms`` as a
    fixture file. Returns the number of fixtures written.

    By default exports events from runs of *any* status —
    in-progress runs (``status='running'``) get their partial event
    stream too, which is useful when debugging a hang or a
    mid-investigation cost spike. Pass ``complete_only=True`` to
    restrict to runs that have already ended (``status='complete'``
    or ``'error'``); the nightly cron job typically wants this.

    Output filename shape:
    ``<provider>-<model>-<runid>-<seq>.json``. The full ULID is in
    the name so two runs created in the same millisecond never
    collide on disk (ULIDs are millisecond-prefixed; the random
    suffix disambiguates).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    storage = SQLiteStorage(path=db_path)
    storage.connect()
    try:
        # TODO(phase-2/postgres): the Storage Protocol has no
        # ``iter_events_since(occurred_at_ms, kind=...)`` method
        # yet so we reach into the SQLite connection directly.
        # Phase 2's Postgres backend will need a Protocol method
        # (CL5 review Finding #3 enumerates the other call sites
        # that share this pattern).
        conn = storage._conn()  # type: ignore[attr-defined]
        where = "e.kind = 'llm_call' AND e.occurred_at >= ?"
        params: list[Any] = [since_ms]
        if complete_only:
            where += " AND r.status IN ('complete', 'error')"
        cur = conn.execute(
            f"""
            SELECT e.id, e.run_id, e.sequence, e.payload_json,
                   ec.request_json, ec.response_json
            FROM events e
            LEFT JOIN event_contents ec ON ec.event_id = e.id
            JOIN runs r ON r.id = e.run_id
            WHERE {where}
            ORDER BY e.run_id, e.sequence
            """,
            params,
        )
        count = 0
        for row in cur.fetchall():
            raw = row["payload_json"]
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue

            fixture = _extract_call_payload(
                payload,
                include_content=include_content,
                request_json=row["request_json"],
                response_json=row["response_json"],
            )

            provider = (fixture.get("provider") or "unknown").replace(
                "/", "_"
            )
            model = (fixture.get("model") or "unknown").replace("/", "_")
            # Use the full run id so two runs created in the same
            # millisecond don't collide (ULIDs share their
            # timestamp-derived prefix; only the random suffix
            # disambiguates). Sanitise just in case some non-ULID
            # id shape lands.
            run_id = (row["run_id"] or "unknown").replace("/", "_")
            fname = (
                f"{provider}-{model}-{run_id}-seq{row['sequence']:04d}.json"
            )
            (output_dir / fname).write_text(
                json.dumps(fixture, indent=2, default=str)
            )
            count += 1
    finally:
        storage.close()
    return count


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="extract_run_fixtures",
        description=(
            "Export recent llm_call events as JSON fixtures for the "
            "validation corpus."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to the SQLite DB. Defaults to ~/.inkfoot/runs.db (the "
            "Inkfoot default storage location)."
        ),
    )
    parser.add_argument(
        "--since",
        default="24h",
        help=(
            "Window cutoff. Accepts '24h' / '7d' / '30m' / 'YYYY-MM-DD'. "
            "Default: 24h."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / "tests" / "fixtures" / "internal"),
        help=(
            "Output directory (default: tests/fixtures/internal/). "
            "Will be created if missing."
        ),
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help=(
            "Also export request/response bodies from the "
            "event_contents sibling table. Only meaningful for runs "
            "recorded under capture_mode='replay'."
        ),
    )
    parser.add_argument(
        "--complete-only",
        action="store_true",
        help=(
            "Skip runs that are still in progress (status='running'). "
            "The nightly cron typically wants this; default off so "
            "ad-hoc invocations can debug mid-run failures."
        ),
    )
    args = parser.parse_args(argv)

    try:
        since_ms = _parse_since(args.since)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else _default_db_path()
    if not db_path.exists():
        print(f"extract_run_fixtures: DB not found at {db_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output)
    count = extract(
        db_path=db_path,
        output_dir=output_dir,
        since_ms=since_ms,
        include_content=args.include_content,
        complete_only=args.complete_only,
    )
    print(f"extract_run_fixtures: wrote {count} fixture(s) to {output_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
