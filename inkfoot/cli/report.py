"""``inkfoot report`` — attribution bar chart + smells.

The renderer is a **pure function** of ``(run, ledger_totals,
smells)`` → ``str``. That's the §5.10 contract: no storage I/O, no
side effects, no terminal-width detection. The CLI scaffold below
loads the inputs from storage and hands them to the renderer; the
renderer can be unit-tested without spinning up anything.

Output shape per §5.10:

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
from inkfoot.money import format_usd, nd_to_usd
from inkfoot.pricing import estimate_per_category

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
    # so a tiny category still shows. Mirrors §5.10's expected output.
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
    """Render the §5.10 single-run report as a string.

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

    # Categories sorted by cost descending (§5.10). When cost is
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

    # Footnote when always-zero Phase-0 categories were hidden.
    # We scope this specifically to the three fields that *can't*
    # carry tokens in Phase 0 (summariser/guardrail/retry_overhead);
    # other zero categories (retrieved_context, cache_*, etc.) hide
    # without the footnote because their absence isn't surprising
    # to the reader.
    if not show_zero:
        always_zero_short_labels = sorted(
            _short_label(name)
            for name in _ALWAYS_ZERO_IN_PHASE_0
            if int(ledger_totals.get(name, 0)) == 0
        )
        if always_zero_short_labels:
            lines.append("")
            lines.append(
                f"({_format_list(always_zero_short_labels)} "
                f"are always-zero in Phase 0 — hidden by default)"
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


# Fields that are guaranteed to be zero in Phase 0 (no translator
# populates them; smell engine doesn't synthesise). The footnote
# under the bar chart names just these — listing all hidden
# categories would surprise the reader for retrieved_context (which
# E5's tag_retrieval *does* populate when the user marks it) or
# cache_* (which depend on the provider's behaviour).
_ALWAYS_ZERO_IN_PHASE_0 = (
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
# typer would be cleaner but isn't a hard dep in Phase 0.
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

        if getattr(args, "last", None):
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

        if getattr(args, "group_by", None) == "node":
            print(_render_per_node(row, events))
            return 0

        ledger_totals = _aggregate_ledger_totals(events)
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


def _render_per_node(
    run: dict[str, Any], events: list[dict[str, Any]]
) -> str:
    """Per-node ledger summary for ``inkfoot report --run <id>
    --group-by node`` (ADR-1-1).

    Groups every ``llm_call`` event by its
    ``payload.metadata.node_name`` and emits one row per node with
    fresh-input tokens, output tokens, dollar cost (sum of
    per-category estimates), and call count. Calls without a
    ``node_name`` land under ``(no node)`` — important to show so
    the user spots adapters that aren't tagging their nodes.
    """
    from inkfoot.smells._helpers import (  # noqa: PLC0415
        ledger_from_payload,
    )

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
        node_name = (
            metadata.get("node_name") if isinstance(metadata, dict) else None
        )
        bucket = node_name or "(no node)"
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
        return (
            f"Run {run.get('id') or '?'} · {run.get('task') or '(no task)'}\n"
            "No node-tagged LLM calls in this run.\n"
            "Hint: install a Pattern-C adapter (inkfoot.langgraph.instrument) "
            "or call inkfoot.tag_node('phase') before LLM calls."
        )

    lines = [
        (
            f"Run {run.get('id') or '?'} · "
            f"{run.get('task') or '(no task)'} · per-node ledger"
        ),
        "",
        (
            f"  {'node':<24} {'calls':>6} {'input_tok':>10} "
            f"{'output_tok':>11} {'cost':>10}"
        ),
    ]
    # Sort by nanodollar spend desc so the eye lands on the most
    # expensive node first; ``(no node)`` floats wherever its total
    # places.
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


def _p95(values: list[int]) -> int:
    """Approximate 95th percentile from a sorted sample. SQLite has
    no built-in percentile aggregate (without an extension), so the
    aggregate view pulls per-bucket totals into Python and computes
    p95 here. Index = ``int(n × 0.95)``, clamped at ``n - 1``."""
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * 0.95))
    return s[idx]


def _render_aggregate(storage: "Storage", args: Any) -> str:
    """Cross-run aggregate view (``--last 7d`` / ``--task name``).

    Phase 0 implementation: SELECT recent runs, summarise by
    bucket (``--group-by task`` | ``agent_kind``), emit a table
    with five columns per E5-S3 AC:

    * ``runs`` — count of runs in the bucket
    * ``avg_$`` — average ``total_nanodollars``
    * ``p95_$`` — 95th-percentile ``total_nanodollars``
      (computed in Python; SQLite has no native percentile agg)
    * ``success%`` — outcome="success" rate
    * ``cost/success`` — ``total_$ / n_success`` (or ``—`` when
      no successes in the bucket)
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

    # TODO(phase-2/postgres): the Storage Protocol has no
    # ``aggregate_runs_since`` method yet so we reach into the
    # SQLite connection directly. Phase 2's Postgres backend will
    # need a proper Protocol method; see CL5 review Finding #3.
    conn = storage._conn()  # type: ignore[attr-defined]
    task_filter = getattr(args, "task", None)
    where = "started_at >= ?"
    params: list[Any] = [cutoff_ms]
    if task_filter:
        where += " AND task = ?"
        params.append(task_filter)

    group_by = getattr(args, "group_by", None) or "task"
    if group_by == "node":
        return (
            "inkfoot report: --group-by node only applies to a single "
            "run (pair with --run, not --last). Per-node aggregates "
            "across runs are deferred to Phase 4."
        )
    if group_by not in {"task", "agent_kind"}:
        return (
            f"inkfoot report: invalid --group-by value {group_by!r} "
            f"(expected 'task', 'agent_kind', or 'node' with --run)"
        )

    # Per-bucket aggregates SQLite can compute natively. p95 is
    # computed in Python below from the per-run totals.
    #
    # ``NULLS LAST`` requires SQLite 3.30+ (October 2019). Modern
    # Python ships a newer SQLite; older builder images may need
    # to upgrade. Falling back to ``ORDER BY total_nd IS NULL,
    # total_nd DESC`` would work on older SQLite, but Phase 0's
    # 3.10+ Python floor implies a recent SQLite anyway.
    cur = conn.execute(
        f"""
        SELECT
            {group_by} AS bucket,
            COUNT(*) AS n_runs,
            SUM(total_nanodollars) AS total_nd,
            AVG(total_nanodollars) AS avg_nd,
            SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS n_success
        FROM runs
        WHERE {where}
        GROUP BY bucket
        ORDER BY total_nd DESC NULLS LAST
        """,
        params,
    )
    rows = cur.fetchall()
    if not rows:
        return f"inkfoot report: no runs in the last {last}."

    # Pull per-run totals once so we can compute p95 per-bucket in
    # Python. One extra query rather than one-per-bucket keeps the
    # round-trip count bounded.
    per_bucket_totals: dict[str, list[int]] = {}
    detail_cur = conn.execute(
        f"SELECT {group_by} AS bucket, total_nanodollars FROM runs "
        f"WHERE {where}",
        params,
    )
    for row in detail_cur.fetchall():
        bucket = row["bucket"] or "(none)"
        per_bucket_totals.setdefault(bucket, []).append(
            int(row["total_nanodollars"] or 0)
        )

    lines = [
        f"Recent runs ({last}, grouped by {group_by}):",
        "",
        (
            f"  {'bucket':<32} {'runs':>6} {'avg_$':>10} "
            f"{'p95_$':>10} {'success%':>9} {'cost/success':>14}"
        ),
    ]
    for row in rows:
        bucket = (row["bucket"] or "(none)")[:32]
        n_runs = row["n_runs"] or 0
        avg_nd = int(row["avg_nd"] or 0)
        total_nd = int(row["total_nd"] or 0)
        n_success = row["n_success"] or 0
        success_pct = (n_success / n_runs * 100) if n_runs else 0.0
        p95_nd = _p95(per_bucket_totals.get(bucket, []))
        cost_per_success = (
            format_usd(total_nd // n_success, decimals=4)
            if n_success > 0
            else "—"
        )
        lines.append(
            f"  {bucket:<32} {n_runs:>6} "
            f"{format_usd(avg_nd, decimals=4):>10} "
            f"{format_usd(p95_nd, decimals=4):>10} "
            f"{success_pct:>8.1f}% "
            f"{cost_per_success:>14}"
        )
    return "\n".join(lines)
