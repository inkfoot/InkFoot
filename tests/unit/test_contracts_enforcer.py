"""Tests for the storage-agnostic ContractEnforcer decision engine."""

from __future__ import annotations

import pytest

from inkfoot.contracts.enforcer import ContractEnforcer
from inkfoot.contracts.schema import (
    BudgetClause,
    Contract,
    DegradeAction,
    DegradeStep,
    OutcomeClause,
)


def _contract(
    *,
    max_llm_calls: int | None = None,
    max_nanodollars: int | None = None,
    ladder: list[tuple[int, DegradeAction]] | None = None,
    cheap_model: str | None = None,
    required_success_rate: float | None = None,
    window: int = 100,
) -> Contract:
    steps = tuple(
        DegradeStep(at_percent=p, action=a) for p, a in (ladder or [])
    )
    return Contract(
        schema_version=1,
        task="triage",
        budget=BudgetClause(
            max_llm_calls=max_llm_calls, max_nanodollars=max_nanodollars
        ),
        outcome=OutcomeClause(
            required_success_rate=required_success_rate,
            measure_window_runs=window,
        ),
        degrade=steps,
        cheap_model=cheap_model,
    )


def _enforcer(contract: Contract) -> ContractEnforcer:
    return ContractEnforcer({contract.task: contract})


def _call(enforcer: ContractEnforcer, run_id: str = "r1"):
    return enforcer.before_call(
        run_id=run_id,
        task="triage",
        provider="anthropic",
        model="claude-sonnet-4-6",
        request_kwargs={"messages": [{"role": "user", "content": "hi"}]},
    )


# ----------------------------------------------------------------------
# Degrade ladder on the call-count dimension (deterministic — no pricing)
# ----------------------------------------------------------------------


def test_ladder_fires_warn_switch_block_at_80_90_100() -> None:
    # 10 calls allowed → projected call N hits N*10%.
    contract = _contract(
        max_llm_calls=10,
        cheap_model="claude-haiku-4-5",
        ladder=[
            (80, DegradeAction.WARN),
            (90, DegradeAction.SWITCH_TO_CHEAP_MODEL),
            (100, DegradeAction.BLOCK),
        ],
    )
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")

    actions = []
    for _ in range(10):
        outcome = _call(enforcer)
        actions.append(outcome.action)
        # Record a zero-cost completed call so the call_count advances.
        enforcer.record_call(
            run_id="r1", nanodollars=0, output_tokens=200, task="triage"
        )

    # Projected call counts: 1..10 → percent 10..100.
    # 80% first reached at projected-call 8 (index 7).
    assert actions[6] == "allow"  # projected 70%
    assert actions[7] == "warn"  # projected 80%
    assert actions[8] == "switch_to_cheap_model"  # projected 90%
    assert actions[9] == "block"  # projected 100%


def test_switch_rewrites_model_via_outcome_new_model() -> None:
    contract = _contract(
        max_llm_calls=1,
        cheap_model="claude-haiku-4-5",
        ladder=[(90, DegradeAction.SWITCH_TO_CHEAP_MODEL)],
    )
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")
    outcome = _call(enforcer)
    assert outcome.action == "switch_to_cheap_model"
    assert outcome.new_model == "claude-haiku-4-5"


def test_block_decision_returned_when_at_ceiling() -> None:
    contract = _contract(
        max_llm_calls=1,
        ladder=[(100, DegradeAction.BLOCK)],
    )
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")
    outcome = _call(enforcer)
    assert outcome.action == "block"


def test_each_rung_emits_event_once_but_block_repeats() -> None:
    contract = _contract(
        max_llm_calls=1,
        ladder=[(80, DegradeAction.WARN), (100, DegradeAction.BLOCK)],
    )
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")
    # First call already projects 100% (1/1) → block, one event.
    first = _call(enforcer)
    second = _call(enforcer)
    assert first.action == "block" and len(first.violations) == 1
    # Block keeps refusing, but the event isn't re-emitted for the same rung.
    assert second.action == "block" and len(second.violations) == 0


def test_no_contract_allows() -> None:
    enforcer = ContractEnforcer({})
    outcome = enforcer.before_call(
        run_id="r1",
        task="unknown",
        provider="anthropic",
        model="claude-sonnet-4-6",
        request_kwargs={},
    )
    assert outcome.action == "allow"


def test_record_call_count_call_false_folds_spend_without_counting() -> None:
    """Policy helper calls fold their real spend but don't advance the
    call count or the per-task output average."""
    contract = _contract(max_llm_calls=2, ladder=[(100, DegradeAction.BLOCK)])
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")

    enforcer.record_call(
        run_id="r1",
        nanodollars=5_000,
        output_tokens=None,
        task="triage",
        count_call=False,
    )

    state = enforcer._runs["r1"]
    assert state.call_count == 0
    assert state.spent_nanodollars == 5_000
    assert "triage" not in enforcer._output_avg

    # The next agent call projects 1/2 = 50% — the helper left no trace
    # on the call-count dimension.
    assert _call(enforcer).action == "allow"


# ----------------------------------------------------------------------
# Pre-call cost estimate (nanodollar dimension)
# ----------------------------------------------------------------------


def test_nanodollar_estimate_is_positive_for_priced_model() -> None:
    contract = _contract(
        max_nanodollars=1,
        ladder=[(1, DegradeAction.WARN)],
    )
    enforcer = _enforcer(contract)
    enforcer.register_run("r1", "triage")
    outcome = enforcer.before_call(
        run_id="r1",
        task="triage",
        provider="anthropic",
        model="claude-sonnet-4-6",
        request_kwargs={
            "messages": [{"role": "user", "content": "estimate this please"}]
        },
    )
    # A 1-nanodollar ceiling is blown by any real call → warn fires and
    # the projected value reflects a positive cost estimate.
    assert outcome.action == "warn"
    assert outcome.violations[0].projected_value > 0


# A four-turn conversation fixture: the input the enforcer prices before
# the call. The "actual" billed cost is the same input priced together
# with the output the call really produced.
_FOUR_TURN_REQUEST = {
    "messages": [
        {"role": "user", "content": "A customer says their invoice is wrong."},
        {
            "role": "assistant",
            "content": "Which charge looks incorrect, and what amount did "
            "they expect?",
        },
        {
            "role": "user",
            "content": "They were billed $49 but their plan is the $29 tier. "
            "They have screenshots of the pricing page.",
        },
        {
            "role": "assistant",
            "content": "Understood. I'll verify the plan on the account and "
            "check whether a proration or a stale price applied.",
        },
        {
            "role": "user",
            "content": "Please draft a short reply explaining the difference "
            "and the refund, in a friendly tone.",
        },
    ]
}


def test_precall_estimate_within_30pct_of_actual() -> None:
    # The headline accuracy bar: the pre-call estimate must land within
    # 30% of what the call actually bills on a realistic 4-turn run. The
    # estimator predicts output tokens from the per-task moving average;
    # here the run's true output (560 tokens) differs from the default
    # 500 prediction, exercising the band rather than an exact match.
    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.pricing import estimate_nanodollars

    provider, model, task = "anthropic", "claude-sonnet-4-6", "triage"
    enforcer = _enforcer(_contract(max_nanodollars=1))

    estimate = enforcer._estimate_call_nanodollars(  # noqa: SLF001
        provider=provider,
        model=model,
        request_kwargs=_FOUR_TURN_REQUEST,
        task=task,
    )

    # Independently price the *actual* billed cost: identical input
    # tokenisation, paired with the output the call really produced.
    input_tokens = enforcer._count_input_tokens(_FOUR_TURN_REQUEST, model)  # noqa: SLF001
    actual_output_tokens = 560
    actual = estimate_nanodollars(
        provider,
        model,
        CausalTokenLedger(
            user_input_tokens=input_tokens,
            tool_schema_tokens=0,
            output_tokens=actual_output_tokens,
        ),
    )
    assert actual is not None and actual > 0
    assert abs(estimate - actual) / actual <= 0.30


# ----------------------------------------------------------------------
# Outcome window — advisory only
# ----------------------------------------------------------------------


def test_outcome_window_flags_low_success_rate() -> None:
    contract = _contract(required_success_rate=0.95, window=10)
    enforcer = _enforcer(contract)
    recent = ["success"] * 7 + ["failure"] * 3  # 70%
    violation = enforcer.evaluate_outcome_window(
        task="triage", recent_outcomes=recent
    )
    assert violation is not None
    assert violation.level == "outcome"
    assert violation.action is None
    assert violation.projected_value == pytest.approx(0.7)


def test_outcome_window_passes_when_rate_met() -> None:
    contract = _contract(required_success_rate=0.95, window=10)
    enforcer = _enforcer(contract)
    recent = ["success"] * 10
    assert (
        enforcer.evaluate_outcome_window(task="triage", recent_outcomes=recent)
        is None
    )


def test_outcome_window_ignores_unknown_task() -> None:
    enforcer = ContractEnforcer({})
    assert (
        enforcer.evaluate_outcome_window(task="nope", recent_outcomes=["failure"])
        is None
    )
