"""Generate a starting-point Token Contract from a task's run history.

``inkfoot contract draft --task <name> --window 30d`` answers the
question a developer hits the moment they want a contract: "what
budget is realistic for this task?" Rather than guess, we read the
runs already recorded for the task, compute robust percentiles over
their cost and shape, and emit a contract that sits a little above the
observed spread:

* ``max_nanodollars`` = p95 cost + 10% headroom
* ``max_llm_calls``   = p99 call count + 1
* ``cache_hit_rate_min`` = p25 cache-hit rate (a floor most runs clear)
* ``required_success_rate`` = observed rate − 1pp tolerance

The output is a comment-annotated YAML draft, never an authority: the
header records the window, the run count, and any cost outliers (runs
above 10× the median) that were *excluded* from the percentiles so a
single pathological run can't inflate the budget. The developer is
expected to read it, adjust, and commit it like any other code.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from inkfoot.benchmark.schema import percentile

# Below this many runs the percentiles are too noisy to trust; we still
# emit a draft but prepend a loud warning so the developer treats the
# numbers as a placeholder rather than a measurement.
MIN_RUNS_FOR_CONFIDENCE = 20

# A run costing more than this multiple of the median is treated as an
# outlier: excluded from the budget percentiles and surfaced in the
# header so the developer can decide whether it's signal or noise.
OUTLIER_MEDIAN_MULTIPLE = 10.0

_WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class DraftError(ValueError):
    """Raised when a draft cannot be produced (bad window, no runs)."""


@dataclass(frozen=True)
class _RunFacts:
    nanodollars: int
    llm_calls: int
    cache_hit_rate: Optional[float]
    succeeded: bool


@dataclass(frozen=True)
class DraftResult:
    """The rendered YAML plus the facts behind it, for testing/inspection."""

    task: str
    window: str
    run_count: int
    outlier_count: int
    yaml_text: str
    low_confidence: bool


def parse_window(raw: str) -> int:
    """Convert ``30d`` / ``24h`` / ``90m`` into a span in seconds."""
    text = (raw or "").strip()
    if len(text) < 2 or text[-1] not in _WINDOW_UNITS or not text[:-1].isdigit():
        raise DraftError(
            f"invalid --window {raw!r}; expected `<n><unit>` where unit is "
            f"one of s/m/h/d (e.g. 30d, 24h, 90m)"
        )
    return int(text[:-1]) * _WINDOW_UNITS[text[-1]]


def collect_run_facts(
    storage: Any, task: str, window_seconds: int, *, now_ms: Optional[int] = None
) -> list[_RunFacts]:
    """Read the finished runs for ``task`` within the trailing window.

    Per-run cost and cache stats come from the ``runs`` aggregate row;
    the LLM-call count is the number of ``llm_call`` events the run
    emitted. Only runs with a recorded outcome are considered, since a
    run still in flight has no meaningful total to learn from.
    """
    conn = storage._conn()  # noqa: SLF001 - SQLite is the only backend today
    floor_ms = (now_ms if now_ms is not None else int(time.time() * 1000)) - (
        window_seconds * 1000
    )
    rows = conn.execute(
        """
        SELECT id, total_nanodollars, total_input_tokens,
               total_cache_read_tokens, total_cache_creation_tokens, outcome
        FROM runs
        WHERE task = ? AND started_at >= ? AND outcome IS NOT NULL
        ORDER BY started_at DESC
        """,
        [task, floor_ms],
    ).fetchall()

    facts: list[_RunFacts] = []
    for row in rows:
        run_id = row[0]
        calls = conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND kind = 'llm_call'",
            [run_id],
        ).fetchone()[0]
        cache_read = int(row[3] or 0)
        denom = cache_read + int(row[4] or 0) + int(row[2] or 0)
        cache_rate = cache_read / denom if denom > 0 else None
        facts.append(
            _RunFacts(
                nanodollars=int(row[1] or 0),
                llm_calls=int(calls or 0),
                cache_hit_rate=cache_rate,
                succeeded=row[5] == "success",
            )
        )
    return facts


def build_draft(task: str, window: str, facts: list[_RunFacts]) -> DraftResult:
    """Compute percentiles and render the YAML draft for ``task``."""
    if not facts:
        raise DraftError(
            f"no completed runs found for task {task!r} in the last {window}. "
            f"Run the agent under instrumentation first, then draft a contract."
        )

    costs = [f.nanodollars for f in facts]
    median_cost = percentile(costs, 50)
    outlier_threshold = median_cost * OUTLIER_MEDIAN_MULTIPLE
    kept = [f for f in facts if f.nanodollars <= outlier_threshold] or facts
    outlier_count = len(facts) - len(kept)

    kept_costs = [f.nanodollars for f in kept]
    # Floor at 1: the loader rejects a 0 ceiling, so a zero-cost history
    # (e.g. unpriced models in local testing) must still round-trip.
    max_nanodollars = max(1, int(round(percentile(kept_costs, 95) * 1.10)))
    max_llm_calls = int(round(percentile([f.llm_calls for f in kept], 99))) + 1

    cache_rates = [f.cache_hit_rate for f in kept if f.cache_hit_rate is not None]
    cache_floor = round(percentile(cache_rates, 25), 2) if cache_rates else None

    observed_rate = sum(1 for f in facts if f.succeeded) / len(facts)
    required_success_rate = max(0.0, round(observed_rate - 0.01, 3))

    low_confidence = len(facts) < MIN_RUNS_FOR_CONFIDENCE
    yaml_text = _render_yaml(
        task=task,
        window=window,
        run_count=len(facts),
        outlier_count=outlier_count,
        outlier_threshold=outlier_threshold,
        max_nanodollars=max_nanodollars,
        max_llm_calls=max_llm_calls,
        cache_floor=cache_floor,
        required_success_rate=required_success_rate,
        low_confidence=low_confidence,
    )
    return DraftResult(
        task=task,
        window=window,
        run_count=len(facts),
        outlier_count=outlier_count,
        yaml_text=yaml_text,
        low_confidence=low_confidence,
    )


def _render_yaml(
    *,
    task: str,
    window: str,
    run_count: int,
    outlier_count: int,
    outlier_threshold: float,
    max_nanodollars: int,
    max_llm_calls: int,
    cache_floor: Optional[float],
    required_success_rate: float,
    low_confidence: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"# Draft Token Contract for task {task!r}.")
    lines.append(
        f"# Generated from {run_count} run(s) over the last {window}. "
        f"Review before committing."
    )
    if low_confidence:
        lines.append(
            f"# WARNING: fewer than {MIN_RUNS_FOR_CONFIDENCE} runs in the "
            f"window — these numbers are a rough placeholder, not a measured "
            f"baseline. Re-draft once more history accumulates."
        )
    if outlier_count:
        lines.append(
            f"# Excluded {outlier_count} cost outlier(s) above "
            f"{int(outlier_threshold)} nanodollars (>{int(OUTLIER_MEDIAN_MULTIPLE)}x "
            f"the median) from the budget percentiles. Inspect them before "
            f"widening the budget to cover them."
        )
    lines.append("")
    lines.append("schema_version: 1")
    lines.append(f"task: {_yaml_scalar(task)}")
    lines.append("budget:")
    lines.append(f"  max_nanodollars: {max_nanodollars}  # p95 + 10% headroom")
    lines.append(f"  max_llm_calls: {max_llm_calls}  # p99 + 1")
    if cache_floor is not None:
        lines.append(f"  cache_hit_rate_min: {cache_floor}  # p25")
    lines.append("outcome:")
    lines.append(
        f"  required_success_rate: {required_success_rate}  # observed - 1pp"
    )
    lines.append("  measure_window_runs: 100")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when YAML would otherwise mis-parse it."""
    if value and all(c.isalnum() or c in "-_." for c in value):
        return value
    return json.dumps(value)
