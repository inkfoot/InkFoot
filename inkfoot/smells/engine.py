"""The :class:`SmellEngine` — orchestrates per-run + cross-run smell
evaluation.

The engine is **lazy and off the hot path** (per the architecture
note's hot-path policy). Reports materialise an event stream from
storage, then call :meth:`evaluate`; the engine never touches
storage itself. This keeps the SDK shim's hot path clean and lets
the same engine plug into a future Cloud dashboard renderer
unchanged.

Each smell's ``detect`` callable returns at most one
:class:`DetectionResult` per call — repeated triggers in a single
run land as a single result with the highest-impact evidence.

Cross-run aggregation (:meth:`evaluate_aggregate`) returns the
``{smell_id: {run_ids}}`` mapping that aggregate reporting
actually consumes — "how many distinct runs fired this smell",
which is the only useful prevalence shape today. The previous
flat-list contract collapsed the run grouping and was the wrong
shape for the renderer; a future Cost Smell Library may surface
genuine cross-run rules under a different method without
breaking this one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.run import Run
    from inkfoot.smells import CostSmell, DetectionResult


_LOG = logging.getLogger("inkfoot.smells.engine")


class SmellEngine:
    """Evaluates a list of :class:`CostSmell` rules against a run.

    Default behaviour uses :data:`DEFAULT_SMELLS`; tests pass an
    explicit list so each smell can be isolated. A failing smell
    (one whose ``detect`` raises) is **isolated**: the engine logs
    the exception at ``WARNING`` and continues with the remaining
    smells. A bug in one smell must not silence the others.
    """

    def __init__(self, smells: Optional[Sequence["CostSmell"]] = None) -> None:
        if smells is None:
            from inkfoot.smells import DEFAULT_SMELLS

            smells = DEFAULT_SMELLS
        self._smells: tuple["CostSmell", ...] = tuple(smells)

    # ------------------------------------------------------------------
    # Per-run evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        run: "Run",
        events: Iterable[dict[str, Any]],
    ) -> list["DetectionResult"]:
        """Run every registered smell over ``(run, events)``. Returns
        the list of fired results in smell-registration order.

        ``events`` is materialised to a tuple up front so each smell
        sees the same snapshot — a smell that consumes the iterator
        can't accidentally starve the next one.
        """
        snapshot = tuple(events)
        results: list["DetectionResult"] = []
        for smell in self._smells:
            try:
                hit = smell.detect(run, snapshot)
            except Exception:  # pylint: disable=broad-except
                _LOG.warning(
                    "smell %s raised during detection on run %s; "
                    "skipping (other smells continue)",
                    smell.id,
                    getattr(run, "id", "<unknown>"),
                    exc_info=True,
                )
                continue
            if hit is not None:
                results.append(hit)
        return results

    # ------------------------------------------------------------------
    # Cross-run aggregation
    # ------------------------------------------------------------------

    def evaluate_aggregate(
        self,
        runs: Iterable[tuple["Run", Iterable[dict[str, Any]]]],
    ) -> dict[str, set[str]]:
        """Aggregate detections across a stream of ``(run, events)``
        pairs.

        Returns ``{smell_id: {run_id, ...}}`` — one entry per smell
        that fired on at least one run, mapped to the set of run
        ids that triggered it. This is the shape aggregate reports
        consume: prevalence ("in how many runs did this smell
        appear?") is the only useful cross-run statistic today, and
        flattening the per-run findings into a single list (the
        previous contract) lost the run grouping it depends on.

        ``runs`` may be a generator. The engine pulls each pair
        lazily and never holds more than one run's events in
        memory at a time.

        Runs without a usable ``id`` (neither attribute nor dict
        key) are silently skipped. Smell detectors that raise are
        isolated per :meth:`evaluate`'s contract — one bad detector
        on one run won't drop the rest of the run's pairs.
        """
        triggered: dict[str, set[str]] = {}
        for run, events in runs:
            run_id = _coerce_run_id(run)
            if not run_id:
                continue
            hits = self.evaluate(run, events)
            for smell_id in {hit.smell.id for hit in hits}:
                triggered.setdefault(smell_id, set()).add(run_id)
        return triggered

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def smells(self) -> tuple["CostSmell", ...]:
        """Read-only view of the registered smells."""
        return self._smells


def _coerce_run_id(run: Any) -> str:
    """Best-effort extraction of ``run.id``.

    The smell engine's contract is intentionally loose about what
    ``run`` is: it accepts the :class:`~inkfoot.run.Run` dataclass
    used in production paths and the plain-dict shape that test
    fixtures emit. Both expose ``id``; we read whichever form is
    available."""
    val = getattr(run, "id", None)
    if val:
        return str(val)
    if isinstance(run, dict):
        val = run.get("id")
        if val:
            return str(val)
    return ""
