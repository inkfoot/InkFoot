"""Pricing module tests."""

from __future__ import annotations

from datetime import date

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import (
    PRICING_ND_PER_TOKEN,
    PRICING_TABLE_REVISION,
    PriceRow,
    estimate_nanodollars,
    revision_date,
)


# ----------------------------------------------------------------------
# Acceptance
# ----------------------------------------------------------------------


def test_sonnet_1000_input_returns_exactly_three_million_nd() -> None:
    """The acceptance criterion: 1000 input tokens × $3/Mtok = 3M nd."""
    ledger = CausalTokenLedger(user_input_tokens=1000)
    nd = estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
    assert nd == 3_000_000


def test_unknown_model_returns_none_not_error() -> None:
    ledger = CausalTokenLedger(user_input_tokens=100, output_tokens=50)
    assert estimate_nanodollars("anthropic", "imaginary-model", ledger) is None
    assert estimate_nanodollars("nobody", "gpt-4o", ledger) is None


def test_revision_constant_parses_as_iso_date() -> None:
    parsed = date.fromisoformat(PRICING_TABLE_REVISION)
    assert isinstance(parsed, date)
    assert revision_date() == parsed


# ----------------------------------------------------------------------
# Cost arithmetic
# ----------------------------------------------------------------------


def test_output_tokens_priced_at_output_rate() -> None:
    """Sonnet 4.6 output is 15_000 nd/tok; 1000 output → 15M nd."""
    ledger = CausalTokenLedger(output_tokens=1000)
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 15_000_000
    )


def test_cache_read_priced_at_cache_read_rate_not_input_rate() -> None:
    """Sonnet 4.6 cache_read is 300 nd/tok (vs input 3000)."""
    ledger = CausalTokenLedger(cache_read_tokens=1000)
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 300_000
    )


def test_cache_creation_priced_at_cache_write_rate() -> None:
    """Sonnet 4.6 cache_write is 3750 nd/tok."""
    ledger = CausalTokenLedger(cache_creation_tokens=1000)
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 3_750_000
    )


def test_openai_cache_creation_costs_zero_no_billed_writes() -> None:
    """OpenAI doesn't bill cache writes — the row has cache_write=0
    and the math reflects that even if a translator (mistakenly) put
    a non-zero count on the ledger.

    Under the new semantics: input_total = 100 (structural).
    fresh = max(0, 100 - 0 - 999) = 0. So cost = 0 × input_rate
    + 0 × cache_read + 999 × cache_write(=0) + 0 × output = 0.
    """
    ledger = CausalTokenLedger(
        user_input_tokens=100, cache_creation_tokens=999
    )
    assert estimate_nanodollars("openai", "gpt-4o", ledger) == 0


def test_fresh_input_recovered_from_input_total_minus_cache() -> None:
    """A typical caching agent: 130-token structural request body
    (the API ships the full request including cached prefix),
    50 of which the provider billed as cache_read, 20 as
    cache_creation. fresh = 130 - 50 - 20 = 60.

    Sonnet 4.6: 60×3000 + 50×300 + 20×3750 = 180_000 + 15_000 + 75_000
              = 270_000.
    """
    ledger = CausalTokenLedger(
        # Structural tokenisation of the request body (incl. the
        # cached prefix). On a real call this is the sum of system +
        # user + tools + memory + tool_results that the tokeniser
        # walked.
        system_static_tokens=80,
        user_input_tokens=50,
        # Provider-reported overlays:
        cache_read_tokens=50,
        cache_creation_tokens=20,
    )
    assert ledger.input_total == 130  # structural sum
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 270_000
    )


def test_no_double_count_on_cache_heavy_call() -> None:
    """Worked example: 5,000-token system
    block, 4,800 served from cache, 100 fresh.

    Under the old (buggy) semantics, ledger.input_total would have
    been ≈ 5,000 + 4,800 = 9,800 (cache tokens counted twice). cost
    estimate would have priced fresh as max(0, 9,800 - 4,800) = 5,000
    at fresh rates — 10× too expensive on a Sonnet cache-hit.

    Under the new semantics, input_total = 5,000 (just the
    structural sum). fresh = 5,000 - 4,800 = 200; the 100
    discrepancy is the tokeniser drift between the provider and
    tiktoken which is well within the 2% bar. Cost is computed at
    the right rates.
    """
    ledger = CausalTokenLedger(
        system_static_tokens=5_000,  # structural sum of the request
        cache_read_tokens=4_800,
    )
    assert ledger.input_total == 5_000
    # fresh = 5000 - 4800 = 200. cost = 200×3000 + 4800×300 = 600000 + 1440000.
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 200 * 3_000 + 4_800 * 300
    )


def test_fresh_input_clamped_at_zero_when_cache_exceeds_total() -> None:
    """Pathological ledger: provider reports more cached tokens than
    the tokeniser saw in the structural categories. fresh_input
    clamps at 0; math doesn't go negative."""
    ledger = CausalTokenLedger(
        # Structural sum = 0 (no tokenised content)
        # Cache overlays = 30 read + 25 write
        cache_read_tokens=30,
        cache_creation_tokens=25,
    )
    nd = estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
    # fresh = max(0, 0 - 30 - 25) = 0. cost = 0 + 30×300 + 25×3750 = 102_750.
    assert nd == 102_750


def test_zero_ledger_costs_nothing() -> None:
    assert (
        estimate_nanodollars(
            "anthropic", "claude-sonnet-4-6", CausalTokenLedger()
        )
        == 0
    )


def test_haiku_pricing_matches_table() -> None:
    """Haiku 4.5 input = 800 nd/tok. 1000 input → 800_000 nd."""
    ledger = CausalTokenLedger(user_input_tokens=1000)
    assert (
        estimate_nanodollars("anthropic", "claude-haiku-4-5", ledger)
        == 800_000
    )


# ----------------------------------------------------------------------
# Pricing table integrity
# ----------------------------------------------------------------------


def test_pricing_table_contains_baseline_models() -> None:
    for key in (
        ("anthropic", "claude-opus-4-7"),
        ("anthropic", "claude-sonnet-4-6"),
        ("anthropic", "claude-haiku-4-5"),
        ("openai", "gpt-4o"),
        ("openai", "gpt-4o-mini"),
        ("openai", "o1"),
    ):
        assert key in PRICING_ND_PER_TOKEN, f"missing pricing for {key}"


def test_all_pricing_values_are_non_negative_integers() -> None:
    for key, row in PRICING_ND_PER_TOKEN.items():
        for attr in ("input", "output", "cache_read", "cache_write"):
            value = getattr(row, attr)
            assert isinstance(value, int), (
                f"{key}.{attr} must be int; got {type(value).__name__}"
            )
            assert value >= 0, f"{key}.{attr} is negative"


def test_pricerow_is_frozen() -> None:
    row = PRICING_ND_PER_TOKEN[("anthropic", "claude-sonnet-4-6")]
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.input = 1  # type: ignore[misc]


# ----------------------------------------------------------------------
# Claude on Bedrock — two SDK paths, one rate
# ----------------------------------------------------------------------


def test_bedrock_claude_priced_under_both_provider_keys() -> None:
    """A Claude-on-Bedrock model resolves whether the call arrived via
    the boto3 ``bedrock`` path or the ``anthropic_bedrock`` client, and
    both share the identical row so the rates can't drift apart."""
    model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert ("bedrock", model) in PRICING_ND_PER_TOKEN
    assert ("anthropic_bedrock", model) in PRICING_ND_PER_TOKEN
    assert (
        PRICING_ND_PER_TOKEN[("bedrock", model)]
        == PRICING_ND_PER_TOKEN[("anthropic_bedrock", model)]
    )


def test_common_anthropic_bedrock_models_resolve_to_a_price() -> None:
    ledger = CausalTokenLedger(user_input_tokens=1_000, output_tokens=1_000)
    for model in (
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "anthropic.claude-3-opus-20240229-v1:0",
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ):
        nd = estimate_nanodollars("anthropic_bedrock", model, ledger)
        assert nd is not None, f"{model} should resolve to a price"
        assert int(nd) > 0


def test_unknown_anthropic_bedrock_model_is_unpriced() -> None:
    ledger = CausalTokenLedger(user_input_tokens=10, output_tokens=10)
    assert (
        estimate_nanodollars(
            "anthropic_bedrock", "anthropic.imaginary-model-v9:0", ledger
        )
        is None
    )
