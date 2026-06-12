"""``summariser-not-firing`` smell.

Fires when the run keeps carrying big tool results — three or more
calls each with over 2000 tool-result tokens — and shows no sign of
a summariser anywhere in its event stream. The agent is paying full
input rate for raw tool output that a summariser would have folded
into a few hundred tokens.

**What counts as "a summariser".** The event stream can't see your
process config, only what ran, so "not configured" is approximated
as "no summariser activity in this run": no call stamped with the
``summariser_call`` metadata flag (the shim marks folded helper
calls with it), no ``summariser_tokens`` anywhere in the ledgers,
and no CheapSummariser policy events (``summariser_replaced`` /
``summariser_ab_assignment`` / ``summariser_quality_regression``).
A configured summariser whose threshold is set so high it never
fires is indistinguishable from no summariser — which is exactly
the situation worth flagging.

Sibling smell: ``oversized-tool-result-recycled`` catches one big
result being recycled across turns; this smell catches the broader
pattern of consistently oversized results with no summarisation in
place.

Cost impact: the per-call excess above the threshold, summed over
oversized calls, × input price — the tokens a summariser would have
removed, billed at the full rate they cost today (same convention
as ``oversized-tool-result-recycled``).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "summariser-not-firing"
_OVERSIZED_THRESHOLD_TOKENS = 2000
_MIN_OVERSIZED_CALLS = 3

# Event kinds CheapSummariser writes; any of them proves a
# summariser is active on this run. Literals mirror the policy's
# emit calls (the house pattern — smells copy event-shape literals
# rather than importing from the policy/shim layers).
_SUMMARISER_EVENT_KINDS = frozenset(
    {
        "summariser_replaced",
        "summariser_ab_assignment",
        "summariser_quality_regression",
    }
)
_SUMMARISER_CALL_METADATA_KEY = "summariser_call"


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    events = list(events)

    for ev in events:
        if isinstance(ev, dict) and ev.get("kind") in _SUMMARISER_EVENT_KINDS:
            return None

    oversized_calls = 0
    excess_tool_result_tokens = 0
    max_tool_result_tokens = 0
    first_breach_sequence: Optional[int] = None
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        last_payload = payload
        ledger = ledger_from_payload(payload)
        if ledger.summariser_tokens > 0:
            return None
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get(
            _SUMMARISER_CALL_METADATA_KEY
        ):
            return None
        tool_result = ledger.tool_result_tokens
        if tool_result > max_tool_result_tokens:
            max_tool_result_tokens = tool_result
        if tool_result > _OVERSIZED_THRESHOLD_TOKENS:
            oversized_calls += 1
            excess_tool_result_tokens += (
                tool_result - _OVERSIZED_THRESHOLD_TOKENS
            )
            if oversized_calls == _MIN_OVERSIZED_CALLS:
                first_breach_sequence = int(event.get("sequence", 0) or 0)

    if oversized_calls < _MIN_OVERSIZED_CALLS:
        return None

    # Cost impact: the excess a summariser would have removed ×
    # input rate, priced off the LAST call's row (homogeneous runs
    # are the common case).
    cost_impact_nd = 0
    if last_payload is not None:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = excess_tool_result_tokens * row.input

    return DetectionResult(
        smell=SUMMARISER_NOT_FIRING,
        triggered_at_sequence=first_breach_sequence or 0,
        severity="warn",
        evidence={
            "oversized_calls": oversized_calls,
            "threshold_tokens": _OVERSIZED_THRESHOLD_TOKENS,
            "min_oversized_calls": _MIN_OVERSIZED_CALLS,
            "max_tool_result_tokens": max_tool_result_tokens,
            "excess_tool_result_tokens": excess_tool_result_tokens,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


SUMMARISER_NOT_FIRING = CostSmell(
    id=SMELL_ID,
    title="Summariser not firing on oversized tool results",
    description=(
        "Three or more calls in this run each carried over 2000 "
        "tokens of raw tool results, and no summariser activity "
        "appears anywhere in the run. Every oversized result is "
        "billed at full input rate on every call it stays in "
        "context. A summariser folds those bodies into a few "
        "hundred tokens before they re-enter the conversation."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Enable CheapSummariser(threshold_tokens=1500) so oversized "
        "tool results are folded into short summaries before they "
        "recycle through the context."
    ),
    suggested_policy="CheapSummariser",
    evidence_query=(
        "SELECT sequence, tool_result_tokens, summariser_tokens "
        "FROM events_json WHERE run_id = :run_id "
        "AND kind = 'llm_call'"
    ),
    primary_category="tool_result_tokens",
)
