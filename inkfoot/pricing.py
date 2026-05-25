"""Pricing module — per-token nanodollar rates for cost estimation.

Snapshot of the public per-Mtok pricing of every model Phase 0
supports, expressed as integer nanodollars per token (ADR-0-4). All
math is integer; no floats anywhere. See ``phase-0-classify.md``
§5.11 for the source table.

The OSS ships a *static* snapshot; Phase 3's Cloud variant pulls
fresh tables on signin (see ``PRICING_TABLE_REVISION`` for the
revision the dashboard checks against).

Unknown ``(provider, model)`` pairs return ``None`` from
:func:`estimate_nanodollars` rather than raising, so reports can show
"tokens only, no $" for models we haven't priced yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.money import Nanodollar

# When this snapshot was taken (provider list-prices as of this
# date). Format is ISO-8601 ``YYYY-MM-DD``. Parsers should use
# ``date.fromisoformat(PRICING_TABLE_REVISION)``.
PRICING_TABLE_REVISION = "2026-01-15"


@dataclass(frozen=True, slots=True)
class PriceRow:
    """Per-token nanodollar rates for one ``(provider, model)`` pair.

    All four rates apply to *input-side* spending bucketed by
    Anthropic's billing convention (which OpenAI roughly mirrors):

    * ``input`` — fresh input tokens (not served from cache, not
      written to cache).
    * ``output`` — model-generated output tokens.
    * ``cache_read`` — tokens served from the provider's prompt
      cache. Cheaper than ``input``.
    * ``cache_write`` — tokens written *into* the cache as a
      side-effect of the call (Anthropic only — OpenAI doesn't bill
      writes, so we leave ``cache_write=0`` for OpenAI rows).
    """

    input: int
    output: int
    cache_read: int
    cache_write: int


# Source: provider list pages as of PRICING_TABLE_REVISION. Numbers
# are nanodollars per token. Sample arithmetic: Sonnet 4.6 input is
# $3 per million tokens; per-token = 3e-6 USD = 3 000 nanodollars.
PRICING_ND_PER_TOKEN: dict[tuple[str, str], PriceRow] = {
    # Anthropic — claude-N family at the time of writing.
    ("anthropic", "claude-opus-4-7"): PriceRow(
        input=15_000, output=75_000, cache_read=1_500, cache_write=18_750
    ),
    ("anthropic", "claude-sonnet-4-6"): PriceRow(
        input=3_000, output=15_000, cache_read=300, cache_write=3_750
    ),
    ("anthropic", "claude-haiku-4-5"): PriceRow(
        input=800, output=4_000, cache_read=80, cache_write=1_000
    ),
    # OpenAI — gpt-4o + o1. Cache pricing is read-only (no billed
    # writes); we keep cache_write=0 and let estimate_nanodollars
    # short-circuit the cache_creation × 0 term to zero.
    ("openai", "gpt-4o"): PriceRow(
        input=2_500, output=10_000, cache_read=1_250, cache_write=0
    ),
    ("openai", "gpt-4o-mini"): PriceRow(
        input=150, output=600, cache_read=75, cache_write=0
    ),
    ("openai", "o1"): PriceRow(
        input=15_000, output=60_000, cache_read=7_500, cache_write=0
    ),
}


def estimate_nanodollars(
    provider: str,
    model: str,
    ledger: CausalTokenLedger,
) -> Optional[Nanodollar]:
    """Estimate the billed cost of one LLM call in nanodollars.

    Splits the input-side bill across three rates:

    * Fresh input — ``ledger.input_total - cache_read_tokens -
      cache_creation_tokens``. Priced at ``row.input``.
    * Cache reads — ``ledger.cache_read_tokens``. Priced at
      ``row.cache_read``.
    * Cache writes — ``ledger.cache_creation_tokens``. Priced at
      ``row.cache_write`` (0 for OpenAI).

    Plus output tokens × ``row.output``.

    **Why the subtraction is correct.** ``ledger.input_total`` sums
    the 11 *structural* categories — these tokenise the full request
    body, which still includes the cached portion (Anthropic ships
    the cached prefix in the request even when it's served from
    cache). The cache fields (``cache_read_tokens`` /
    ``cache_creation_tokens``) are billing overlays from
    ``response.usage``: they tell us how many of those tokens the
    provider billed at the cache rate. So
    ``input_total - cache_read - cache_creation`` recovers the
    fresh portion that the provider billed at the full input rate.

    Returns ``None`` when ``(provider, model)`` isn't in the table —
    the caller (typically the report renderer) shows tokens but
    omits the dollar column. **Never raises** for unknown models.

    Pathological inputs:

    * Negative ledger fields are clamped at the boundary by the
      ledger dataclass (it accepts any int but the translators only
      ever assign non-negatives). We don't re-validate here — that's
      the ledger's job.
    * ``input_total < cache_read + cache_creation`` is *not*
      impossible if a translator emits inconsistent fields (e.g.
      provider reports cached tokens we didn't see in the request
      body). We clamp ``fresh_input`` at 0 in that case so the
      estimate never goes negative; the validation invariant
      catches the underlying bug separately.
    """
    row = PRICING_ND_PER_TOKEN.get((provider, model))
    if row is None:
        return None

    fresh_input = max(
        0,
        ledger.input_total
        - ledger.cache_read_tokens
        - ledger.cache_creation_tokens,
    )
    total = (
        fresh_input * row.input
        + ledger.cache_read_tokens * row.cache_read
        + ledger.cache_creation_tokens * row.cache_write
        + ledger.output_tokens * row.output
    )
    return Nanodollar(total)


def revision_date() -> date:
    """Parse :data:`PRICING_TABLE_REVISION` into a :class:`date`.
    Useful so smell rules and tests can compare against current
    time without re-parsing the constant everywhere.
    """
    return date.fromisoformat(PRICING_TABLE_REVISION)
