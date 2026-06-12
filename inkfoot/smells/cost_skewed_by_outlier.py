"""``cost-skewed-by-outlier`` smell.

Fires when a single run costs more than 10× the median (p50) of its
task's recent runs — the "one run is dragging the whole task's
average" pattern that makes per-task aggregates lie.

This is the first *cross-run* smell, and the engine deliberately
never touches storage (detectors are pure functions of one run +
its events). The peer context therefore arrives as enrichment keys
the loading layer attaches to the run dict before evaluation:

* :data:`PEER_P50_KEY` — the median ``total_nanodollars`` across
  the task's recent peer runs (excluding the run under test).
* :data:`PEER_COUNT_KEY` — how many peer runs that median is
  computed from.

``inkfoot report`` attaches both (from the rows it already fetched
in the aggregate view; via a bounded peer query in the single-run
view). Without the keys — e.g. a live in-process evaluation — the
smell stays silent rather than guessing.

Cost impact: ``run_cost − peer_p50`` — the excess nanodollars above
the task's typical run. Direct dollars, no rate conversion needed.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import iter_llm_call_payloads


SMELL_ID = "cost-skewed-by-outlier"
_OUTLIER_RATIO_THRESHOLD = 10.0

# Enrichment keys the loading layer attaches to the run dict.
PEER_P50_KEY = "task_peer_p50_nanodollars"
PEER_COUNT_KEY = "task_peer_count"

# Below this many peers the median is too noisy to call anything an
# outlier — a task's second-ever run is often 10× its first.
MIN_PEER_RUNS = 5


def peer_p50(totals: Sequence[int]) -> int:
    """Median of peer run totals, by the same index convention the
    report renderer uses for p95 (``int(n * 0.50)`` clamped) so the
    two stats stay comparable in one table."""
    if not totals:
        return 0
    ordered = sorted(totals)
    idx = min(len(ordered) - 1, int(len(ordered) * 0.50))
    return ordered[idx]


def _enrichment(run: Any) -> tuple[Optional[int], int]:
    if not isinstance(run, dict):
        return None, 0
    p50 = run.get(PEER_P50_KEY)
    count = run.get(PEER_COUNT_KEY)
    if isinstance(p50, bool) or not isinstance(p50, int):
        return None, 0
    if isinstance(count, bool) or not isinstance(count, int):
        return None, 0
    return p50, count


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    p50, peer_count = _enrichment(run)
    if p50 is None or p50 <= 0 or peer_count < MIN_PEER_RUNS:
        return None

    # The run's own cost comes from the event log (the source of
    # truth); the runs-table total is a fallback for event streams
    # captured without per-call estimates.
    run_cost_nd = 0
    costliest_sequence = 0
    costliest_nd = -1
    for event, payload in iter_llm_call_payloads(events):
        estimate = payload.get("estimated_nanodollars")
        if isinstance(estimate, bool) or not isinstance(estimate, int):
            continue
        run_cost_nd += estimate
        if estimate > costliest_nd:
            costliest_nd = estimate
            costliest_sequence = int(event.get("sequence", 0) or 0)
    if run_cost_nd == 0:
        total = run.get("total_nanodollars")
        if not isinstance(total, bool) and isinstance(total, int):
            run_cost_nd = total

    if run_cost_nd <= p50 * _OUTLIER_RATIO_THRESHOLD:
        return None

    return DetectionResult(
        smell=COST_SKEWED_BY_OUTLIER,
        triggered_at_sequence=costliest_sequence,
        severity="warn",
        evidence={
            "run_cost_nanodollars": run_cost_nd,
            "task_peer_p50_nanodollars": p50,
            "task_peer_count": peer_count,
            "ratio": round(run_cost_nd / p50, 2),
            "threshold_ratio": _OUTLIER_RATIO_THRESHOLD,
        },
        estimated_cost_impact_nd=max(0, run_cost_nd - p50),
    )


COST_SKEWED_BY_OUTLIER = CostSmell(
    id=SMELL_ID,
    title="Cost skewed by outlier run",
    description=(
        "This run cost more than 10× the median of its task's "
        "recent runs. One run like this drags the task's average "
        "far above what a typical run costs, so per-task aggregates "
        "stop reflecting reality. The usual causes are a retry "
        "storm, a runaway loop, or an unusually large input that "
        "deserves its own task name."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Investigate what made this run exceptional, and consider "
        "enforcing a BudgetCap so a single run cannot overshoot the "
        "task's typical cost unbounded."
    ),
    suggested_policy="BudgetCap",
    evidence_query=(
        "SELECT id, total_nanodollars FROM runs "
        "WHERE task = :task ORDER BY started_at DESC"
    ),
    primary_category=None,
)
