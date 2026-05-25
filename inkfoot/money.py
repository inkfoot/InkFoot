"""Integer nanodollars (10⁻⁹ USD) — the *only* shape Inkfoot stores
money in (ADR-0-4).

Token costs routinely sit below a cent (e.g. Haiku output at $4/Mtok =
0.0004 cents/token). Cents lose precision per-token; floats drift
across millions of tokens. A 64-bit signed integer in nanodollars
holds ~$9.2 billion, which is ample headroom for any single workspace
in the foreseeable future.

In-memory math uses :class:`decimal.Decimal` with ``ROUND_HALF_EVEN``
when precision matters. Floats are refused at the API boundary —
``usd_to_nd(0.0004)`` is a *typed* mistake we surface as ``TypeError``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import NewType, Union

Nanodollar = NewType("Nanodollar", int)
"""One billionth of a US dollar. Stored as a Python ``int``."""


_NANOS_PER_USD = 1_000_000_000
_NANOS_PER_USD_DEC = Decimal(_NANOS_PER_USD)

# Signed 64-bit int range. SQLite INTEGER is up to 8 bytes signed; we
# refuse to silently overflow at the API boundary.
_INT64_MAX = (1 << 63) - 1
_INT64_MIN = -(1 << 63)


def _reject_floats(value: object, *, arg_name: str) -> None:
    """``isinstance(True, int)`` is True in Python, but ``isinstance(True,
    bool)`` is also True — so we accept bools as ints implicitly. Floats
    (including float subclasses) are *never* accepted: that's the whole
    point of nanodollars."""
    # Order matters: check bool before int (bool is a subclass of int).
    if isinstance(value, float):
        raise TypeError(
            f"{arg_name} refuses float input ({value!r}); use Decimal or int. "
            "Nanodollars exist to avoid float drift — see ADR-0-4."
        )


def usd_to_nd(value: Union[Decimal, int]) -> Nanodollar:
    """Convert a USD amount to integer nanodollars.

    Accepts :class:`~decimal.Decimal` and ``int``. Rejects ``float``
    (including subclasses) with :class:`TypeError`.

    Rounds half-to-even at the nanodollar boundary — i.e. sub-nano
    fractions like ``Decimal("0.0000000001")`` (one tenth of a
    nanodollar) round to 0; ``Decimal("0.0000000015")`` rounds to 2
    nanodollars under banker's rounding.

    Raises :class:`OverflowError` if the result would overflow signed
    64-bit int storage.
    """
    _reject_floats(value, arg_name="usd_to_nd")
    if not isinstance(value, (Decimal, int)):
        raise TypeError(
            f"usd_to_nd accepts Decimal or int, not {type(value).__name__}"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"usd_to_nd refuses non-finite Decimal: {value}")
        nanos_dec = (value * _NANOS_PER_USD_DEC).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
        nanos = int(nanos_dec)
    else:
        nanos = value * _NANOS_PER_USD

    if nanos > _INT64_MAX or nanos < _INT64_MIN:
        raise OverflowError(
            f"usd_to_nd({value!r}) = {nanos} nanodollars overflows int64 "
            f"({_INT64_MIN} .. {_INT64_MAX}). Nanodollars cap at ~$9.2B."
        )
    return Nanodollar(nanos)


def nd_to_usd(value: int) -> Decimal:
    """Convert integer nanodollars to a :class:`~decimal.Decimal` USD
    amount. Lossless: ``nd_to_usd(usd_to_nd(d)) == d`` for any Decimal
    ``d`` representable to nanodollar precision.

    The returned Decimal carries 9 fractional digits regardless of
    leading zeros — this preserves the invariant that round-tripping
    through this pair doesn't change the precision context.
    """
    _reject_floats(value, arg_name="nd_to_usd")
    if not isinstance(value, int):
        raise TypeError(
            f"nd_to_usd accepts int (nanodollars), not {type(value).__name__}"
        )
    return Decimal(value) / _NANOS_PER_USD_DEC


def format_usd(value: int, *, decimals: int = 4) -> str:
    """Format integer nanodollars as a USD display string.

    Defaults to 4 decimals (matches per-run display per ADR-0-4).
    Aggregate views typically pass ``decimals=2``.

    Negative amounts (refunds) display with a leading minus.
    """
    _reject_floats(value, arg_name="format_usd")
    if not isinstance(value, int):
        raise TypeError(
            f"format_usd accepts int (nanodollars), not {type(value).__name__}"
        )
    if decimals < 0:
        raise ValueError(f"decimals must be >= 0, got {decimals}")

    usd = nd_to_usd(value)
    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    rounded = usd.quantize(quant, rounding=ROUND_HALF_EVEN)
    return f"${rounded}"
