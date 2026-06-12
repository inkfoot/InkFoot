"""Cost-per-success rollup for the aggregate report view.

``inkfoot report --last 7d`` answers "what does one *successful*
run actually cost?" — bucket spend divided by successful runs, so
the money burned on failures and retries along the way is folded
into the headline number instead of hidden behind a per-run
average. ``cost/success`` is therefore the leftmost metric column
in the table; ``avg_$`` / ``p95_$`` stay for distribution shape.

Two outcome-derived cost columns:

* ``cost/success`` — bucket spend ÷ runs that called
  ``set_outcome("success")``.
* ``cost/accepted_answer`` — bucket spend ÷ runs that called
  ``set_outcome("accepted_answer")``, the human-accepted tier for
  review workflows.

Runs that never called ``set_outcome`` carry no outcome. Averaging
them into their bucket would silently corrupt ``cost/success`` —
their spend counts while their successes can't — so the rollup
diverts them into a dedicated ``uninstrumented`` row instead:
visible, separately totalled, excluded from every outcome rate.
The row doubles as a coverage gauge — a fat ``uninstrumented``
bucket means the fleet isn't reporting outcomes yet.

Everything here is pure Python over run-row dicts — no SQL, no
storage handle — so the same rollup serves any backend's window
query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

from inkfoot.money import format_usd


# Synthetic bucket label for runs that never called set_outcome.
UNINSTRUMENTED_BUCKET = "uninstrumented"

SUCCESS_OUTCOME = "success"
ACCEPTED_ANSWER_OUTCOME = "accepted_answer"

_NO_VALUE = "—"


def _p95(values: list[int]) -> int:
    """Approximate 95th percentile from a sample. SQLite has no
    built-in percentile aggregate (without an extension), so the
    aggregate view computes p95 in Python from per-run totals.
    Index = ``int(n × 0.95)``, clamped at ``n - 1``."""
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * 0.95))
    return s[idx]


@dataclass(frozen=True, slots=True)
class BucketRow:
    """One row of the aggregate table, fully computed.

    ``uninstrumented`` marks the synthetic bucket for runs with no
    outcome; it renders ``—`` in every outcome-derived column and
    pins to the bottom of the table regardless of spend.
    """

    bucket: str
    n_runs: int
    total_nanodollars: int
    avg_nanodollars: int
    p95_nanodollars: int
    n_success: int
    n_accepted_answer: int
    uninstrumented: bool = False

    @property
    def cost_per_success_nd(self) -> Optional[int]:
        if self.n_success <= 0:
            return None
        return self.total_nanodollars // self.n_success

    @property
    def cost_per_accepted_answer_nd(self) -> Optional[int]:
        if self.n_accepted_answer <= 0:
            return None
        return self.total_nanodollars // self.n_accepted_answer

    @property
    def success_rate(self) -> Optional[float]:
        """Fraction of the bucket's runs with outcome ``success``.
        ``None`` for the uninstrumented bucket — a rate over runs
        that never reported an outcome would be noise."""
        if self.uninstrumented or self.n_runs <= 0:
            return None
        return self.n_success / self.n_runs


def rollup_cost_per_success(
    runs: Iterable[dict[str, Any]],
    *,
    bucket_of: Callable[[dict[str, Any]], str],
) -> list[BucketRow]:
    """Roll run rows up into :class:`BucketRow` records.

    ``runs`` is any iterable of run-row dicts carrying ``outcome``
    and ``total_nanodollars`` (the ``runs``-table projections).
    ``bucket_of`` maps an *outcome-bearing* run to its bucket label
    — a column value (``run["task"]``), a tag value, whatever the
    caller groups by. Runs without an outcome never reach
    ``bucket_of``; they aggregate under
    :data:`UNINSTRUMENTED_BUCKET`.

    Rows come back sorted by spend descending (ties broken by
    bucket name), with the uninstrumented row pinned last.
    """
    grouped: dict[tuple[bool, str], dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        outcome = run.get("outcome")
        uninstrumented = not isinstance(outcome, str) or not outcome
        label = (
            UNINSTRUMENTED_BUCKET
            if uninstrumented
            else str(bucket_of(run))
        )
        agg = grouped.setdefault(
            (uninstrumented, label),
            {"totals": [], "n_success": 0, "n_accepted": 0},
        )
        total = run.get("total_nanodollars")
        if isinstance(total, bool) or not isinstance(total, int):
            total = 0
        agg["totals"].append(total)
        if outcome == SUCCESS_OUTCOME:
            agg["n_success"] += 1
        elif outcome == ACCEPTED_ANSWER_OUTCOME:
            agg["n_accepted"] += 1

    rows: list[BucketRow] = []
    for (uninstrumented, label), agg in grouped.items():
        totals: list[int] = agg["totals"]
        n_runs = len(totals)
        total_nd = sum(totals)
        rows.append(
            BucketRow(
                bucket=label,
                n_runs=n_runs,
                total_nanodollars=total_nd,
                avg_nanodollars=total_nd // n_runs if n_runs else 0,
                p95_nanodollars=_p95(totals),
                n_success=agg["n_success"],
                n_accepted_answer=agg["n_accepted"],
                uninstrumented=uninstrumented,
            )
        )
    rows.sort(
        key=lambda row: (
            row.uninstrumented,
            -row.total_nanodollars,
            row.bucket,
        )
    )
    return rows


def render_aggregate_table(
    rows: Sequence[BucketRow],
    *,
    window_label: str,
    group_label: str,
) -> str:
    """Render the aggregate table. Pure: same inputs, same string.

    Column order puts ``cost/success`` (and its review-workflow
    sibling ``cost/accepted_answer``) directly after the run count
    — the headline metrics — with ``avg_$`` / ``p95_$`` /
    ``success%`` trailing for distribution shape.
    """
    lines = [
        f"Recent runs ({window_label}, grouped by {group_label}):",
        "",
        (
            f"  {'bucket':<32} {'runs':>6} {'cost/success':>13} "
            f"{'cost/accepted_answer':>21} {'avg_$':>10} "
            f"{'p95_$':>10} {'success%':>9}"
        ),
    ]
    for row in rows:
        rate = row.success_rate
        lines.append(
            f"  {row.bucket[:32]:<32} {row.n_runs:>6} "
            f"{_money_or_dash(row.cost_per_success_nd):>13} "
            f"{_money_or_dash(row.cost_per_accepted_answer_nd):>21} "
            f"{format_usd(row.avg_nanodollars, decimals=4):>10} "
            f"{format_usd(row.p95_nanodollars, decimals=4):>10} "
            + (
                f"{rate * 100:>8.1f}%"
                if rate is not None
                else f"{_NO_VALUE:>9}"
            )
        )
    return "\n".join(lines)


def _money_or_dash(nd: Optional[int]) -> str:
    if nd is None:
        return _NO_VALUE
    return format_usd(nd, decimals=4)
