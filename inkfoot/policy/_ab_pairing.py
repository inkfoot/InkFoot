"""Pure A/B pairing + quality-delta arithmetic for the summariser's
trust mode.

Deliberately free of storage and IO: callers gather per-run
``(branch, outcome, quality_score)`` observations however they like —
synchronously from SQLite today, from a background worker or a
warehouse query tomorrow — and this module only does the comparison.
Keeping the arithmetic pure keeps the execution model swappable.

Branch semantics: ``"A"`` is the control population (raw tool results
kept), ``"B"`` is the treatment (oversized tool results summarised).
A positive ``success_rate_drop`` means the summarised branch
underperforms the raw branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

CONTROL_BRANCH = "A"
TREATMENT_BRANCH = "B"


@dataclass(frozen=True, slots=True)
class ABObservation:
    """One run's contribution to the per-task A/B comparison."""

    run_id: str
    task: str
    branch: str  # "A" (control/raw) | "B" (treatment/summarised)
    outcome: Optional[str]  # success | accepted_answer | failure | human_escalated
    quality_score: Optional[float] = None


@dataclass(frozen=True, slots=True)
class ABQualityDelta:
    """Per-task comparison of the control vs. treatment populations."""

    task: str
    control_runs: int
    treatment_runs: int
    control_success_rate: float
    treatment_success_rate: float
    control_mean_quality: Optional[float]
    treatment_mean_quality: Optional[float]

    @property
    def success_rate_drop(self) -> float:
        """Positive when summarised runs succeed less often than raw
        runs. Expressed as a fraction (0.05 == five percentage
        points)."""
        return self.control_success_rate - self.treatment_success_rate

    @property
    def quality_score_delta(self) -> Optional[float]:
        """control mean quality − treatment mean quality, or ``None``
        when either branch has no scored runs."""
        if self.control_mean_quality is None or self.treatment_mean_quality is None:
            return None
        return self.control_mean_quality - self.treatment_mean_quality


def compute_quality_delta(
    task: str,
    observations: Iterable[ABObservation],
    *,
    min_runs_per_branch: int = 5,
) -> Optional[ABQualityDelta]:
    """Compare control vs. treatment outcomes for ``task``.

    Returns ``None`` until each branch has at least
    ``min_runs_per_branch`` observations with a recorded outcome — a
    one-run sample must not flip a kill-switch.
    """
    if min_runs_per_branch < 1:
        raise ValueError(
            f"min_runs_per_branch must be >= 1, got {min_runs_per_branch}"
        )

    control: list[ABObservation] = []
    treatment: list[ABObservation] = []
    for obs in observations:
        if obs.task != task or not obs.outcome:
            continue
        if obs.branch == CONTROL_BRANCH:
            control.append(obs)
        elif obs.branch == TREATMENT_BRANCH:
            treatment.append(obs)

    if len(control) < min_runs_per_branch or len(treatment) < min_runs_per_branch:
        return None

    def _success_rate(group: list[ABObservation]) -> float:
        return sum(1 for o in group if o.outcome == "success") / len(group)

    def _mean_quality(group: list[ABObservation]) -> Optional[float]:
        scores = [
            o.quality_score
            for o in group
            if isinstance(o.quality_score, (int, float))
        ]
        return sum(scores) / len(scores) if scores else None

    return ABQualityDelta(
        task=task,
        control_runs=len(control),
        treatment_runs=len(treatment),
        control_success_rate=_success_rate(control),
        treatment_success_rate=_success_rate(treatment),
        control_mean_quality=_mean_quality(control),
        treatment_mean_quality=_mean_quality(treatment),
    )
