"""Embedding pricing tests."""

from __future__ import annotations

from inkfoot.pricing import (
    EMBEDDING_PRICING_ND_PER_TOKEN,
    estimate_embedding_nanodollars,
)


def test_small_model_1000_tokens_is_twenty_thousand_nd() -> None:
    """text-embedding-3-small lists at $0.02/Mtok = 20 nd/token."""
    assert (
        estimate_embedding_nanodollars("openai", "text-embedding-3-small", 1000)
        == 20_000
    )


def test_large_model_priced_higher_than_small() -> None:
    small = estimate_embedding_nanodollars(
        "openai", "text-embedding-3-small", 1000
    )
    large = estimate_embedding_nanodollars(
        "openai", "text-embedding-3-large", 1000
    )
    assert large > small


def test_unknown_model_returns_none() -> None:
    assert estimate_embedding_nanodollars("openai", "not-a-model", 100) is None
    assert estimate_embedding_nanodollars("nobody", "voyage-3", 100) is None


def test_zero_tokens_costs_nothing() -> None:
    assert (
        estimate_embedding_nanodollars("openai", "text-embedding-3-small", 0) == 0
    )


def test_negative_tokens_clamped_to_zero() -> None:
    assert (
        estimate_embedding_nanodollars("openai", "text-embedding-3-small", -50)
        == 0
    )


def test_priced_models_cover_each_provider_family() -> None:
    providers = {provider for provider, _ in EMBEDDING_PRICING_ND_PER_TOKEN}
    assert {"openai", "gemini", "voyage"} <= providers
