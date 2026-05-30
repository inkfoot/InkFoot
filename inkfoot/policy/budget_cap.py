"""``BudgetCap`` — observation-only budget watcher.

Current implementation behaviour (per §5.8 + observe-only policy contract):

* Estimate the call's nanodollar cost in ``before_call``.
* If the run's running total + this call's estimate exceeds
  ``max_nd``, emit a ``budget_warning`` event.
* **Never block** — the current implementation is observe-only. A future release
  ``ContractEnforcer`` will turn the same data into an enforcement
  action.

Running totals are kept on the policy instance (one-policy-per-run
identity isn't enforced, so the policy keys by ``run_id``). We do
*not* read ``runs.total_nanodollars`` because the aggregator is
async and the value is stale by up to the poll interval (default
500 ms) — within that window we'd under-count and miss the budget
breach.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Optional

from inkfoot.pricing import estimate_nanodollars

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.policy import CallContext, PolicyDecision

# Module-level import (deferred for circular reasons).
from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision  # noqa: E402


class BudgetCap(Policy):
    """Emit ``budget_warning`` when a run's cumulative cost crosses
    ``max_nd`` nanodollars.

    ``max_nd`` is the total budget for the *run*, not per-call.

    **Current timing quirk** (Finding #5 in the review):
    ``before_call`` doesn't know the *current* call's cost yet —
    that's populated by the shim's ``after_call`` once the
    translator has run on the response. So the policy fires on the
    call *after* the one that breaches: call N puts the running
    total over ``max_nd`` and call N+1's ``before_call`` is what
    emits ``budget_warning``. Reports still surface the breach
    correctly; the event's ``occurred_at`` just lags by one call.
    a future ``ContractEnforcer`` will gain a pre-call estimator
    and shift the warning to fire on the breach call itself, but
    The current implementation observe-only posture (observe-only policy contract) accepts the lag.

    The policy fires *once* per run — a follow-up call that would
    have also breached doesn't re-fire until the totals are reset
    (next run).
    """

    NAME = "BudgetCap"
    SUPPORTED_PATTERNS = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }

    def __init__(self, max_nd: int) -> None:
        if not isinstance(max_nd, int) or isinstance(max_nd, bool):
            raise TypeError(f"max_nd must be int, got {type(max_nd).__name__}")
        if max_nd < 0:
            raise ValueError(f"max_nd must be non-negative, got {max_nd}")
        self.max_nd = max_nd
        self._totals: dict[str, int] = {}
        self._fired: set[str] = set()
        self._lock = threading.Lock()

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        # ``ctx.estimated_nanodollars`` is populated by the shim
        # *after* the call (we know real token counts then), so on
        # ``before_call`` we use a pre-call rough estimator. The current implementation
        # uses a flat 0 — a future ``ContractEnforcer`` adds a
        # tokeniser-driven pre-call estimate. We rely on ``after_call``
        # in the current implementation to update the running total; ``before_call``
        # just emits the breach event if the *prior* total already
        # exceeds the budget.
        running = self._totals.get(ctx.run_id, 0)
        if running > self.max_nd and ctx.run_id not in self._fired:
            with self._lock:
                if ctx.run_id not in self._fired:
                    self._fired.add(ctx.run_id)
                    return PolicyDecision(
                        action="warn",
                        reason=(
                            f"cumulative cost {running} nd exceeded "
                            f"budget {self.max_nd} nd"
                        ),
                        metadata={
                            "current_total_nd": running,
                            "max_nd": self.max_nd,
                        },
                        emit_event_kind="budget_warning",
                    )
        return PolicyDecision(action="allow")

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        delta = ctx.estimated_nanodollars or 0
        if not isinstance(delta, int) or isinstance(delta, bool):
            return
        with self._lock:
            self._totals[ctx.run_id] = self._totals.get(ctx.run_id, 0) + delta

    # ------------------------------------------------------------------
    # Test helpers — exposed for inspection, not part of the policy
    # surface the shim cares about.
    # ------------------------------------------------------------------

    def current_total(self, run_id: str) -> int:
        with self._lock:
            return self._totals.get(run_id, 0)

    def reset(self) -> None:
        with self._lock:
            self._totals.clear()
            self._fired.clear()
