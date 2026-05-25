"""Pricing module tests (E2-S5 acceptance)."""

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
    a non-zero count on the ledger."""
    ledger = CausalTokenLedger(
        user_input_tokens=100, cache_creation_tokens=999
    )
    # 100 fresh input × 2500 = 250_000; cache_creation × 0 = 0.
    # input_total = 100 + 999 = 1099. fresh = 1099 - 0 - 999 = 100. ✓
    assert estimate_nanodollars("openai", "gpt-4o", ledger) == 250_000


def test_fresh_input_excludes_cache_buckets() -> None:
    """Mixed ledger: 100 fresh input + 50 cache_read + 20 cache_create.
    input_total = 170. fresh = 170 - 50 - 20 = 100.
    Sonnet 4.6: 100×3000 + 50×300 + 20×3750 = 300000 + 15000 + 75000 = 390000.
    """
    ledger = CausalTokenLedger(
        user_input_tokens=100,
        cache_read_tokens=50,
        cache_creation_tokens=20,
    )
    assert (
        estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
        == 390_000
    )


def test_fresh_input_clamped_at_zero_when_cache_exceeds_total() -> None:
    """Pathological ledger where cache_read + cache_creation > input_total.
    fresh_input clamps at 0, math doesn't go negative."""
    ledger = CausalTokenLedger(
        # input_total = 30 + 25 = 55 but cache sums to 55, fresh = 0
        cache_read_tokens=30,
        cache_creation_tokens=25,
    )
    nd = estimate_nanodollars("anthropic", "claude-sonnet-4-6", ledger)
    # 0 fresh × 3000 + 30 × 300 + 25 × 3750 = 9000 + 93750 = 102750
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
                f"{key}.{attr} must be int (ADR-0-4); got {type(value).__name__}"
            )
            assert value >= 0, f"{key}.{attr} is negative"


def test_pricerow_is_frozen() -> None:
    row = PRICING_ND_PER_TOKEN[("anthropic", "claude-sonnet-4-6")]
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.input = 1  # type: ignore[misc]
