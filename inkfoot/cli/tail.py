"""``inkfoot tail`` — live event stream over the storage event log.

The tail polls the SQLite events table on a short interval (default
200 ms) and prints one line per newly-inserted event. Optional
``--task NAME`` and ``--since 10m`` flags filter the stream and
backfill historical events before live-tailing.

Output is a compact one-liner suited for grep-and-eyeball:

    HH:MM:SS.mmm  kind            run-short  key=value ...

where ``key=value`` segments are smell- and event-kind-specific so
the most informative fields ride in line. Long payloads are
truncated rather than wrapping the terminal.

The tail is a polling loop because the SQLite storage backend has
no built-in notify mechanism. ``--poll-interval-ms`` lets a user
tune the latency/CPU trade-off; the default keeps median latency
under one second, which matches the acceptance bar in the design
note.

A handful of seams exist so tests can drive the loop without
sleeping:

* :func:`run` accepts an ``argparse.Namespace`` (or anything with
  ``getattr``-friendly attrs); the public CLI hands it in.
* :func:`tail_loop` is the loop body — tests call it with a
  ``max_iterations`` cap so they don't poll forever.
* :func:`fetch_new_events` is the read primitive; tests can stub
  it for cursor-bookkeeping assertions.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any, Iterable, Iterator, Optional

from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path


_LOG = logging.getLogger("inkfoot.cli.tail")


# Output column widths. ``kind`` is the longest legitimate event
# kind today (``llm_call``, ``policy_event``, ``checkpoint``) plus
# a margin so we don't reflow when a future event kind shows up.
_KIND_WIDTH = 14
_RUN_SHORT_CHARS = 8

# Hard cap on the per-poll fetch. Protects an operator who points
# tail at a heavily-written DB and `--since 30d` — without this the
# initial backfill SELECT would load every event into memory at
# once. The loop will keep paging the cap on subsequent iterations
# so no events are silently dropped, just trickled.
_FETCH_BATCH_SIZE = 1000

# Maximum number of characters from a payload field that we squeeze
# into the one-liner. Beyond this we truncate with an ellipsis so
# the terminal doesn't wrap and lose the next event under the fold.
_PAYLOAD_FIELD_CHAR_BUDGET = 96


_SINCE_PATTERN = re.compile(r"^(\d+)([smhd])$")


class TailArgError(ValueError):
    """Raised when ``--since`` (or another flag) is malformed."""


def run(args: Any) -> int:
    """argparse entry point — the CLI dispatcher calls this."""
    db_path = args.db if getattr(args, "db", None) else _default_db_path()
    raw_poll = getattr(args, "poll_interval_ms", None)
    # ``getattr ... or 200`` would silently coerce a user-supplied
    # zero to the default — we want zero to surface as an error.
    poll_ms = int(raw_poll if raw_poll is not None else 200)
    if poll_ms <= 0:
        print(
            "inkfoot tail: --poll-interval-ms must be positive",
            file=sys.stderr,
        )
        return 2

    since_raw = getattr(args, "since", None)
    try:
        since_ms = _parse_since(since_raw)
    except TailArgError as exc:
        print(f"inkfoot tail: {exc}", file=sys.stderr)
        return 2

    task_filter: Optional[str] = getattr(args, "task", None) or None
    max_iterations: Optional[int] = getattr(args, "max_iterations", None)

    storage = SQLiteStorage(path=db_path)
    storage.connect()
    try:
        try:
            tail_loop(
                storage=storage,
                task_filter=task_filter,
                since_ms=since_ms,
                poll_interval_s=poll_ms / 1000.0,
                max_iterations=max_iterations,
                writer=sys.stdout,
            )
        except KeyboardInterrupt:
            # ``Ctrl-C`` is the expected way to exit a live tail.
            # Swallow the exception so the shell doesn't see a
            # non-zero exit and the user gets a clean newline.
            sys.stdout.write("\n")
            return 0
        return 0
    finally:
        storage.close()


# ----------------------------------------------------------------------
# Loop core — separated so tests don't need argparse + a real CLI.
# ----------------------------------------------------------------------


def tail_loop(
    *,
    storage: SQLiteStorage,
    task_filter: Optional[str],
    since_ms: Optional[int],
    poll_interval_s: float,
    max_iterations: Optional[int],
    writer,
    sleep: Any = time.sleep,
) -> int:
    """Drive the tail loop. Returns the number of events emitted.

    ``sleep`` is exposed so tests can replace it with a no-op and
    drive ``max_iterations`` iterations instantly.
    """
    if since_ms is not None:
        cursor = _resolve_backfill_cursor(
            storage=storage, since_ms=since_ms
        )
    else:
        # No backfill: the tail starts from "now". Pin the cursor
        # to the highest existing rowid so the first poll only
        # surfaces events the storage hasn't yet seen — the help
        # text on ``--since`` promises this default explicitly.
        cursor = _resolve_live_cursor(storage=storage)

    emitted = 0
    iteration = 0
    while True:
        new_events, cursor = fetch_new_events(
            storage=storage,
            cursor=cursor,
            task_filter=task_filter,
            limit=_FETCH_BATCH_SIZE,
        )
        for event in new_events:
            writer.write(format_event_line(event) + "\n")
            writer.flush()
            emitted += 1
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            return emitted
        sleep(poll_interval_s)


def _resolve_backfill_cursor(*, storage: SQLiteStorage, since_ms: int) -> int:
    """Pick the rowid floor for backfill given a ``--since`` window.

    Returns the largest rowid whose ``occurred_at`` is *before*
    ``since_ms``. New events have rowids strictly greater than this
    value, so the main loop picks them up via ``WHERE rowid > ?``.
    """
    conn = storage._conn()  # type: ignore[attr-defined]
    cur = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM events WHERE occurred_at < ?",
        (since_ms,),
    )
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _resolve_live_cursor(*, storage: SQLiteStorage) -> int:
    """Pick the rowid floor for a "no backfill" tail.

    Returns the highest existing rowid so the loop's first poll
    only surfaces events inserted after the tail started."""
    conn = storage._conn()  # type: ignore[attr-defined]
    cur = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events")
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def fetch_new_events(
    *,
    storage: SQLiteStorage,
    cursor: int,
    task_filter: Optional[str],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Pull events with ``rowid > cursor`` (and optional task
    filter); return ``(events_in_rowid_order, new_cursor)``.

    The new cursor is the maximum rowid actually returned, so a
    subsequent call won't re-emit the same rows.
    """
    conn = storage._conn()  # type: ignore[attr-defined]
    params: list[Any] = [int(cursor)]
    join_clause = ""
    where_extra = ""
    if task_filter:
        join_clause = "JOIN runs ON events.run_id = runs.id"
        where_extra = " AND runs.task = ?"
        params.append(task_filter)
    params.append(int(limit))
    cur = conn.execute(
        f"""
        SELECT events.rowid AS _rowid, events.*
        FROM events
        {join_clause}
        WHERE events.rowid > ? {where_extra}
        ORDER BY events.rowid ASC
        LIMIT ?
        """,
        params,
    )
    rows = [dict(row) for row in cur.fetchall()]
    new_cursor = rows[-1]["_rowid"] if rows else cursor
    return rows, int(new_cursor)


# ----------------------------------------------------------------------
# Line formatter — pure, tested directly.
# ----------------------------------------------------------------------


def format_event_line(event: dict[str, Any]) -> str:
    """Render one event row as a single output line.

    The line shape is::

        HH:MM:SS.mmm  kind            run-short  key=value ...

    Event-kind-aware ``key=value`` fragments come from
    :func:`_payload_fields_for_kind`. Unknown kinds fall back to a
    generic "first few payload keys" projection.
    """
    timestamp = _format_timestamp_ms(int(event.get("occurred_at") or 0))
    kind = str(event.get("kind") or "?")
    run_short = _short_run_id(str(event.get("run_id") or ""))
    payload = _safe_payload(event.get("payload_json"))
    fields = _payload_fields_for_kind(kind, payload)
    rendered_fields = " ".join(
        f"{key}={_truncate(_stringify(value))}"
        for key, value in fields
        if value is not None and value != ""
    )
    return (
        f"{timestamp}  {kind.ljust(_KIND_WIDTH)} {run_short}"
        + (f"  {rendered_fields}" if rendered_fields else "")
    )


def _payload_fields_for_kind(
    kind: str, payload: dict[str, Any]
) -> list[tuple[str, Any]]:
    """Pick the most informative fields for the given event kind.

    Falls through to a generic projection (first few keys) when the
    kind is unknown — preferable to printing the whole JSON blob
    inline."""
    if kind == "llm_call":
        return [
            ("provider", payload.get("provider")),
            ("model", payload.get("model")),
            (
                "input_tokens",
                _sum_input_tokens(payload),
            ),
            ("output_tokens", payload.get("output_tokens")),
            ("cost_nd", payload.get("estimated_nanodollars")),
        ]
    if kind == "run_start":
        return [
            ("task", payload.get("task")),
            ("agent_kind", _nested(payload, "metadata", "agent_kind")),
        ]
    if kind == "run_end":
        return [
            ("status", payload.get("status")),
            ("error", payload.get("error_message")),
        ]
    if kind == "outcome":
        return [
            ("outcome", payload.get("outcome")),
            ("quality", payload.get("quality_score")),
        ]
    if kind == "user_tag":
        return [
            ("key", payload.get("key")),
            ("value", payload.get("value")),
        ]
    if kind == "checkpoint":
        return [("label", payload.get("label"))]
    if kind == "smell":
        return [
            ("id", payload.get("smell_id") or payload.get("id")),
            ("severity", payload.get("severity")),
        ]
    # Generic fallthrough — only surface keys whose name starts
    # with one of a small safe-prefix list. Unfiltered projections
    # would happily print ``api_key=sk-...`` for a future event
    # kind that smuggles a secret into the payload; this list
    # captures the descriptive-metadata fields tail callers
    # actually want to see (id, name, label, status, kind, task,
    # count, …) while denying everything else.
    return [
        (key, payload.get(key))
        for key in sorted(payload)
        if _is_safe_generic_field(key)
    ][:3]


# Field-name prefixes the generic projection will surface. Anything
# else falls through silently — the tail line still renders, just
# without unknown-key fragments. Keep this list deny-by-default:
# adding a new safe prefix is one line, removing one isn't.
_SAFE_GENERIC_PREFIXES: tuple[str, ...] = (
    "id",
    "name",
    "label",
    "status",
    "kind",
    "task",
    "count",
    "stage",
    "type",
)


def _is_safe_generic_field(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in _SAFE_GENERIC_PREFIXES)


# ----------------------------------------------------------------------
# Small helpers.
# ----------------------------------------------------------------------


def _parse_since(raw: Optional[str]) -> Optional[int]:
    """Convert ``--since 10m`` to an absolute Unix-ms floor.

    Returns ``None`` when ``raw`` is falsy (no backfill — only
    events inserted after the command starts).
    """
    if not raw:
        return None
    match = _SINCE_PATTERN.match(raw)
    if not match:
        raise TailArgError(
            f"invalid --since value {raw!r}; expected `<n><unit>` "
            f"where unit is one of s/m/h/d (e.g. 10m, 2h, 7d)"
        )
    quantity = int(match.group(1))
    unit = match.group(2)
    seconds = quantity * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return int(time.time() * 1000) - seconds * 1000


def _format_timestamp_ms(ms: int) -> str:
    """Render a Unix-ms timestamp as ``HH:MM:SS.mmm`` (local time).

    The wall-clock fragment is enough to correlate events across
    log lines without padding every line with the full date — most
    tails happen inside a single session.
    """
    if ms <= 0:
        return "--:--:--.---"
    seconds, milliseconds = divmod(int(ms), 1000)
    return time.strftime("%H:%M:%S", time.localtime(seconds)) + f".{milliseconds:03d}"


def _short_run_id(run_id: str) -> str:
    """Return a stable short form for visual scanning.

    Run ids look like ``run-01H...``. We strip the ``run-`` prefix
    first so the suffix can't split inside the hyphen on short
    ids, then keep the last :data:`_RUN_SHORT_CHARS`. Shorter ids
    pass through left-padded so test fixtures and abnormal ids
    still align in the output column."""
    if not run_id:
        return ("?" * _RUN_SHORT_CHARS).ljust(_RUN_SHORT_CHARS)
    stripped = run_id[4:] if run_id.startswith("run-") else run_id
    if not stripped:
        return ("?" * _RUN_SHORT_CHARS).ljust(_RUN_SHORT_CHARS)
    return stripped[-_RUN_SHORT_CHARS:].ljust(_RUN_SHORT_CHARS)


def _safe_payload(raw: Any) -> dict[str, Any]:
    """Parse ``payload_json`` defensively; never raise."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, (str, bytes, bytearray)):
        return {}
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover — defensive
            return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested(payload: dict[str, Any], outer: str, inner: str) -> Any:
    """Return ``payload[outer][inner]`` or None for missing levels."""
    block = payload.get(outer)
    if not isinstance(block, dict):
        return None
    return block.get(inner)


def _sum_input_tokens(payload: dict[str, Any]) -> Optional[int]:
    """Pull the structural input total from a NeutralCall payload.

    The NeutralCall payload carries one int per cause category, not
    a precomputed total — sum the 11 structural categories so the
    tail line shows the headline ``input_tokens`` an operator
    expects to see.
    """
    keys = (
        "system_static_tokens",
        "system_dynamic_tokens",
        "user_input_tokens",
        "tool_schema_tokens",
        "tool_result_tokens",
        "retrieved_context_tokens",
        "memory_tokens",
        "retry_overhead_tokens",
        "summariser_tokens",
        "reasoning_tokens",
        "guardrail_tokens",
    )
    total = 0
    saw_any = False
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            total += int(raw)
            saw_any = True
        except (TypeError, ValueError):
            continue
    return total if saw_any else None


def _stringify(value: Any) -> str:
    """Coerce a payload value into something line-printable."""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if value is None:
        return ""
    return json.dumps(value, default=str)


def _truncate(text: str, *, budget: int = _PAYLOAD_FIELD_CHAR_BUDGET) -> str:
    """Trim ``text`` to ``budget`` chars with an ellipsis suffix."""
    if len(text) <= budget:
        return text
    return text[: budget - 1] + "…"


__all__ = [
    "run",
    "tail_loop",
    "fetch_new_events",
    "format_event_line",
    "TailArgError",
]
