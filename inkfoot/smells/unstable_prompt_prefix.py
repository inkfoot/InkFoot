"""``unstable-prompt-prefix`` smell.

Fires when more than 10% of the run's system block is *dynamic* —
i.e. the system prompt drifts call-over-call and breaks the
provider's prompt cache. The classic cause is a timestamp /
user-context line embedded inside the system block.

Cost impact: ``system_dynamic_tokens × cache_read_price`` — the
tokens you *would have* served at the cheaper cache-read rate if
the prefix were stable.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "unstable-prompt-prefix"
_DYNAMIC_FRACTION_THRESHOLD = 0.10


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    total_static = 0
    total_dynamic = 0
    first_dynamic_sequence: Optional[int] = None
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        ledger = ledger_from_payload(payload)
        total_static += ledger.system_static_tokens
        total_dynamic += ledger.system_dynamic_tokens
        if (
            ledger.system_dynamic_tokens > 0
            and first_dynamic_sequence is None
        ):
            first_dynamic_sequence = int(event.get("sequence", 0) or 0)
        last_payload = payload

    denominator = total_static + total_dynamic
    if denominator == 0:
        return None
    dynamic_fraction = total_dynamic / denominator
    if dynamic_fraction <= _DYNAMIC_FRACTION_THRESHOLD:
        return None

    # Cost impact: the dynamic tokens are paying full input rate when
    # they could have served at cache_read rate. We approximate using
    # the LAST call's pricing — homogeneous runs are the common case;
    # mixed-provider runs are an open question.
    cost_impact_nd = 0
    if last_payload is not None:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = total_dynamic * row.cache_read

    return DetectionResult(
        smell=UNSTABLE_PROMPT_PREFIX,
        triggered_at_sequence=first_dynamic_sequence or 0,
        severity="warn",
        evidence={
            "system_static_tokens": total_static,
            "system_dynamic_tokens": total_dynamic,
            "dynamic_fraction": round(dynamic_fraction, 4),
            "threshold": _DYNAMIC_FRACTION_THRESHOLD,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


UNSTABLE_PROMPT_PREFIX = CostSmell(
    id=SMELL_ID,
    title="Unstable prompt prefix",
    description=(
        "More than 10% of this run's system block is dynamic. The "
        "provider's prompt cache only fires when the prefix is "
        "byte-identical call-over-call, so every drifting token "
        "costs full input rate instead of the (cheaper) cache-read "
        "rate. The usual culprit is a timestamp or user-context "
        "line embedded inside the system block — move it out into "
        "a normal user message, or split the system block so the "
        "static portion stays cacheable."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Move time-varying content (timestamps, request IDs, "
        "per-call context) out of the system block."
    ),
    suggested_policy="CacheControlPlacer",
    evidence_query=(
        "SELECT system_static_tokens, system_dynamic_tokens "
        "FROM events_json WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
)
