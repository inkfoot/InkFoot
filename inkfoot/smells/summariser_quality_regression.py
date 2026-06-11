"""``summariser-quality-regression`` smell.

Fires when a run carries a ``summariser_quality_regression`` event —
written by :class:`~inkfoot.policy.CheapSummariser` (trust mode) at
the moment its A/B comparison showed the summarised branch's success
rate trailing the raw branch by more than the configured threshold.

Unlike the other smells this one doesn't re-derive anything from the
``llm_call`` stream: the policy already did the population comparison
across runs (a single run can't witness a cross-run regression), so
the detector simply surfaces the recorded finding in the report for
the run where the kill-switch flipped.

There is no per-run dollar impact to estimate — the cost of the
regression is degraded *quality*, not tokens — so
``estimated_cost_impact_nd`` stays 0.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from inkfoot.smells import CostSmell, DetectionResult


SMELL_ID = "summariser-quality-regression"

_EVENT_KIND = "summariser_quality_regression"

# Payload keys (written by CheapSummariser) copied into the evidence
# dict when present. Listed explicitly so a payload-shape drift shows
# up as missing evidence rather than a crash.
_EVIDENCE_KEYS = (
    "task",
    "control_runs",
    "treatment_runs",
    "control_success_rate",
    "treatment_success_rate",
    "success_rate_drop",
    "quality_score_delta",
    "threshold",
)


def _detect(run: Any, events: Iterable[dict[str, Any]]) -> Optional[DetectionResult]:
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != _EVENT_KIND:
            continue
        try:
            payload = json.loads(ev.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        evidence = {k: payload.get(k) for k in _EVIDENCE_KEYS if k in payload}
        return DetectionResult(
            smell=SUMMARISER_QUALITY_REGRESSION,
            triggered_at_sequence=int(ev.get("sequence", 0) or 0),
            severity="critical",
            evidence=evidence,
        )
    return None


SUMMARISER_QUALITY_REGRESSION = CostSmell(
    id=SMELL_ID,
    title="Summariser quality regression",
    description=(
        "Runs where oversized tool results were summarised succeed "
        "measurably less often than runs that kept the raw results. "
        "The summariser is trading away task quality for token "
        "savings; it has been auto-disabled for this task."
    ),
    severity="critical",
    detect=_detect,
    recommendation=(
        "Inspect the summarised tool results preserved in the event "
        "log to see what the summaries dropped. Raise the summariser's "
        "threshold_tokens or max_summary_tokens so more context "
        "survives, or leave it disabled for this task. Re-enable with "
        "inkfoot.policy.cheap_summariser.enable_summariser_for_task() "
        "once the configuration changes."
    ),
    suggested_policy=None,
    evidence_query=(
        "SELECT payload_json FROM events "
        "WHERE run_id = :run_id AND kind = 'summariser_quality_regression'"
    ),
    primary_category="summariser_tokens",
)
