"""``unbounded-conversation-history`` smell.

Fires when any single call in the run carries more than 50,000
memory tokens — the conversation history / agent memory block has
grown without bound and is being re-sent on every call. Nothing
trims it, so the context (and the bill) grows linearly with turn
count until the model's window runs out.

The threshold applies to the *largest single call*, not the sum
across calls: history is recycled context, so summing across turns
would count the same tokens once per turn.

Cost impact: the per-call excess above the threshold, summed over
breaching calls, × cache_read price. History is a stable prefix, so
in the best case the provider already serves it from cache —
cache_read is the optimistic floor on what trimming the excess
would recover.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "unbounded-conversation-history"
_MEMORY_TOKENS_THRESHOLD = 50_000


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    max_memory_tokens = 0
    breaching_calls = 0
    excess_memory_tokens = 0
    first_breach_sequence: Optional[int] = None
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        last_payload = payload
        ledger = ledger_from_payload(payload)
        memory = ledger.memory_tokens
        if memory > max_memory_tokens:
            max_memory_tokens = memory
        if memory > _MEMORY_TOKENS_THRESHOLD:
            breaching_calls += 1
            excess_memory_tokens += memory - _MEMORY_TOKENS_THRESHOLD
            if first_breach_sequence is None:
                first_breach_sequence = int(event.get("sequence", 0) or 0)

    if first_breach_sequence is None:
        return None

    # Cost impact: the excess above the threshold × cache_read —
    # the optimistic floor. Even when the provider serves the whole
    # history from cache, the excess still bills at the cache-read
    # rate on every breaching call; trimming it recovers at least
    # this much.
    cost_impact_nd = 0
    if last_payload is not None:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = excess_memory_tokens * row.cache_read

    return DetectionResult(
        smell=UNBOUNDED_CONVERSATION_HISTORY,
        triggered_at_sequence=first_breach_sequence,
        severity="warn",
        evidence={
            "max_memory_tokens": max_memory_tokens,
            "threshold_tokens": _MEMORY_TOKENS_THRESHOLD,
            "breaching_calls": breaching_calls,
            "excess_memory_tokens": excess_memory_tokens,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


UNBOUNDED_CONVERSATION_HISTORY = CostSmell(
    id=SMELL_ID,
    title="Unbounded conversation history",
    description=(
        "At least one call in this run carried more than 50,000 "
        "tokens of conversation history / agent memory. Nothing is "
        "trimming the history, so every additional turn re-sends "
        "the whole transcript and the per-call cost grows linearly "
        "until the context window runs out. Long-running agents "
        "need a memory policy: compress older turns into a summary, "
        "or truncate beyond a fixed window."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Add memory compression — fold older turns into a running "
        "summary — or truncate history beyond a fixed turn window."
    ),
    suggested_policy=None,
    evidence_query=(
        "SELECT sequence, memory_tokens FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call' "
        "ORDER BY memory_tokens DESC"
    ),
    primary_category="memory_tokens",
)
