"""``inkfoot report`` — attribution bar chart + smells.

The renderer is a **pure function** of ``(run, ledger_totals,
smells)`` → ``str``: no storage I/O, no
side effects, no terminal-width detection. The CLI scaffold below
loads the inputs from storage and hands them to the renderer; the
renderer can be unit-tested without spinning up anything.

Output shape:

* Header line: ``Run <id> · <task> · <duration> · $<cost> · <outcome>``
* "Causal attribution:" block — one row per ledger field with a
  12-col bar, percentage, dollar figure, and a smell-marker if the
  field is a smell's ``primary_category``.
* Empty rows hidden by default; ``show_zero=True`` includes all 14.
* "Smells detected (N):" block — one stanza per :class:`DetectionResult`
  with the recommendation.
* Optional "Estimated savings if both fixed: ~$… (-Y%)." footer
  computed from the sum of smell impacts.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence

from inkfoot.ledger import INPUT_CATEGORIES
from inkfoot.money import format_usd
from inkfoot.pricing import estimate_per_category
from inkfoot.reports.cost_per_success import (
    render_aggregate_table,
    rollup_cost_per_success,
)

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.smells import DetectionResult
    from inkfoot.storage import Storage


# 14 ledger fields in canonical reporting order. ``output_tokens``
# sits at the bottom regardless of cost share so the eye lands on
# input attribution first.
_ALL_FIELDS: tuple[str, ...] = INPUT_CATEGORIES + (
    "cache_creation_tokens",
    "cache_read_tokens",
    "output_tokens",
)
_BAR_WIDTH = 12


def _short_label(field: str) -> str:
    """Strip the ``_tokens`` suffix for the label column."""
    return field[:-7] if field.endswith("_tokens") else field


def _format_duration(started_at: Optional[int], ended_at: Optional[int]) -> str:
    """Render run duration as ``18.2s`` / ``2m 15s`` / ``in progress``."""
    if not started_at or not ended_at:
        return "in progress"
    ms = max(0, int(ended_at) - int(started_at))
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}m {seconds:02d}s"


def _format_outcome(run: dict[str, Any]) -> str:
    outcome = run.get("outcome")
    if outcome is None:
        return "(no outcome)"
    qs = run.get("quality_score")
    if qs is None:
        return outcome
    return f"{outcome} ({float(qs):.2f})"


def _bar(share: float) -> str:
    """Render the 12-column unicode bar for ``share`` ∈ [0, 1]."""
    if share <= 0:
        return "░" * _BAR_WIDTH
    filled = max(0, min(_BAR_WIDTH, int(share * _BAR_WIDTH)))
    # Render at least one filled cell when share is small but non-zero
    # so a tiny category still shows.
    if filled == 0 and share > 0:
        filled = 1
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _smell_markers_by_field(
    smells: Sequence["DetectionResult"],
) -> dict[str, list["DetectionResult"]]:
    """Group smell hits by their ``primary_category`` so the bar-chart
    row knows which marker(s) to render."""
    out: dict[str, list["DetectionResult"]] = {}
    for hit in smells:
        cat = hit.smell.primary_category
        if cat is None:
            continue
        out.setdefault(cat, []).append(hit)
    return out


def _short_smell_tag(hit: "DetectionResult") -> str:
    """One-word tag for the inline bar-chart marker. The full smell
    title shows in the "Smells detected" block below."""
    short = {
        "unstable-prompt-prefix": "cache-breaker",
        "oversized-tool-result-recycled": "oversized",
        "expensive-model-low-entropy": "expensive-for-task",
        "recurring-cache-writes": "cache-thrash",
        "runaway-retry-loop": "retry-loop",
        "tool-schema-drift": "schema-drift",
        "cost-skewed-by-outlier": "outlier",
        "unbounded-conversation-history": "unbounded-history",
        "over-instrumented-retries": "retry-overhead",
        "summariser-not-firing": "no-summariser",
    }
    return short.get(hit.smell.id, hit.smell.id)


# ----------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------


def render(
    *,
    run: dict[str, Any],
    ledger_totals: dict[str, int],
    smells: Sequence["DetectionResult"],
    show_zero: bool = False,
) -> str:
    """Render the single-run report as a string.

    Pure: same inputs always produce the same output. ``run`` is a
    dict (``SQLiteStorage.get_run`` row), ``ledger_totals`` is the
    per-category nanodollar split from :func:`estimate_per_category`,
    ``smells`` is the engine's per-run detection list.
    """
    total_nd = sum(ledger_totals.values())
    if total_nd == 0:
        # Fall back to the run's projected total so the headline
        # number isn't $0.0000 on a run we couldn't price.
        total_nd = int(run.get("total_nanodollars") or 0)

    lines: list[str] = []

    # Header.
    header_parts = [
        f"Run {run.get('id', '<unknown>')}",
    ]
    task = run.get("task")
    if task:
        header_parts.append(task)
    header_parts.append(
        _format_duration(run.get("started_at"), run.get("ended_at"))
    )
    header_parts.append(format_usd(total_nd, decimals=4))
    header_parts.append(_format_outcome(run))
    lines.append(" · ".join(header_parts))
    lines.append("")

    lines.append("Causal attribution:")
    markers = _smell_markers_by_field(smells)

    # Categories sorted by cost descending. When cost is
    # zero across the board, fall back to declaration order so the
    # output stays deterministic.
    fields_with_cost = [
        (name, int(ledger_totals.get(name, 0))) for name in _ALL_FIELDS
    ]
    if any(cost > 0 for _, cost in fields_with_cost):
        fields_with_cost.sort(key=lambda kv: kv[1], reverse=True)

    for name, cost_nd in fields_with_cost:
        if cost_nd == 0 and not show_zero:
            continue
        share = (cost_nd / total_nd) if total_nd > 0 else 0.0
        label = _short_label(name).ljust(18)
        pct = f"{share * 100:5.1f}%"
        bar = _bar(share)
        dollars = format_usd(cost_nd, decimals=4)
        marker_str = ""
        for hit in markers.get(name, ()):
            marker_str += f"  ⚠ {_short_smell_tag(hit)}"
        lines.append(f"  {label} {pct}  {bar}  {dollars}{marker_str}")

    # Footnote when always-zero current categories were hidden.
    # We scope this specifically to the three fields that *can't*
    # carry tokens in the current implementation (summariser/guardrail/retry_overhead);
    # other zero categories (retrieved_context, cache_*, etc.) hide
    # without the footnote because their absence isn't surprising
    # to the reader.
    if not show_zero:
        always_zero_short_labels = sorted(
            _short_label(name)
            for name in _ALWAYS_ZERO_CATEGORIES
            if int(ledger_totals.get(name, 0)) == 0
        )
        if always_zero_short_labels:
            lines.append("")
            lines.append(
                f"({_format_list(always_zero_short_labels)} "
                f"are always-zero in the current implementation — hidden by default)"
            )

    # Smells block.
    if smells:
        lines.append("")
        lines.append(f"Smells detected ({len(smells)}):")
        for hit in smells:
            tag = _short_smell_tag(hit)
            lines.append(f"  · {hit.smell.id}  ({tag})")
            lines.append(f"    → {hit.smell.recommendation}")
        total_savings_nd = sum(hit.estimated_cost_impact_nd for hit in smells)
        if total_savings_nd > 0 and total_nd > 0:
            pct = -(total_savings_nd / total_nd * 100)
            lines.append("")
            lines.append(
                f"Estimated savings if fixed: "
                f"~{format_usd(total_savings_nd, decimals=4)}/run "
                f"({pct:+.0f}%)."
            )
    return "\n".join(lines)


# Fields that are guaranteed to be zero in the current implementation (no translator
# populates them; smell engine doesn't synthesise). The footnote
# under the bar chart names just these — listing all hidden
# categories would surprise the reader for retrieved_context (which
# tag_retrieval *does* populate when the user marks it) or
# cache_* (which depend on the provider's behaviour).
_ALWAYS_ZERO_CATEGORIES = (
    "summariser_tokens",
    "guardrail_tokens",
    "retry_overhead_tokens",
)


def _format_list(items: list[str]) -> str:
    """Render ``items`` as ``"a, b, and c"`` (Oxford-comma joined).
    Single-item lists return as-is; two-item lists join with " and ".
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


# ----------------------------------------------------------------------
# CLI scaffold — argparse-based (matches existing CLI for now).
# typer would be cleaner but isn't a hard dep in the current implementation.
# ----------------------------------------------------------------------


def run(args: Any) -> int:
    """Invoked by ``inkfoot/cli/main.py`` when the user runs
    ``inkfoot report``. Loads the storage backend, materialises the
    inputs, hands them to :func:`render`, and prints."""
    from inkfoot.smells import DEFAULT_SMELLS  # noqa: PLC0415
    from inkfoot.smells.engine import SmellEngine  # noqa: PLC0415
    from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path  # noqa: PLC0415

    db_path = args.db if getattr(args, "db", None) else _default_db_path()
    storage = SQLiteStorage(path=db_path)
    try:
        storage.connect()

        group_by = getattr(args, "group_by", None)
        is_aggregate = bool(getattr(args, "last", None))
        group_by_error = _group_by_error(group_by, aggregate=is_aggregate)
        if group_by_error:
            print(group_by_error)
            return 2

        if is_aggregate:
            print(_render_aggregate(storage, args))
            return 0

        run_id = getattr(args, "run", None)
        if not run_id:
            print(
                "inkfoot report: pass --run <id> or --last <duration>"
            )
            return 2

        row = storage.get_run(run_id)
        if row is None:
            print(f"inkfoot report: no run with id {run_id!r}")
            return 1

        events = list(storage.iter_events(run_id))

        metadata_key = _metadata_group_key(group_by)
        if metadata_key is not None:
            print(_render_per_metadata(row, events, key=metadata_key))
            return 0

        ledger_totals = _aggregate_ledger_totals(events)
        # ``--no-smells`` is the power-user opt-out for the inline
        # rendering. When set we hand an empty list to the renderer
        # so the stanza disappears (and the cheaper code path skips
        # smell evaluation entirely).
        if getattr(args, "no_smells", False):
            smells = []
        else:
            row = _attach_outlier_context(storage, dict(row))
            smells = SmellEngine(list(DEFAULT_SMELLS)).evaluate(row, events)
        print(
            render(
                run=row,
                ledger_totals=ledger_totals,
                smells=smells,
                show_zero=bool(getattr(args, "show_zero", False)),
            )
        )
        return 0
    finally:
        storage.close()


def _group_by_error(group_by: Any, *, aggregate: bool) -> Optional[str]:
    """Validate a ``--group-by`` value for the selected view. Returns
    the error message to print (the CLI exits 2 on it), or ``None``
    when the value is valid.

    Shared by :func:`run` (so both views fail with the same exit
    code) and :func:`_render_aggregate` — the latter re-checks so
    direct callers (tests, future API surface) get the same
    validation as the CLI path.
    """
    metadata_key = _metadata_group_key(group_by)
    if metadata_key == "":
        return (
            f"inkfoot report: invalid --group-by value {group_by!r} "
            "(metadata.<key> needs a key, e.g. metadata.agent_name)"
        )
    tag_key = _tag_group_key(group_by)
    if tag_key == "":
        return (
            f"inkfoot report: invalid --group-by value {group_by!r} "
            "(tag.<key> needs a key, e.g. tag.customer_tier)"
        )
    if aggregate:
        if metadata_key is not None:
            return (
                f"inkfoot report: --group-by {group_by} only applies to "
                "a single run (pair with --run, not --last). "
                "Per-metadata aggregates across runs are deferred to "
                "future aggregate analysis."
            )
        if tag_key is not None:
            return None
        if (group_by or "task") not in ("task", "agent_kind"):
            return (
                f"inkfoot report: invalid --group-by value {group_by!r} "
                "(expected 'task', 'agent_kind', or 'tag.<key>'; "
                "'node' / 'metadata.<key>' pair with --run)"
            )
        return None
    if metadata_key is not None:
        return None
    if tag_key is not None:
        return (
            f"inkfoot report: --group-by {group_by} only applies to "
            "the aggregate view (pair with --last, not --run) — a "
            "single run has at most one value per tag."
        )
    if group_by not in (None, "task", "agent_kind"):
        return (
            f"inkfoot report: invalid --group-by value {group_by!r} "
            "(expected 'task', 'agent_kind', 'node', "
            "'metadata.<key>', or 'tag.<key>')"
        )
    return None


def _metadata_group_key(group_by: Any) -> Optional[str]:
    """Map a ``--group-by`` value onto the metadata key the
    single-run view slices by. ``node`` stays as the alias for
    ``metadata.node_name``; ``metadata.<key>`` selects any
    adapter-stamped key (``metadata.agent_name`` /
    ``metadata.task_name`` for multi-agent crews, ...). ``None``
    means "not a metadata group-by" (task / agent_kind / default).
    """
    if group_by == "node":
        return "node_name"
    if isinstance(group_by, str) and group_by.startswith("metadata."):
        return group_by[len("metadata."):]
    return None


def _tag_group_key(group_by: Any) -> Optional[str]:
    """Map a ``--group-by`` value onto the user-tag key the
    aggregate view buckets by (``tag.customer_tier`` →
    ``customer_tier``). ``None`` means "not a tag group-by"."""
    if isinstance(group_by, str) and group_by.startswith("tag."):
        return group_by[len("tag."):]
    return None


def _render_per_metadata(
    run: dict[str, Any], events: list[dict[str, Any]], *, key: str
) -> str:
    """Per-metadata-value ledger summary for ``inkfoot report --run
    <id> --group-by metadata.<key>`` (and the ``node`` alias for
    ``metadata.node_name``) — the framework metadata contract.

    Groups every ``llm_call`` event by its ``payload.metadata.<key>``
    and emits one row per value with fresh-input tokens, output
    tokens, dollar cost (sum of per-category estimates), and call
    count. Calls without the key land under ``(no <label>)`` —
    important to show so the user spots adapters that aren't
    stamping the field.
    """
    from inkfoot.smells._helpers import (  # noqa: PLC0415
        ledger_from_payload,
    )

    label = "node" if key == "node_name" else key
    rows: dict[str, dict[str, int]] = {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != "llm_call":
            continue
        raw = ev.get("payload_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        metadata = payload.get("metadata") or {}
        value = metadata.get(key) if isinstance(metadata, dict) else None
        bucket = str(value) if value else f"(no {label})"
        ledger = ledger_from_payload(payload)
        provider = payload.get("provider", "")
        model = payload.get("model", "")
        per_cat = estimate_per_category(provider, model, ledger)
        bucket_row = rows.setdefault(
            bucket,
            {"calls": 0, "input": 0, "output": 0, "nanodollars": 0},
        )
        bucket_row["calls"] += 1
        bucket_row["input"] += sum(
            int(getattr(ledger, name, 0) or 0) for name in INPUT_CATEGORIES
        )
        bucket_row["output"] += int(getattr(ledger, "output_tokens", 0) or 0)
        bucket_row["nanodollars"] += sum(int(nd) for nd in per_cat.values())

    if not rows:
        header = (
            f"Run {run.get('id') or '?'} · {run.get('task') or '(no task)'}"
        )
        if key == "node_name":
            return (
                f"{header}\n"
                "No node-tagged LLM calls in this run.\n"
                "Hint: install a framework adapter "
                "(inkfoot.langgraph.instrument) "
                "or call inkfoot.tag_node('node-name') before LLM calls."
            )
        return (
            f"{header}\n"
            f"No LLM calls carrying metadata.{key} in this run.\n"
            "Hint: install a framework adapter that stamps it (e.g. "
            "inkfoot.crewai.instrument sets agent_name and task_name)."
        )

    lines = [
        (
            f"Run {run.get('id') or '?'} · "
            f"{run.get('task') or '(no task)'} · per-{label} ledger"
        ),
        "",
        (
            f"  {label:<24} {'calls':>6} {'input_tok':>10} "
            f"{'output_tok':>11} {'cost':>10}"
        ),
    ]
    # Sort by nanodollar spend desc so the eye lands on the most
    # expensive bucket first; ``(no <label>)`` floats wherever its
    # total places.
    for bucket, agg in sorted(
        rows.items(), key=lambda kv: kv[1]["nanodollars"], reverse=True
    ):
        lines.append(
            f"  {bucket[:24]:<24} {agg['calls']:>6} "
            f"{agg['input']:>10} {agg['output']:>11} "
            f"{format_usd(agg['nanodollars'], decimals=4):>10}"
        )
    return "\n".join(lines)


def _aggregate_ledger_totals(
    events: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Sum the per-category nanodollar splits across every
    ``llm_call`` event in the run. Falls back to "tokens only" (all
    zeros) when no calls have pricing."""
    totals: dict[str, int] = {name: 0 for name in _ALL_FIELDS}
    from inkfoot.smells._helpers import (  # noqa: PLC0415
        ledger_from_payload,
    )

    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != "llm_call":
            continue
        raw = ev.get("payload_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        provider = payload.get("provider", "")
        model = payload.get("model", "")
        ledger = ledger_from_payload(payload)
        per_cat = estimate_per_category(provider, model, ledger)
        for name, nd in per_cat.items():
            totals[name] += int(nd)
    return totals


def _render_aggregate(storage: "Storage", args: Any) -> str:
    """Cross-run aggregate view (``--last 7d`` / ``--task name``).

    SELECTs the window's run rows once and hands them to
    :func:`inkfoot.reports.cost_per_success.rollup_cost_per_success`
    — bucketed by ``--group-by task`` | ``agent_kind`` |
    ``tag.<key>`` — then renders the documented columns:

    * ``runs`` — count of runs in the bucket
    * ``cost/success`` — bucket spend ÷ successful runs (the
      headline; ``—`` when the bucket has no successes)
    * ``cost/accepted_answer`` — bucket spend ÷ accepted-answer
      runs
    * ``avg_$`` / ``p95_$`` — distribution shape (p95 in Python;
      SQLite has no native percentile aggregate)
    * ``success%`` — outcome="success" rate among the bucket's
      outcome-bearing runs

    Runs that never called ``set_outcome`` aggregate into the
    pinned-last ``uninstrumented`` row instead of silently
    polluting their bucket's outcome math.
    """
    import re  # noqa: PLC0415

    last = getattr(args, "last", None) or "30d"
    m = re.match(r"^(\d+)([smhd])$", last)
    if not m:
        return f"inkfoot report: invalid --last value {last!r} (e.g. 7d, 24h)"
    n, unit = int(m.group(1)), m.group(2)
    seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    import time as _time  # noqa: PLC0415

    cutoff_ms = int(_time.time() * 1000) - seconds * 1000

    # TODO(future/postgres): the Storage Protocol has no
    # ``aggregate_runs_since`` method yet so we reach into the
    # SQLite connection directly. A future Postgres backend will
    # need a proper Protocol method.
    conn = storage._conn()  # type: ignore[attr-defined]
    task_filter = getattr(args, "task", None)
    where = "started_at >= ?"
    params: list[Any] = [cutoff_ms]
    if task_filter:
        where += " AND task = ?"
        params.append(task_filter)

    error = _group_by_error(getattr(args, "group_by", None), aggregate=True)
    if error:
        return error
    group_by = getattr(args, "group_by", None) or "task"

    cur = conn.execute(
        f"""
        SELECT id, task, agent_kind, outcome, total_nanodollars
        FROM runs
        WHERE {where}
        """,
        params,
    )
    run_rows = [dict(row) for row in cur.fetchall()]
    if not run_rows:
        return f"inkfoot report: no runs in the last {last}."

    tag_key = _tag_group_key(group_by)
    if tag_key is not None:
        from inkfoot.reports.tag_groupby import (  # noqa: PLC0415
            UNKNOWN_BUCKET,
            tag_buckets,
        )

        buckets = tag_buckets(
            conn,
            key=tag_key,
            since_ms=cutoff_ms,
            task_filter=task_filter,
        )

        def bucket_of(run: dict[str, Any]) -> str:
            return buckets.get(run.get("id"), UNKNOWN_BUCKET)

    else:
        # Validated above: a plain column name (task / agent_kind).
        column = group_by

        def bucket_of(run: dict[str, Any]) -> str:
            return run.get(column) or "(none)"

    bucket_rows = rollup_cost_per_success(run_rows, bucket_of=bucket_of)
    lines = [
        render_aggregate_table(
            bucket_rows, window_label=last, group_label=group_by
        )
    ]

    if not getattr(args, "no_smells", False):
        aggregate_stanza = _render_aggregate_smells(
            storage=storage,
            since_ms=cutoff_ms,
            window_label=last,
            task_filter=task_filter,
        )
        if aggregate_stanza:
            lines.append("")
            lines.append(aggregate_stanza)

    return "\n".join(lines)


# Maximum number of recent runs the aggregate smell evaluator will
# pull events for. Set high enough to give a meaningful "hit rate %"
# on a busy task without making the cross-run scan unboundedly slow
# on a years-old database. Operators with very large stores can pass
# a tighter ``--last`` window if they want the full picture.
_MAX_AGGREGATE_SMELL_RUNS = 500


def _render_aggregate_smells(
    *,
    storage: "Storage",
    since_ms: int,
    window_label: str,
    task_filter: Optional[str],
) -> str:
    """Render the "Aggregate smells (last 7d):" stanza for the
    aggregate report view.

    Streams up to :data:`_MAX_AGGREGATE_SMELL_RUNS` of the most
    recent runs in the window through
    :meth:`SmellEngine.evaluate_aggregate`, then emits
    ``<id>: <n>/<total> runs (<pct>%)`` per smell id so the eye
    lands on prevalence rather than absolute count.

    The engine sees the runs through a counting generator so we
    never materialise every run's events at once — important for
    multi-turn agents where a single run can carry hundreds of
    event rows.

    Returns ``""`` when no runs matched the window — callers
    decide whether to suppress the blank-line separator.
    """
    from inkfoot.smells import DEFAULT_SMELLS  # noqa: PLC0415
    from inkfoot.smells.engine import SmellEngine  # noqa: PLC0415

    counter = _CountingRunStream(
        _iter_recent_runs_with_events(
            storage=storage,
            since_ms=since_ms,
            task_filter=task_filter,
            limit=_MAX_AGGREGATE_SMELL_RUNS,
        )
    )
    engine = SmellEngine(list(DEFAULT_SMELLS))
    triggered_runs = engine.evaluate_aggregate(counter)
    total_runs = counter.consumed

    if total_runs == 0:
        return ""

    rows = sorted(
        ((sid, len(ids)) for sid, ids in triggered_runs.items()),
        key=lambda row: (-row[1], row[0]),
    )
    if not rows:
        return f"Aggregate smells (last {window_label}): none detected"

    lines = [f"Aggregate smells (last {window_label}):"]
    for smell_id, hit_count in rows:
        pct = (hit_count / total_runs * 100) if total_runs else 0.0
        lines.append(
            f"  · {smell_id}: {hit_count}/{total_runs} runs ({pct:.0f}%)"
        )
    return "\n".join(lines)


class _CountingRunStream:
    """Generator wrapper that counts pairs consumed.

    The engine reads ``(run, events)`` pairs one at a time and
    discards each pair's events before reaching for the next, so
    memory usage is O(1 run) rather than O(all runs). We still
    need the total count to render the ``X/Y runs (Z%)`` line, so
    this small adapter records the number of pairs yielded."""

    def __init__(self, source: Iterable[tuple[dict[str, Any], Iterable[dict[str, Any]]]]) -> None:
        self._source = source
        self.consumed = 0

    def __iter__(self):
        for pair in self._source:
            self.consumed += 1
            yield pair


def _iter_recent_runs_with_events(
    *,
    storage: "Storage",
    since_ms: int,
    task_filter: Optional[str],
    limit: int,
) -> Iterable[tuple[dict[str, Any], Iterable[dict[str, Any]]]]:
    """Yield ``(run_row, events_iter)`` for the most recent runs in
    the window, one at a time.

    Reaches into the SQLite connection directly — the Storage
    Protocol still needs ``recent_runs(since_ms, task, limit)`` /
    ``max_event_rowid()`` / ``events_after_rowid(cursor, task,
    limit)`` so a future Postgres backend can avoid raw-SQL
    special-casing here, in the aggregate-runs SELECT above, and
    in :mod:`inkfoot.cli.tail`. Tracked as a follow-up; the
    current implementation keeps the layering loose so each new
    aggregate-view feature doesn't compound the debt."""
    conn = storage._conn()  # type: ignore[attr-defined]
    where = "started_at >= ?"
    params: list[Any] = [since_ms]
    if task_filter:
        where += " AND task = ?"
        params.append(task_filter)
    cur = conn.execute(
        f"""
        SELECT * FROM runs
        WHERE {where}
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    )
    runs = [dict(row) for row in cur.fetchall()]
    _attach_window_peer_context(runs)
    for run in runs:
        run_id = run.get("id")
        if not run_id:
            continue
        yield run, storage.iter_events(run_id)


def _attach_window_peer_context(runs: list[dict[str, Any]]) -> None:
    """Attach same-task peer-cost context (median + peer count) to
    each run dict so the ``cost-skewed-by-outlier`` smell — whose
    detector is a pure function and never queries storage — can
    fire during the aggregate rollup. Peers are the *other*
    same-task runs in this window slice; the smell stays silent
    below its minimum peer count."""
    from inkfoot.smells.cost_skewed_by_outlier import (  # noqa: PLC0415
        PEER_COUNT_KEY,
        PEER_P50_KEY,
        peer_p50,
    )

    totals_by_task: dict[Any, list[int]] = {}
    for run in runs:
        totals_by_task.setdefault(run.get("task"), []).append(
            int(run.get("total_nanodollars") or 0)
        )
    for run in runs:
        peers = list(totals_by_task[run.get("task")])
        peers.remove(int(run.get("total_nanodollars") or 0))
        run[PEER_P50_KEY] = peer_p50(peers)
        run[PEER_COUNT_KEY] = len(peers)


def _attach_outlier_context(
    storage: "Storage", run: dict[str, Any]
) -> dict[str, Any]:
    """Single-run flavour of :func:`_attach_window_peer_context`:
    fetch the same-task peers' totals (bounded, most recent first)
    and attach the median + count the ``cost-skewed-by-outlier``
    smell reads. Same Storage-Protocol gap as the aggregate view —
    reaches into the SQLite connection directly."""
    from inkfoot.smells.cost_skewed_by_outlier import (  # noqa: PLC0415
        PEER_COUNT_KEY,
        PEER_P50_KEY,
        peer_p50,
    )

    task = run.get("task")
    run_id = run.get("id")
    if not task or not run_id:
        return run
    conn = storage._conn()  # type: ignore[attr-defined]
    cur = conn.execute(
        """
        SELECT total_nanodollars FROM runs
        WHERE task = ? AND id != ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [task, run_id, _MAX_AGGREGATE_SMELL_RUNS],
    )
    totals = [
        int(row["total_nanodollars"] or 0) for row in cur.fetchall()
    ]
    run[PEER_P50_KEY] = peer_p50(totals)
    run[PEER_COUNT_KEY] = len(totals)
    return run
