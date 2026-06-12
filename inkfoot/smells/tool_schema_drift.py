"""``tool-schema-drift`` smell.

Fires when the run's tool-schema fingerprint changes mid-run — i.e.
the set of tools the framework compiled changed between calls. Tool
schemas serialise near the top of the request body, so a mid-run
change breaks the provider's prompt cache for every call from that
point on: the schema block (and everything after it) re-tokenises
at full input rate instead of being served from cache.

The detector keys on ``metadata.tools_fingerprint`` — the stable
hash adapters stamp per call — rather than on ``tools_offered``,
because policies like LazyToolExposure legitimately narrow the
per-call tool list without changing the underlying schema. Runs
whose calls carry no fingerprint (raw SDK shims without an adapter)
stay silent.

Cost impact: ``tool_schema_tokens`` summed over the calls at and
after the first fingerprint change, × cache_read price — the
optimistic floor convention: even served perfectly from cache,
those schema tokens would still cost this much.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult
from inkfoot.smells._helpers import (
    iter_llm_call_payloads,
    ledger_from_payload,
    price_row_for,
)


SMELL_ID = "tool-schema-drift"


def _fingerprint_of(payload: dict[str, Any]) -> Optional[str]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    fingerprint = metadata.get("tools_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        return fingerprint
    return None


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    fingerprints_seen: list[str] = []  # distinct, in first-seen order
    first_change_sequence: Optional[int] = None
    schema_tokens_after_change = 0
    calls_after_change = 0
    last_payload: Optional[dict[str, Any]] = None

    for event, payload in iter_llm_call_payloads(events):
        last_payload = payload
        fingerprint = _fingerprint_of(payload)
        if fingerprint is None:
            continue
        if fingerprint not in fingerprints_seen:
            fingerprints_seen.append(fingerprint)
            if len(fingerprints_seen) == 2:
                first_change_sequence = int(event.get("sequence", 0) or 0)
        if first_change_sequence is not None:
            ledger = ledger_from_payload(payload)
            schema_tokens_after_change += ledger.tool_schema_tokens
            calls_after_change += 1

    if len(fingerprints_seen) < 2:
        return None

    # Cost impact: schema tokens from the first change onward ×
    # cache_read. Same optimistic-floor convention as
    # unstable-prompt-prefix: a lower bound on the recoverable
    # cost, priced off the LAST call's row (homogeneous runs are
    # the common case).
    cost_impact_nd = 0
    if last_payload is not None:
        row = price_row_for(last_payload)
        if row is not None:
            cost_impact_nd = schema_tokens_after_change * row.cache_read

    return DetectionResult(
        smell=TOOL_SCHEMA_DRIFT,
        triggered_at_sequence=first_change_sequence or 0,
        severity="warn",
        evidence={
            "distinct_fingerprints": len(fingerprints_seen),
            "fingerprints": fingerprints_seen,
            "first_change_sequence": first_change_sequence,
            "calls_after_change": calls_after_change,
            "tool_schema_tokens_after_change": schema_tokens_after_change,
        },
        estimated_cost_impact_nd=cost_impact_nd,
    )


TOOL_SCHEMA_DRIFT = CostSmell(
    id=SMELL_ID,
    title="Tool schema drift mid-run",
    description=(
        "The tool-schema fingerprint changed partway through this "
        "run — tools were added, removed, or reordered between "
        "calls. Tool schemas serialise near the top of the request "
        "body, so every call after the change misses the provider's "
        "prompt cache on the schema block and pays full input rate "
        "for it. Keep the tool set stable for the lifetime of a run: "
        "register every tool up front and keep the ordering "
        "deterministic."
    ),
    severity="warn",
    detect=_detect,
    recommendation=(
        "Stabilise tool ordering and avoid adding tools mid-run. If "
        "the agent only needs a subset of tools per call, narrow "
        "exposure with LazyToolExposure instead of mutating the "
        "registered set."
    ),
    suggested_policy="LazyToolExposure",
    evidence_query=(
        "SELECT sequence, metadata.tools_fingerprint, "
        "tool_schema_tokens FROM events_json "
        "WHERE run_id = :run_id AND kind = 'llm_call'"
    ),
    primary_category="tool_schema_tokens",
)
