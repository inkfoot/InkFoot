"""``runaway-retry-loop`` smell.

Fires when the same tool is invoked more than 5 times in a single
run — the canonical "agent stuck in a loop" pattern. The ideal detector
would key on ``(tool_name, args_hash)`` so two *different* invocations
of the same tool aren't conflated, but the current metadata-mode
events only carry tool *names* (not args). We match by name alone
and document the args-hash refinement as a future replay-mode
enhancement once ``event_contents`` rows are populated end-to-end.

Cost impact: ``sum(retry_overhead_tokens)`` across all events in
the run. ``retry_overhead_tokens`` isn't populated in the current implementation (no
retry classifier yet — the tag API and framework adapters
land it); when it's 0 we still surface the smell with no dollar
figure rather than nothing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "runaway-retry-loop"
_MAX_CALLS_PER_TOOL = 5


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    tool_counts: Counter[str] = Counter()
    first_breach_sequence: Optional[int] = None
    breach_tool: Optional[str] = None
    total_retry_overhead = 0
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        ledger = ledger_from_payload(payload)
        total_retry_overhead += ledger.retry_overhead_tokens
        last_payload = payload

        tools_called = payload.get("tools_called") or ()
        if not isinstance(tools_called, (list, tuple)):
            continue
        for raw_name in tools_called:
            if not isinstance(raw_name, str) or not raw_name:
                continue
            tool_counts[raw_name] += 1
            if (
                tool_counts[raw_name] > _MAX_CALLS_PER_TOOL
                and first_breach_sequence is None
            ):
                first_breach_sequence = int(event.get("sequence", 0) or 0)
                breach_tool = raw_name

    if breach_tool is None:
        return None

    # Cost impact: sum of retry_overhead_tokens × input rate. When
    # the translator hasn't classified retries yet (the current implementation) this is
    # zero — the smell still fires; the user sees the loop, just
    # without a dollar figure.
    #
    # Scope note: this sums retry
    # overhead across *every* tool in the run, not just the breach
    # tool. In the current implementation retry_overhead_tokens is always 0 so the
    # difference is invisible; once retry classification starts populating it the
    # the report renderer should label the impact line "all retry overhead
    # on the run" rather than "retries from the breach tool", and a
    # follow-up may want to split by tool name to be more precise.
    cost_impact_nd = 0
    if last_payload is not None and total_retry_overhead > 0:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = total_retry_overhead * row.input

    return DetectionResult(
        smell=RUNAWAY_RETRY_LOOP,
        triggered_at_sequence=first_breach_sequence or 0,
        severity="critical",
        evidence={
            "tool_name": breach_tool,
            "call_count": tool_counts[breach_tool],
            "threshold": _MAX_CALLS_PER_TOOL,
            "tool_call_distribution": dict(tool_counts),
            "retry_overhead_tokens": total_retry_overhead,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


RUNAWAY_RETRY_LOOP = CostSmell(
    id=SMELL_ID,
    title="Runaway retry loop",
    description=(
        "The agent invoked the same tool more than 5 times in this "
        "run. The classic cause is a tool whose result the agent "
        "can't interpret, so it keeps retrying with marginally "
        "different inputs. Each retry pays for the full prompt + "
        "tool-result history again — the cost grows quadratically "
        "in the worst case."
    ),
    severity="critical",
    detect=_detect,
    recommendation=(
        "Inspect the tool's output for ambiguity or a missing exit "
        "condition in the agent's loop. Add a guard that breaks the "
        "loop after N attempts or escalates to a human."
    ),
    suggested_policy="RetryThrottle",
    evidence_query=(
        "SELECT tools_called, sequence FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
)
