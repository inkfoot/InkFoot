"""``ContractEnforcer`` — the runtime that turns a contract's degrade
ladder into per-call decisions.

The enforcer is deliberately storage-agnostic so it can be unit-tested
without a database. It owns the *decision*: given a contract and the
running state of a single run, what should happen to the next LLM call
— allow it, warn, switch it to a cheaper model, or block it outright.
The caller (the shim integration in :mod:`inkfoot.contracts.runtime`)
owns the *side effects*: writing the violation events and raising
:class:`~inkfoot.errors.PolicyBlocked` when the decision is to block.

Cost estimation is pessimistic by design. Before a call is made we
can't know its output token count, so we predict it from a per-task
moving average (defaulting to 500). Rounding errors push the estimate
up rather than down: it is better to warn slightly early than to miss
a budget. The degrade ladder fires at coarse percentages (typically
80/90/100) precisely so a noisy estimate still catches a runaway run
before it blows the ceiling.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from inkfoot.contracts.schema import Contract, DegradeAction, DegradeStep
from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import estimate_nanodollars
from inkfoot.tokenisers import tokenise, tokenise_tools

# Output-token count assumed for a task with no observed history. The
# moving average replaces this once real calls have completed.
DEFAULT_OUTPUT_TOKENS = 500

# How much weight a fresh observation carries in the per-task output
# moving average. A light alpha keeps the estimate stable across a
# noisy run while still tracking a genuine shift in output size.
_OUTPUT_EWMA_ALPHA = 0.3


@dataclass(frozen=True)
class ContractViolation:
    """A single budget/outcome breach the enforcer wants recorded.

    ``level`` distinguishes the runtime band (``warn`` / ``degrade`` /
    ``block``) from an advisory ``outcome`` shortfall. ``action`` is the
    degrade action that was applied (``None`` for outcome events, which
    never act). The numeric pair (``projected_value`` / ``threshold``)
    lets a report show "projected $0.052 against a $0.050 ceiling"
    without re-deriving the math.
    """

    level: str
    clause_name: str
    projected_value: float
    threshold: float
    action: Optional[str] = None
    task: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "clause_name": self.clause_name,
            "projected_value": self.projected_value,
            "threshold": self.threshold,
            "task": self.task,
        }


@dataclass(frozen=True)
class EnforcementOutcome:
    """What the enforcer decided for one pending call.

    ``action`` is one of ``allow`` / ``warn`` / ``switch_to_cheap_model``
    / ``block``. ``new_model`` is set only for a switch. ``violations``
    are the events the caller should persist (possibly empty even when
    an action is taken, because each band emits its event only once per
    run).
    """

    action: str = "allow"
    new_model: Optional[str] = None
    violations: tuple[ContractViolation, ...] = ()


ALLOW = EnforcementOutcome(action="allow")


@dataclass
class _RunState:
    """Mutable per-run accounting the enforcer keeps in memory."""

    task: str
    tier: Optional[str] = None
    call_count: int = 0
    spent_nanodollars: int = 0
    fired_percents: set[int] = field(default_factory=set)


class ContractEnforcer:
    """Holds a ``{task: Contract}`` map and decides per-call actions."""

    def __init__(
        self,
        contracts: Mapping[str, Contract],
        *,
        default_cheap_model: Optional[str] = None,
    ) -> None:
        self._contracts = dict(contracts)
        self._default_cheap_model = default_cheap_model
        self._runs: dict[str, _RunState] = {}
        self._output_avg: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Run registration
    # ------------------------------------------------------------------

    def has_contract(self, task: Optional[str]) -> bool:
        return task is not None and task in self._contracts

    def register_run(
        self, run_id: str, task: str, *, tier: Optional[str] = None
    ) -> None:
        """Begin tracking a run. Idempotent — re-registering keeps the
        existing accounting so a re-entrant ``agent_run`` doesn't reset
        a run's spend mid-flight."""
        with self._lock:
            if run_id not in self._runs:
                self._runs[run_id] = _RunState(task=task, tier=tier)

    def release_run(self, run_id: str) -> None:
        """Drop a finished run's accounting. Idempotent."""
        with self._lock:
            self._runs.pop(run_id, None)

    # ------------------------------------------------------------------
    # Per-call decision
    # ------------------------------------------------------------------

    def before_call(
        self,
        *,
        run_id: str,
        task: Optional[str],
        provider: str,
        model: str,
        request_kwargs: Mapping[str, Any],
        tier: Optional[str] = None,
    ) -> EnforcementOutcome:
        """Evaluate the degrade ladder for the next call on ``run_id``.

        Returns :data:`ALLOW` when there's no contract for the task or
        the projected spend is below the lowest ladder rung.
        """
        if task is None or task not in self._contracts:
            return ALLOW
        contract = self._contracts[task]
        budget = contract.resolved_budget(tier)
        if budget is None or not contract.degrade:
            return ALLOW

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                state = _RunState(task=task, tier=tier)
                self._runs[run_id] = state
            spent = state.spent_nanodollars
            prior_calls = state.call_count
            already_fired = set(state.fired_percents)

        estimate = self._estimate_call_nanodollars(
            provider=provider, model=model, request_kwargs=request_kwargs, task=task
        )

        # Evaluate the ladder against whichever budget dimension is most
        # consumed — the one closest to (or past) its ceiling drives the
        # decision. Only the dimensions knowable before the call are
        # considered here; the rest are checked by the CI gate.
        percent, clause_name, projected, threshold = self._worst_dimension(
            budget=budget,
            projected_nanodollars=spent + estimate,
            projected_calls=prior_calls + 1,
        )
        if percent is None:
            return ALLOW

        step = _ladder_step_for(contract.degrade, percent)
        if step is None:
            return ALLOW

        violation = ContractViolation(
            level=_level_for(step.action),
            clause_name=clause_name,
            projected_value=float(projected),
            threshold=float(threshold),
            action=step.action.value,
            task=task,
        )

        # Each ladder rung emits its event once per run; a block,
        # however, must keep refusing every subsequent call.
        emit = step.at_percent not in already_fired
        if emit:
            with self._lock:
                tracked = self._runs.get(run_id)
                if tracked is not None:
                    tracked.fired_percents.add(step.at_percent)

        events = (violation,) if emit else ()

        if step.action is DegradeAction.BLOCK:
            return EnforcementOutcome(action="block", violations=events)
        if step.action is DegradeAction.SWITCH_TO_CHEAP_MODEL:
            target = contract.cheap_model or self._default_cheap_model
            return EnforcementOutcome(
                action="switch_to_cheap_model",
                new_model=target,
                violations=events,
            )
        return EnforcementOutcome(action="warn", violations=events)

    def record_call(
        self,
        *,
        run_id: str,
        nanodollars: Optional[int],
        output_tokens: Optional[int],
        task: Optional[str] = None,
        count_call: bool = True,
    ) -> None:
        """Fold a completed call's actuals into the run's accounting.

        Called after the SDK returns. Updates the run's running spend
        and the per-task output-token moving average that the next
        pre-call estimate draws on. ``count_call=False`` folds the
        spend without advancing ``call_count`` — used for policy
        helper calls that are real money but not agent turns.
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is not None:
                if count_call:
                    state.call_count += 1
                if nanodollars:
                    state.spent_nanodollars += int(nanodollars)
                resolved_task = task or state.task
            else:
                resolved_task = task
            if resolved_task and output_tokens and output_tokens > 0:
                prev = self._output_avg.get(resolved_task)
                if prev is None:
                    self._output_avg[resolved_task] = float(output_tokens)
                else:
                    self._output_avg[resolved_task] = (
                        _OUTPUT_EWMA_ALPHA * output_tokens
                        + (1 - _OUTPUT_EWMA_ALPHA) * prev
                    )

    # ------------------------------------------------------------------
    # Outcome window (advisory)
    # ------------------------------------------------------------------

    def evaluate_outcome_window(
        self, *, task: Optional[str], recent_outcomes: list[str], tier: Optional[str] = None
    ) -> Optional[ContractViolation]:
        """Compare a task's recent success rate to its contract floor.

        ``recent_outcomes`` is the trailing window of outcome strings
        (most-recent-first or last, order doesn't matter) bounded to the
        contract's ``measure_window_runs``. Returns a violation when the
        observed success rate is below ``required_success_rate``, or
        ``None`` otherwise. This is advisory: it never blocks.
        """
        if task is None or task not in self._contracts:
            return None
        outcome_clause = self._contracts[task].resolved_outcome(tier)
        if outcome_clause is None or outcome_clause.required_success_rate is None:
            return None
        window = recent_outcomes[: outcome_clause.measure_window_runs]
        if not window:
            return None
        successes = sum(1 for o in window if o == "success")
        rate = successes / len(window)
        if rate >= outcome_clause.required_success_rate:
            return None
        return ContractViolation(
            level="outcome",
            clause_name="required_success_rate",
            projected_value=rate,
            threshold=outcome_clause.required_success_rate,
            action=None,
            task=task,
        )

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def _estimate_call_nanodollars(
        self,
        *,
        provider: str,
        model: str,
        request_kwargs: Mapping[str, Any],
        task: str,
    ) -> int:
        """Predict one call's billed cost without making it.

        Tokenises the request body (messages + system + tools) for the
        input side and prices a predicted output drawn from the per-task
        moving average. Returns 0 when the model isn't priced — an
        unpriced model can't drive a nanodollar ladder, which is the
        honest behaviour rather than inventing a number.
        """
        input_tokens = self._count_input_tokens(request_kwargs, model)
        tool_tokens = self._count_tool_tokens(request_kwargs, model)
        output_tokens = int(round(self._output_avg.get(task, DEFAULT_OUTPUT_TOKENS)))
        ledger = CausalTokenLedger(
            user_input_tokens=input_tokens,
            tool_schema_tokens=tool_tokens,
            output_tokens=output_tokens,
        )
        nd = estimate_nanodollars(provider, model, ledger)
        return int(nd) if nd is not None else 0

    @staticmethod
    def _count_input_tokens(request_kwargs: Mapping[str, Any], model: str) -> int:
        import json

        parts: list[str] = []
        system = request_kwargs.get("system")
        if isinstance(system, str):
            parts.append(system)
        elif system is not None:
            parts.append(json.dumps(system, default=str, sort_keys=True))
        messages = request_kwargs.get("messages")
        if messages is not None:
            parts.append(json.dumps(messages, default=str, sort_keys=True))
        if not parts:
            return 0
        return tokenise("\n".join(parts), model).value

    @staticmethod
    def _count_tool_tokens(request_kwargs: Mapping[str, Any], model: str) -> int:
        tools = request_kwargs.get("tools")
        if not isinstance(tools, (list, tuple)) or not tools:
            return 0
        return tokenise_tools(list(tools), model).value

    # ------------------------------------------------------------------
    # Ladder maths
    # ------------------------------------------------------------------

    @staticmethod
    def _worst_dimension(
        *,
        budget: Any,
        projected_nanodollars: int,
        projected_calls: int,
    ) -> tuple[Optional[float], str, float, float]:
        """Return the (percent, clause_name, projected, threshold) of the
        budget dimension closest to its ceiling.

        Only dimensions that accumulate before a call is made are
        considered: projected spend and projected call count. Returns a
        ``None`` percent when the contract sets neither.
        """
        candidates: list[tuple[float, str, float, float]] = []
        if budget.max_nanodollars:
            pct = projected_nanodollars / budget.max_nanodollars * 100.0
            candidates.append(
                (pct, "max_nanodollars", projected_nanodollars, budget.max_nanodollars)
            )
        if budget.max_llm_calls:
            pct = projected_calls / budget.max_llm_calls * 100.0
            candidates.append(
                (pct, "max_llm_calls", projected_calls, budget.max_llm_calls)
            )
        if not candidates:
            return None, "", 0.0, 0.0
        return max(candidates, key=lambda c: c[0])


def _ladder_step_for(
    ladder: tuple[DegradeStep, ...], percent: float
) -> Optional[DegradeStep]:
    """The most severe ladder rung whose threshold ``percent`` has
    reached. ``ladder`` is sorted ascending by ``at_percent`` at parse
    time, so we walk it and keep the last rung at or below ``percent``."""
    chosen: Optional[DegradeStep] = None
    for step in ladder:
        if percent >= step.at_percent:
            chosen = step
        else:
            break
    return chosen


def _level_for(action: DegradeAction) -> str:
    if action is DegradeAction.BLOCK:
        return "block"
    if action is DegradeAction.SWITCH_TO_CHEAP_MODEL:
        return "degrade"
    return "warn"
