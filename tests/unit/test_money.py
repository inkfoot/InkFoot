"""Nanodollar money type tests."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from inkfoot.money import (
    Nanodollar,
    _INT64_MAX,
    format_usd,
    nd_to_usd,
    usd_to_nd,
)


# ----------------------------------------------------------------------
# Acceptance criteria
# ----------------------------------------------------------------------


def test_usd_to_nd_haiku_per_token_acceptance() -> None:
    """ADR-0-4 driving example: $0.0004 (Haiku output per-token) is
    400,000 nanodollars."""
    assert usd_to_nd(Decimal("0.0004")) == 400_000


def test_nd_to_usd_round_trip_for_haiku_value() -> None:
    nd = usd_to_nd(Decimal("0.0004"))
    assert nd_to_usd(nd) == Decimal("0.000400000")


def test_usd_to_nd_refuses_floats() -> None:
    with pytest.raises(TypeError, match="refuses float"):
        usd_to_nd(0.0004)  # type: ignore[arg-type]


def test_summing_one_million_random_nanodollars_is_exact() -> None:
    """The whole point: 10⁶ rows summed via int gives the same answer
    as the equivalent Decimal sum."""
    rng = random.Random(42)
    decimal_values = [
        Decimal(rng.randint(0, 10_000_000)) / Decimal(1_000_000_000)
        for _ in range(1_000_000)
    ]
    nd_sum = sum(usd_to_nd(d) for d in decimal_values)
    dec_sum = sum(decimal_values)
    assert nd_to_usd(nd_sum) == dec_sum


# ----------------------------------------------------------------------
# usd_to_nd — edge cases
# ----------------------------------------------------------------------


def test_usd_to_nd_zero_is_zero() -> None:
    assert usd_to_nd(Decimal("0")) == 0
    assert usd_to_nd(0) == 0


def test_usd_to_nd_accepts_negative_for_refunds() -> None:
    assert usd_to_nd(Decimal("-0.01")) == -10_000_000


def test_usd_to_nd_accepts_int() -> None:
    assert usd_to_nd(5) == 5 * 1_000_000_000


def test_usd_to_nd_refuses_string() -> None:
    with pytest.raises(TypeError):
        usd_to_nd("0.0004")  # type: ignore[arg-type]


def test_usd_to_nd_refuses_float_subclass() -> None:
    class MyFloat(float):
        pass

    with pytest.raises(TypeError):
        usd_to_nd(MyFloat(0.5))  # type: ignore[arg-type]


def test_usd_to_nd_refuses_nan() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        usd_to_nd(Decimal("nan"))


def test_usd_to_nd_refuses_infinity() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        usd_to_nd(Decimal("Infinity"))


def test_usd_to_nd_rounds_half_to_even_at_nanodollar_boundary() -> None:
    # 0.5 of a nanodollar — banker's rounding goes to even (0).
    assert usd_to_nd(Decimal("0.0000000005")) == 0
    # 1.5 nanodollars — rounds to 2 (the even neighbour).
    assert usd_to_nd(Decimal("0.0000000015")) == 2


def test_usd_to_nd_overflows_above_int64_ceiling() -> None:
    # ~$9.3 billion is just past the int64 ceiling.
    too_big = Decimal(_INT64_MAX) / Decimal(1_000_000_000) + Decimal("1")
    with pytest.raises(OverflowError):
        usd_to_nd(too_big)


# ----------------------------------------------------------------------
# nd_to_usd — edge cases
# ----------------------------------------------------------------------


def test_nd_to_usd_refuses_floats() -> None:
    with pytest.raises(TypeError):
        nd_to_usd(0.5)  # type: ignore[arg-type]


def test_nd_to_usd_handles_negative() -> None:
    assert nd_to_usd(-400_000) == Decimal("-0.000400000")


def test_nd_to_usd_zero() -> None:
    assert nd_to_usd(0) == Decimal("0")


# ----------------------------------------------------------------------
# format_usd
# ----------------------------------------------------------------------


def test_format_usd_default_four_decimals() -> None:
    assert format_usd(400_000) == "$0.0004"


def test_format_usd_two_decimals_for_aggregates() -> None:
    assert format_usd(123_450_000_000, decimals=2) == "$123.45"


def test_format_usd_rounds_half_to_even() -> None:
    # 0.00005 → at 4 decimals, half-to-even goes to 0.0000 (the even
    # neighbour). Confirms banker's rounding.
    assert format_usd(50_000, decimals=4) == "$0.0000"
    # 0.00015 → 0.0002 (even at 2)
    assert format_usd(150_000, decimals=4) == "$0.0002"


def test_format_usd_negative_shows_minus() -> None:
    assert format_usd(-400_000) == "$-0.0004"


def test_format_usd_zero_decimals() -> None:
    assert format_usd(123_400_000_000, decimals=0) == "$123"


def test_format_usd_rejects_negative_decimals() -> None:
    with pytest.raises(ValueError):
        format_usd(400_000, decimals=-1)


def test_format_usd_refuses_float() -> None:
    with pytest.raises(TypeError):
        format_usd(400_000.0)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Type identity
# ----------------------------------------------------------------------


def test_nanodollar_is_a_newtype_returning_int() -> None:
    # NewType is erased at runtime; ``Nanodollar(5)`` should just be 5.
    assert Nanodollar(5) == 5
    assert isinstance(Nanodollar(5), int)
