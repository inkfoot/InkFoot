"""``expensive-model-low-entropy`` smell.

Fires when an expensive model (Opus, gpt-4o, o-series) is used for
a short non-reasoning response — the kind of call a much cheaper
model would have handled just fine. The classic case is a yes/no
classification, a single-line summary, or a key-extract that
doesn't benefit from frontier-model quality.

Cost impact: ``output_tokens × (model_output_price − haiku_output_price)``
per qualifying call, summed. That's "what would you have paid on
Haiku instead" — clamped at 0 so we never report a negative
impact for a model that's already cheaper than Haiku.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    haiku_output_price_nd,
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "expensive-model-low-entropy"
_LOW_OUTPUT_THRESHOLD_TOKENS = 200
_EXPENSIVE_MODEL_PREFIXES = ("claude-opus", "gpt-4o", "o1", "o3")


def _is_expensive(model: str) -> bool:
    lowered = (model or "").lower()
    return any(lowered.startswith(prefix) for prefix in _EXPENSIVE_MODEL_PREFIXES)


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    qualifying_count = 0
    total_potential_savings_nd = 0
    first_sequence: Optional[int] = None
    sample_model: Optional[str] = None
    sample_output_tokens = 0

    haiku_output = haiku_output_price_nd()

    for event, payload in iter_llm_call_payloads(events):
        model = payload.get("model")
        if not isinstance(model, str) or not _is_expensive(model):
            continue
        ledger = ledger_from_payload(payload)
        if ledger.reasoning_tokens != 0:
            # Real reasoning use justifies the expensive model.
            continue
        if ledger.output_tokens >= _LOW_OUTPUT_THRESHOLD_TOKENS:
            continue
        if ledger.output_tokens == 0:
            # Zero-output calls (errors, blocks) wouldn't have saved
            # anything on a cheaper model either.
            continue

        # **Premium-clamp guard (Finding #1 in the CL4 review).**
        # The prefix match catches gpt-4o-mini too because
        # ``"gpt-4o-mini".startswith("gpt-4o")`` is True, but
        # gpt-4o-mini is already cheaper than Haiku — flagging it
        # as "expensive" would surface a confusing warning that
        # recommends a cheaper model the user is already using.
        # Generalising fix: if the model's output rate isn't above
        # Haiku's, there's no savings to surface and the smell stays
        # silent. This naturally covers any future model that
        # shares an "expensive" prefix but isn't actually pricier.
        row = price_row_for(payload)
        if row is None or haiku_output <= 0:
            # No pricing → can't tell if it's actually expensive;
            # stay silent rather than false-positive.
            continue
        premium = row.output - haiku_output
        if premium <= 0:
            continue

        qualifying_count += 1
        if first_sequence is None:
            first_sequence = int(event.get("sequence", 0) or 0)
            sample_model = model
            sample_output_tokens = ledger.output_tokens

        total_potential_savings_nd += ledger.output_tokens * premium

    if qualifying_count == 0:
        return None

    return DetectionResult(
        smell=EXPENSIVE_MODEL_LOW_ENTROPY,
        triggered_at_sequence=first_sequence or 0,
        severity="info",
        evidence={
            "qualifying_calls": qualifying_count,
            "sample_model": sample_model,
            "sample_output_tokens": sample_output_tokens,
            "low_output_threshold": _LOW_OUTPUT_THRESHOLD_TOKENS,
            "expensive_prefixes": list(_EXPENSIVE_MODEL_PREFIXES),
        },
        estimated_cost_impact_nd=total_potential_savings_nd,
    )


EXPENSIVE_MODEL_LOW_ENTROPY = CostSmell(
    id=SMELL_ID,
    title="Expensive model used for low-entropy output",
    description=(
        "One or more calls in this run used an expensive model "
        "(Opus, gpt-4o, o-series) for a short non-reasoning "
        "response. A cheaper model (Haiku, gpt-4o-mini) would have "
        "produced the same answer for a fraction of the cost. The "
        "smell is purely informational — sometimes the expensive "
        "model is the right call (latency, instruction-following) "
        "— but the cost difference is usually worth a look."
    ),
    severity="info",
    detect=_detect,
    recommendation=(
        "Route short, non-reasoning prompts to a cheaper model. "
        "Phase 2's CheapSummariser does this automatically for "
        "summarisation; for classification + key-extract use a "
        "named-routing wrapper today."
    ),
    suggested_policy=None,  # Phase 0 has no routing policy.
    evidence_query=(
        "SELECT model, output_tokens, reasoning_tokens "
        "FROM events_json WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
    primary_category="output_tokens",
)
