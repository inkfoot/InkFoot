"""expensive-model-low-entropy smell tests."""

from __future__ import annotations

from inkfoot.smells.expensive_model_low_entropy import (
    EXPENSIVE_MODEL_LOW_ENTROPY,
)

from tests.unit.test_smells._fixtures import (
    event_from_neutral_call,
    fixture_run,
    make_neutral_call,
)


def _detect(events):
    return EXPENSIVE_MODEL_LOW_ENTROPY.detect(fixture_run(), events)


# ----------------------------------------------------------------------
# Positive
# ----------------------------------------------------------------------


def test_fires_for_opus_with_low_output_no_reasoning() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                model="claude-opus-4-7",
                ledger_fields={
                    "output_tokens": 50,
                    "reasoning_tokens": 0,
                },
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    assert result.smell.id == "expensive-model-low-entropy"
    assert result.severity == "info"
    assert result.evidence["sample_model"] == "claude-opus-4-7"
    assert result.evidence["sample_output_tokens"] == 50
    # Opus output: 75_000 nd/tok; Haiku output: 4000 nd/tok. Premium = 71_000.
    # 50 × 71_000 = 3_550_000.
    assert result.estimated_cost_impact_nd == 50 * (75_000 - 4_000)


def test_fires_for_gpt4o_with_low_output() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                provider="openai",
                model="gpt-4o",
                ledger_fields={"output_tokens": 100, "reasoning_tokens": 0},
            )
        ),
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["sample_model"] == "gpt-4o"
    # gpt-4o output: 10_000 nd. Premium = max(0, 10_000 - 4_000) = 6_000.
    # 100 × 6000 = 600_000.
    assert result.estimated_cost_impact_nd == 100 * (10_000 - 4_000)


def test_aggregates_qualifying_calls_across_run() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                model="claude-opus-4-7",
                ledger_fields={
                    "output_tokens": 50,
                    "reasoning_tokens": 0,
                },
            )
        )
        for i in range(4)
    ]
    result = _detect(events)
    assert result is not None
    assert result.evidence["qualifying_calls"] == 4
    assert result.estimated_cost_impact_nd == 4 * 50 * (75_000 - 4_000)


# ----------------------------------------------------------------------
# Negative
# ----------------------------------------------------------------------


def test_silent_for_cheaper_model_even_with_low_output() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                model="claude-haiku-4-5",
                ledger_fields={
                    "output_tokens": 50,
                    "reasoning_tokens": 0,
                },
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_for_gpt_4o_mini_even_though_prefix_matches() -> None:
    """Finding #1 in the CL4 review.

    ``"gpt-4o-mini".startswith("gpt-4o")`` is True, so the cheap
    model's name shares an "expensive" prefix. The premium-clamp
    guard in ``_detect`` keeps the smell silent because
    gpt-4o-mini's output rate (600 nd/tok) is *below* Haiku's
    (4000 nd/tok) — there's nothing to save by switching."""
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=i + 1,
                provider="openai",
                model="gpt-4o-mini",
                ledger_fields={"output_tokens": 50, "reasoning_tokens": 0},
            )
        )
        for i in range(5)
    ]
    assert _detect(events) is None


def test_silent_for_expensive_model_when_pricing_table_lacks_it() -> None:
    """A future "expensive-looking" model whose pricing isn't in
    the table can't be compared to Haiku — stay silent rather than
    false-positive with cost_impact=0."""
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                provider="openai",
                model="gpt-4o-experimental",  # not in PRICING_ND_PER_TOKEN
                ledger_fields={"output_tokens": 50, "reasoning_tokens": 0},
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_when_output_above_threshold() -> None:
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                model="claude-opus-4-7",
                ledger_fields={
                    "output_tokens": 300,  # above 200 threshold
                    "reasoning_tokens": 0,
                },
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_when_reasoning_tokens_present() -> None:
    """Real reasoning use justifies the expensive model."""
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                model="o1",
                ledger_fields={
                    "output_tokens": 50,
                    "reasoning_tokens": 800,
                },
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_for_zero_output_calls() -> None:
    """Zero output (errors, blocks) wouldn't have saved anything
    on a cheaper model either."""
    events = [
        event_from_neutral_call(
            make_neutral_call(
                sequence=1,
                model="claude-opus-4-7",
                ledger_fields={"output_tokens": 0, "reasoning_tokens": 0},
            )
        ),
    ]
    assert _detect(events) is None


def test_silent_on_empty_event_stream() -> None:
    assert _detect([]) is None
