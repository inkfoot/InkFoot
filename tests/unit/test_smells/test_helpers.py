"""Direct tests for the shared smell-detector helpers.

Covers the pricing lookup's row-resolution order (exact row, then
the provider's wildcard row) and its defensive ``None`` paths; the
ledger reconstruction helper is exercised throughout the per-smell
suites.
"""

from __future__ import annotations

from typing import Any

from inkfoot.smells._helpers import price_row_for


def _payload(provider: Any, model: Any) -> dict[str, Any]:
    return {"provider": provider, "model": model, "ledger": {}}


def test_price_row_for_resolves_exact_row() -> None:
    row = price_row_for(_payload("anthropic", "claude-haiku-4-5"))
    assert row is not None
    assert row.output > 0


def test_price_row_for_resolves_wildcard_for_compat_models() -> None:
    # No exact row for this model; the ("openai_compat", "*") row
    # prices it at exactly $0 instead of "no estimate".
    row = price_row_for(_payload("openai_compat", "some-unlisted-model"))
    assert row is not None
    assert (row.input, row.output, row.cache_read, row.cache_write) == (
        0,
        0,
        0,
        0,
    )


def test_price_row_for_unknown_model_without_wildcard_is_none() -> None:
    assert price_row_for(_payload("anthropic", "claude-imaginary")) is None


def test_price_row_for_non_string_fields_is_none() -> None:
    assert price_row_for(_payload(None, 3)) is None
    assert price_row_for({}) is None
