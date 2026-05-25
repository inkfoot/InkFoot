"""Tests for the Causal Token Ledger (E2-S1 acceptance).

Covers:
- 14-field defaults are all zero.
- ``input_total`` sums the 13 input-side categories (excludes
  ``output_tokens``).
- ``output_total`` equals ``output_tokens``.
- ``validate_against_usage`` accepts ±2% and rejects 5%; the output
  check is exact.
- Frozen dataclass — assignment raises ``FrozenInstanceError``.
- Edge cases: zero raw input, negative inputs rejected, tolerance
  parameter respected, non-string nasties (NaN tolerance, etc.).
"""

from __future__ import annotations

import dataclasses

import pytest

from inkfoot.ledger import (
    INPUT_CATEGORIES,
    INPUT_INVARIANT_TOLERANCE,
    CausalTokenLedger,
    field_names,
    validate_against_usage,
)


# ----------------------------------------------------------------------
# Acceptance
# ----------------------------------------------------------------------


def test_default_construction_is_all_zeros() -> None:
    ledger = CausalTokenLedger()
    for name in field_names():
        assert getattr(ledger, name) == 0


def test_input_total_excludes_output_tokens() -> None:
    ledger = CausalTokenLedger(
        user_input_tokens=10,
        tool_schema_tokens=5,
        output_tokens=999,
    )
    assert ledger.input_total == 15
    assert ledger.output_total == 999


def test_input_total_sums_thirteen_categories() -> None:
    # Each category set to 1 — input_total should be 13.
    fields_set = {name: 1 for name in INPUT_CATEGORIES}
    ledger = CausalTokenLedger(**fields_set)
    assert ledger.input_total == 13


def test_validate_accepts_two_percent_input_slop() -> None:
    ledger = CausalTokenLedger(
        user_input_tokens=98,  # 2% under 100
        output_tokens=50,
    )
    # Should not raise.
    validate_against_usage(ledger, raw_input=100, raw_output=50)


def test_validate_rejects_five_percent_input_mismatch() -> None:
    ledger = CausalTokenLedger(
        user_input_tokens=95,  # 5% under
        output_tokens=50,
    )
    with pytest.raises(AssertionError, match="deviates"):
        validate_against_usage(ledger, raw_input=100, raw_output=50)


def test_validate_rejects_output_mismatch_exactly() -> None:
    ledger = CausalTokenLedger(user_input_tokens=100, output_tokens=49)
    with pytest.raises(AssertionError, match="output_total"):
        validate_against_usage(ledger, raw_input=100, raw_output=50)


def test_ledger_is_frozen() -> None:
    ledger = CausalTokenLedger()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ledger.user_input_tokens = 5  # type: ignore[misc]


# ----------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------


def test_validate_zero_raw_input_only_passes_when_ledger_is_also_zero() -> None:
    zero_ledger = CausalTokenLedger(output_tokens=10)
    # Passes.
    validate_against_usage(zero_ledger, raw_input=0, raw_output=10)
    # Fails — non-zero input attribution against zero raw input.
    bad = CausalTokenLedger(user_input_tokens=5, output_tokens=10)
    with pytest.raises(AssertionError, match="can't compute"):
        validate_against_usage(bad, raw_input=0, raw_output=10)


def test_validate_rejects_negative_raw_inputs() -> None:
    ledger = CausalTokenLedger()
    with pytest.raises(ValueError, match="raw_input"):
        validate_against_usage(ledger, raw_input=-1, raw_output=0)
    with pytest.raises(ValueError, match="raw_output"):
        validate_against_usage(ledger, raw_input=0, raw_output=-1)


def test_validate_rejects_negative_tolerance() -> None:
    ledger = CausalTokenLedger()
    with pytest.raises(ValueError, match="tolerance"):
        validate_against_usage(
            ledger, raw_input=10, raw_output=0, tolerance=-0.01
        )


def test_validate_respects_custom_tolerance() -> None:
    # 4% off — would fail default 2% but pass when tolerance=0.05.
    ledger = CausalTokenLedger(user_input_tokens=96, output_tokens=10)
    with pytest.raises(AssertionError):
        validate_against_usage(ledger, raw_input=100, raw_output=10)
    validate_against_usage(
        ledger, raw_input=100, raw_output=10, tolerance=0.05
    )


def test_default_tolerance_is_two_percent() -> None:
    assert INPUT_INVARIANT_TOLERANCE == pytest.approx(0.02)


def test_input_total_with_cache_fields_included() -> None:
    """cache_read / cache_creation tokens contribute to input_total —
    they are *billed input* tokens, just at different rates."""
    ledger = CausalTokenLedger(
        user_input_tokens=10,
        cache_read_tokens=20,
        cache_creation_tokens=15,
    )
    assert ledger.input_total == 45


def test_field_names_returns_fourteen_fields() -> None:
    names = field_names()
    assert len(names) == 14
    assert "output_tokens" in names
    for input_field in INPUT_CATEGORIES:
        assert input_field in names


def test_input_categories_is_immutable_tuple() -> None:
    assert isinstance(INPUT_CATEGORIES, tuple)
    assert len(INPUT_CATEGORIES) == 13
