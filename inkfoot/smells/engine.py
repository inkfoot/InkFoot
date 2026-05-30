"""The :class:`SmellEngine` — orchestrates per-run + cross-run smell
evaluation.

The engine is **lazy and off the hot path** (per §5.9). Reports
materialise an event stream from storage, then call
:meth:`evaluate`; the engine never touches storage itself. This
keeps the SDK shim's hot path clean and lets the same engine plug
into future Cloud dashboard renderer unchanged.

Each smell's ``detect`` callable returns at most one
:class:`DetectionResult` per call — repeated triggers in a single
run land as a single result with the highest-impact evidence. Cross-
run findings (``evaluate_aggregate``) currently re-run per-run
detection and concatenate; future aggregate analysis will add genuinely cross-run
smells (e.g. "this run pattern repeats across N+ runs").
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
    ) -> list["DetectionResult"]:
        """Run :meth:`evaluate` over every ``(run, events)`` pair.

        The current implementation simply concatenates per-run findings — there is no
        "the same smell fires in 80% of runs" cross-run summarising
        yet. future aggregate analysis's Cost Smell Library adds genuine aggregate
        rules; this method's signature is forward-compatible so the
        renderer doesn't have to change.
        """
        out: list["DetectionResult"] = []
        for run, events in runs:
            out.extend(self.evaluate(run, events))
        return out

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def smells(self) -> tuple["CostSmell", ...]:
        """Read-only view of the registered smells."""
        return self._smells
