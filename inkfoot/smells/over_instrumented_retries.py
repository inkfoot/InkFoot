"""``over-instrumented-retries`` smell.

Fires when the run's SDK-level retries average more than 3 per
completed call — the agent is hammering a failing upstream and
paying for every attempt.

**Current approximation.** The event stream has no call-site
identity, so "retries per call" is approximated as *failed calls ÷
completed calls*: every failed ``llm_call`` event (the shim records
SDK errors as calls with an ``error`` payload and an all-zero
ledger) counts as one retry-worthy attempt, and the completed calls
are what the run actually got for them. A run with no completed
calls at all still fires once it racks up enough failures —
``max(1, completed)`` keeps the division honest.

Cost impact: total ``retry_overhead_tokens`` × input price. Today's
translators report that field as 0 (it populates when the retry
classifier ships), so the smell usually fires with no dollar figure
attached — same convention as ``runaway-retry-loop``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "over-instrumented-retries"
_RETRIES_PER_CALL_THRESHOLD = 3.0


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    events = list(events)

    retry_throttle_events = sum(
        1
        for ev in events
        if isinstance(ev, dict) and ev.get("kind") == "retry_throttle"
    )

    failed = 0
    completed = 0
    total_retry_overhead = 0
    first_breach_sequence: Optional[int] = None
    error_types: dict[str, int] = {}
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        last_payload = payload
        ledger = ledger_from_payload(payload)
        total_retry_overhead += ledger.retry_overhead_tokens
        error = payload.get("error")
        if isinstance(error, dict):
            failed += 1
            error_type = error.get("type")
            if isinstance(error_type, str) and error_type:
                error_types[error_type] = error_types.get(error_type, 0) + 1
        else:
            completed += 1
        if (
            first_breach_sequence is None
            and failed / max(1, completed) > _RETRIES_PER_CALL_THRESHOLD
        ):
            first_breach_sequence = int(event.get("sequence", 0) or 0)

    retries_per_call = failed / max(1, completed)
    if retries_per_call <= _RETRIES_PER_CALL_THRESHOLD:
        return None

    # Cost impact: retry overhead × input rate, priced off the LAST
    # call's row. Translators currently report retry_overhead_tokens
    # as 0, so this is usually 0 — the smell still fires on the
    # run-shape signal alone.
    cost_impact_nd = 0
    if last_payload is not None and total_retry_overhead > 0:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = total_retry_overhead * row.input

    return DetectionResult(
        smell=OVER_INSTRUMENTED_RETRIES,
        triggered_at_sequence=first_breach_sequence or 0,
        severity="warn",
        evidence={
            "failed_calls": failed,
            "completed_calls": completed,
            "retries_per_call": round(retries_per_call, 2),
            "threshold_ratio": _RETRIES_PER_CALL_THRESHOLD,
            "retry_throttle_events": retry_throttle_events,
            "error_types": error_types,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


OVER_INSTRUMENTED_RETRIES = CostSmell(
    id=SMELL_ID,
    title="Retries firing far above call rate",
    description=(
        "SDK retries in this run averaged more than 3 per completed "
        "call. The upstream is failing (rate limits, timeouts, "
        "overload) and the agent keeps re-sending the same request "
        "— each attempt re-tokenises the full context, so a "
        "persistent failure multiplies the run's cost without "
        "producing anything. Back off harder, and stop retrying "
        "into an upstream that keeps refusing."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Tune the SDK's backoff (fewer attempts, longer waits) and "
        "circuit-break the upstream after repeated failures. "
        "RetryThrottle enforces a retry budget per window at the "
        "instrumentation layer."
    ),
    suggested_policy="RetryThrottle",
    evidence_query=(
        "SELECT sequence, error.type FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call' "
        "AND error IS NOT NULL"
    ),
    primary_category="retry_overhead_tokens",
)
