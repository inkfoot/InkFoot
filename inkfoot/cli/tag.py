"""``inkfoot tag <run-id> <key> <value>`` — late-tagging subcommand.

A user who finished a run without the right tags can attach
metadata after the fact. The tag lands as a ``user_tag`` event on
the existing run; the aggregator picks it up via the dirty flag.

Value parsing: the CLI passes ``value`` as a string. We try
``json.loads`` first (so ``inkfoot tag run-1 retries 5`` gives an
int, not the string "5"), falling back to the raw string. This
matches the ``inkfoot.tag()`` API's JSON-scalar contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ulid import ULID


def run(args: argparse.Namespace) -> int:
    """Invoked by ``inkfoot/cli/main.py`` when the user runs
    ``inkfoot tag``. Inserts one ``user_tag`` event on the named
    run, then exits."""
    from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    run_id = args.run_id
    key = args.key
    value_raw = args.value

    db_path: Path | str
    if getattr(args, "db", None):
        db_path = args.db
    else:
        db_path = _default_db_path()

    # Best-effort JSON parse so numeric / bool / null pass through
    # as their typed values. Falls back to raw string.
    try:
        value: Any = json.loads(value_raw)
    except (TypeError, ValueError):
        value = value_raw

    storage = SQLiteStorage(path=db_path)
    try:
        storage.connect()
        run_row = storage.get_run(run_id)
        if run_row is None:
            print(f"inkfoot tag: no run with id {run_id!r}")
            return 1

        # Allocate the next sequence after the run's highest event.
        conn = storage._conn()  # type: ignore[attr-defined]
        max_seq_row = conn.execute(
            "SELECT MAX(sequence) AS max_seq FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        next_seq = (max_seq_row["max_seq"] or 0) + 1

        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="user_tag",
            occurred_at=int(_time.time() * 1000),
            sequence=next_seq,
            payload_json=json.dumps({"key": key, "value": value}),
            capture_mode="metadata",
        )
        print(
            f"inkfoot tag: added tag {key}={value!r} to run {run_id}"
        )
        return 0
    finally:
        storage.close()
