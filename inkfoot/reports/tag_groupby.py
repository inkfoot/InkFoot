"""Tag-value rollup for ``inkfoot report --group-by tag.<key>``.

``inkfoot.tag("customer_tier", "pro")`` (and the after-the-fact
``inkfoot tag`` CLI) write ``user_tag`` events; nothing projects
them onto the ``runs`` table. This module derives each run's
bucket for one tag key from those events in a single
window-bounded query — one round-trip, not one per run.

The SQL keeps to the portable core — a join and equality filters;
the JSON payload parses in Python rather than in dialect-specific
``json_extract`` — so the query *shape* is dialect-portable. The
placeholders are SQLite DB-API qmark style (``?``), which psycopg
does not accept: a future Postgres path needs placeholder
translation (``%s``), not just a ``--dsn`` wired in.

Bucket rules:

* The tag's value, stringified, is the bucket (``5`` → ``"5"``,
  ``true`` → ``"True"``).
* A run tagged with the same key more than once buckets by the
  *last* value (highest event sequence) — the same last-write-wins
  the tag API documents.
* Runs without the tag — or tagged ``None`` / the empty string —
  belong in :data:`UNKNOWN_BUCKET` (``"unknown"``), so a
  partially-tagged fleet stays visible instead of dropping rows.
  The query helper returns tagged runs only — unusable values
  (``None`` / empty) map straight to the unknown bucket; callers
  default the runs that never carried the tag.
"""

from __future__ import annotations

import json
from typing import Any, Optional


# Bucket label for runs the tag never reached.
UNKNOWN_BUCKET = "unknown"


def tag_buckets(
    conn: Any,
    *,
    key: str,
    since_ms: int,
    task_filter: Optional[str] = None,
) -> dict[str, str]:
    """Map ``run_id → bucket`` for every run in the window carrying
    tag ``key``.

    ``conn`` is a DB-API connection whose rows support name access
    (the CLI hands over the storage backend's live connection).
    Runs absent from the result didn't carry the tag; callers
    bucket those as :data:`UNKNOWN_BUCKET`.
    """
    where = "e.kind = 'user_tag' AND r.started_at >= ?"
    params: list[Any] = [since_ms]
    if task_filter:
        where += " AND r.task = ?"
        params.append(task_filter)

    cur = conn.execute(
        f"""
        SELECT e.run_id AS run_id, e.payload_json AS payload_json
        FROM events e
        JOIN runs r ON r.id = e.run_id
        WHERE {where}
        ORDER BY e.sequence
        """,
        params,
    )

    buckets: dict[str, str] = {}
    for row in cur.fetchall():
        run_id = row["run_id"]
        if not run_id:
            continue
        raw = row["payload_json"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("key") != key:
            continue
        value = payload.get("value")
        if value is None or value == "":
            buckets[run_id] = UNKNOWN_BUCKET
        else:
            buckets[run_id] = str(value)
    return buckets
