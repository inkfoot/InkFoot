"""``oversized-tool-result-recycled`` smell.

Fires when a single LLM call carries more than 2000 tool-result
tokens AND at least 3 turns of the run include any tool-result
tokens. The pattern catches the classic "we keep stuffing the full
tool output back into context every turn" failure mode — the
provider doesn't summarise it; the agent doesn't summarise it; the
bill grows linearly in turn count.

**Current approximation (broader than the ideal).** The ideal
rule — *"a tool result of > 2000 tokens appears in
tool_result_tokens for ≥ 3 turns"* — naturally reads as "the *same*
oversized result appears across 3 turns". Tracking *which* result
is which requires args-or-content identity, which only exists in
``event_contents`` (replay mode). The current metadata-mode
events carry per-call token totals but no result identity, so we
approximate it as: "at least one turn has an oversized
result AND at least N turns have any tool-result tokens at all."

That broader heuristic false-positives on the shape
``[5000, 50, 50]`` — one large result followed by small follow-ups
— which is a legitimately benign pattern. future Cloud replay-mode
upgrade lets us key on a content hash and drop the false positives;
until then the bias is towards alerting over silence.

Cost impact: ``oversized_call.tool_result_tokens × (turns - 1) ×
input_price`` — the tool-result body costs full input rate on every
turn it sticks around. Subtracting one turn (the first appearance)
is the "you'd have paid this anyway once" cost.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "oversized-tool-result-recycled"
_OVERSIZED_THRESHOLD_TOKENS = 2000
_MIN_TURNS_TO_FIRE = 3


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    turns_with_tool_results = 0
    oversized_seen = False
    oversized_tokens = 0
    oversized_sequence: Optional[int] = None
    last_payload: Optional[dict[str, Any]] = None
    total_turns = 0

    for event, payload in iter_llm_call_payloads(events):
        ledger = ledger_from_payload(payload)
        last_payload = payload
        total_turns += 1
        if ledger.tool_result_tokens > 0:
            turns_with_tool_results += 1
        if (
            ledger.tool_result_tokens >= _OVERSIZED_THRESHOLD_TOKENS
            and not oversized_seen
        ):
            oversized_seen = True
            oversized_tokens = ledger.tool_result_tokens
            oversized_sequence = int(event.get("sequence", 0) or 0)

    if not oversized_seen:
        return None
    if turns_with_tool_results < _MIN_TURNS_TO_FIRE:
        return None

    # Cost impact: tokens × (turns - 1) × input rate. "turns - 1"
    # is the "you'd have paid this anyway on the first turn" floor.
    cost_impact_nd = 0
    if last_payload is not None and turns_with_tool_results > 1:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = (
                oversized_tokens
                * (turns_with_tool_results - 1)
                * row.input
            )

    return DetectionResult(
        smell=OVERSIZED_TOOL_RESULT_RECYCLED,
        triggered_at_sequence=oversized_sequence or 0,
        severity="warn",
        evidence={
            "tool_result_tokens_at_breach": oversized_tokens,
            "threshold_tokens": _OVERSIZED_THRESHOLD_TOKENS,
            "turns_with_tool_results": turns_with_tool_results,
            "total_turns": total_turns,
            "min_turns_to_fire": _MIN_TURNS_TO_FIRE,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


OVERSIZED_TOOL_RESULT_RECYCLED = CostSmell(
    id=SMELL_ID,
    title="Oversized tool result recycled across turns",
    description=(
        "A tool result of more than 2000 tokens is sitting in the "
        "context across 3+ turns. The agent is paying full input "
        "rate for that body on every turn it stays in scope. The "
        "right fix is to summarise the tool result before reusing "
        "it — a future CheapSummariser does this automatically."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Summarise large tool results before recycling them across "
        "turns. Enable CheapSummariser(threshold_tokens=1500) once "
        "A future release ships; until then, prune the messages array "
        "manually after each tool invocation."
    ),
    suggested_policy="CheapSummariser",
    evidence_query=(
        "SELECT tool_result_tokens, sequence FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
    primary_category="tool_result_tokens",
)
