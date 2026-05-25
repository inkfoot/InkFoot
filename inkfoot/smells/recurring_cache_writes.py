"""``recurring-cache-writes`` smell.

Fires when more than 80% of the run's calls write to the prompt
cache. Cache *writes* are more expensive than fresh input — the
"break-even" math is roughly "after 4 reads, the write pays for
itself"; if you keep writing instead of reading, the cache is
churning and never paying off. The classic cause is a marker
positioned past the unstable section so each call invalidates the
prior write.

Cost impact: ``sum(cache_creation_tokens) × cache_write_premium``,
where ``cache_write_premium = max(0, cache_write_price - input_price)``.
That's the *extra* cost on top of what fresh input would have been.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    cache_write_premium,
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "recurring-cache-writes"
_WRITE_FRACTION_THRESHOLD = 0.80


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    total_calls = 0
    write_calls = 0
    total_creation_tokens = 0
    first_write_sequence: Optional[int] = None
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        ledger = ledger_from_payload(payload)
        last_payload = payload
        total_calls += 1
        if ledger.cache_creation_tokens > 0:
            write_calls += 1
            total_creation_tokens += ledger.cache_creation_tokens
            if first_write_sequence is None:
                first_write_sequence = int(event.get("sequence", 0) or 0)

    if total_calls == 0:
        return None
    write_fraction = write_calls / total_calls
    if write_fraction <= _WRITE_FRACTION_THRESHOLD:
        return None

    cost_impact_nd = 0
    if last_payload is not None and total_creation_tokens > 0:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = total_creation_tokens * cache_write_premium(row)

    return DetectionResult(
        smell=RECURRING_CACHE_WRITES,
        triggered_at_sequence=first_write_sequence or 0,
        severity="warn",
        evidence={
            "calls_with_cache_writes": write_calls,
            "total_calls": total_calls,
            "write_fraction": round(write_fraction, 4),
            "threshold": _WRITE_FRACTION_THRESHOLD,
            "total_cache_creation_tokens": total_creation_tokens,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


RECURRING_CACHE_WRITES = CostSmell(
    id=SMELL_ID,
    title="Recurring cache writes (cache thrashing)",
    description=(
        "More than 80% of this run's calls wrote to the prompt "
        "cache. Writes cost more than fresh input — the break-even "
        "point is roughly 4 reads per write — so a run that keeps "
        "writing is churning the cache without amortising. The "
        "usual cause is a cache_control marker positioned past the "
        "drifting section so each call invalidates the prior write."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Move the cache_control marker earlier in the prompt — "
        "before the unstable section — so the static prefix gets "
        "cached and the per-call drift becomes a normal input read."
    ),
    suggested_policy="CacheControlPlacer",
    evidence_query=(
        "SELECT cache_creation_tokens, sequence FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
)
