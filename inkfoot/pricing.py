"""Pricing module — per-token nanodollar rates for cost estimation.

Snapshot of the public per-Mtok pricing of every model the current implementation
supports, expressed as integer nanodollars per token. All math is
integer; no floats anywhere.

The OSS ships a *static* snapshot; future Cloud variant pulls
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
    * ``cache_write`` — tokens written *into* the cache. Anthropic
      bills marker-driven writes per call; Gemini bills cache-
      resource creation at the full input rate; OpenAI doesn't bill
      writes at all, so OpenAI rows keep ``cache_write=0``.
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
    # Gemini — 1.5 family, ≤128k-prompt tier. Cached-content reads
    # bill at 25% of input; creating the cache resource bills its
    # tokens at the full input rate (the per-hour storage fee on a
    # live resource is resource-level, not per-call, so it has no
    # row here). cache_read is int-truncated from the 0.25 ratio.
    ("gemini", "gemini-1.5-pro"): PriceRow(
        input=1_250, output=5_000, cache_read=312, cache_write=1_250
    ),
    ("gemini", "gemini-1.5-flash"): PriceRow(
        input=75, output=300, cache_read=18, cache_write=75
    ),
    # Bedrock — only the Anthropic family is priced: AWS lists
    # Claude on Bedrock at parity with Anthropic direct (same
    # 0.1× cache-read / 1.25× cache-write ratios). The other
    # families (Llama, Titan, Mistral, Cohere) vary by region and
    # purchasing model (on-demand vs provisioned throughput), so
    # they stay unpriced — estimate_nanodollars returns None and
    # callers fall back to tokens-only reporting.
    ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0"): PriceRow(
        input=3_000, output=15_000, cache_read=300, cache_write=3_750
    ),
    ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"): PriceRow(
        input=800, output=4_000, cache_read=80, cache_write=1_000
    ),
    # OpenAI-compatible endpoints (vLLM, Ollama, Together, …). The
    # "*" wildcard matches any model under the provider type —
    # self-hosted is free at the provider boundary, so the default
    # is all zeros. Operators on a paid compat endpoint add exact
    # ("openai_compat", "<model>") rows, which win over the
    # wildcard.
    ("openai_compat", "*"): PriceRow(
        input=0, output=0, cache_read=0, cache_write=0
    ),
}


# Per-token nanodollar rates for embedding models. Embeddings bill a
# single input-side rate (there is no output, no cache tier), so a
# flat ``int`` per token is enough — no :class:`PriceRow`. Sample
# arithmetic: ``text-embedding-3-small`` lists at $0.02 per million
# tokens → per-token = 0.02e-6 USD = 20 nanodollars.
#
# Unknown ``(provider, model)`` pairs return ``None`` from
# :func:`estimate_embedding_nanodollars`, mirroring the chat path —
# the report shows token counts and omits the dollar column.
EMBEDDING_PRICING_ND_PER_TOKEN: dict[tuple[str, str], int] = {
    ("openai", "text-embedding-3-small"): 20,
    ("openai", "text-embedding-3-large"): 130,
    ("openai", "text-embedding-ada-002"): 100,
    # Google embedding models. ``text-embedding-004`` ships on the
    # free tier (priced at zero here so the report shows $0.0000
    # rather than an unpriced blank); ``gemini-embedding-001`` is the
    # paid successor.
    ("gemini", "text-embedding-004"): 0,
    ("gemini", "gemini-embedding-001"): 150,
    # Voyage AI — the embedding provider Anthropic points to (the
    # Anthropic API has no first-party embeddings endpoint).
    ("voyage", "voyage-3"): 60,
    ("voyage", "voyage-3-lite"): 20,
}


def _lookup_row(provider: str, model: str) -> Optional[PriceRow]:
    """Exact ``(provider, model)`` row first, then the provider's
    ``"*"`` wildcard row (how OpenAI-compat endpoints price every
    model identically)."""
    row = PRICING_ND_PER_TOKEN.get((provider, model))
    if row is None:
        row = PRICING_ND_PER_TOKEN.get((provider, "*"))
    return row


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

    Returns ``None`` when neither ``(provider, model)`` nor the
    provider's ``(provider, "*")`` wildcard is in the table — the
    caller (typically the report renderer) shows tokens but omits
    the dollar column. **Never raises** for unknown models.

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
    row = _lookup_row(provider, model)
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


def estimate_embedding_nanodollars(
    provider: str,
    model: str,
    input_tokens: int,
) -> Optional[Nanodollar]:
    """Estimate the billed cost of one embedding call in nanodollars.

    Embeddings price a single input rate — no output, no cache tiers
    — so the estimate is ``input_tokens × rate``. Negative
    ``input_tokens`` is clamped to zero so a miscounted call never
    yields a negative cost.

    Returns ``None`` when ``(provider, model)`` is not in
    :data:`EMBEDDING_PRICING_ND_PER_TOKEN`, so callers fall back to a
    tokens-only view rather than inventing a price. **Never raises**
    for unknown models.
    """
    rate = EMBEDDING_PRICING_ND_PER_TOKEN.get((provider, model))
    if rate is None:
        return None
    return Nanodollar(max(0, int(input_tokens)) * rate)


def revision_date() -> date:
    """Parse :data:`PRICING_TABLE_REVISION` into a :class:`date`.
    Useful so smell rules and tests can compare against current
    time without re-parsing the constant everywhere.
    """
    return date.fromisoformat(PRICING_TABLE_REVISION)


def estimate_per_category(
    provider: str,
    model: str,
    ledger: CausalTokenLedger,
) -> dict[str, Nanodollar]:
    """Per-ledger-field nanodollar split used by ``inkfoot report``.

    Each of the 14 ledger fields gets priced at the rate that
    applies to its category:

    * Structural cause categories (11 fields) → ``row.input`` rate.
      Their tokens were part of the request body the provider billed
      at the fresh-input rate (cache_read / cache_creation are
      separate overlays, see below).
    * ``cache_read_tokens`` → ``row.cache_read`` rate.
    * ``cache_creation_tokens`` → ``row.cache_write`` rate.
    * ``output_tokens`` → ``row.output`` rate.

    Unknown ``(provider, model)`` returns all-zeros so the renderer
    can show "tokens only, no $" — never raises.

    The per-category sum should equal :func:`estimate_nanodollars`'s
    total for the same ledger (modulo rounding), since both methods
    are pricing the same fields against the same row — just one
    splits per-field and the other aggregates.

    **Note on the structural cats**: this prices them at full input
    rate, while :func:`estimate_nanodollars` subtracts cache_read +
    cache_creation from the structural sum before pricing the fresh
    portion. The per-category renderer can't do that subtraction
    because the cached tokens are scattered across multiple
    structural fields (we don't know which ones), so the bar chart
    shows the "what they'd cost at full input rate" view. The
    headline total at the top of the report uses
    ``estimate_nanodollars`` so the dollar figure stays faithful;
    bar-chart percentages are then a *share of structural cost* —
    the gap is named explicitly in the report docstring.
    """
    from inkfoot.ledger import INPUT_CATEGORIES  # noqa: PLC0415

    row = _lookup_row(provider, model)
    if row is None:
        return {
            name: Nanodollar(0)
            for name in INPUT_CATEGORIES
            + ("cache_read_tokens", "cache_creation_tokens", "output_tokens")
        }

    per_category: dict[str, Nanodollar] = {}
    for name in INPUT_CATEGORIES:
        tokens = getattr(ledger, name, 0)
        per_category[name] = Nanodollar(tokens * row.input)
    per_category["cache_read_tokens"] = Nanodollar(
        ledger.cache_read_tokens * row.cache_read
    )
    per_category["cache_creation_tokens"] = Nanodollar(
        ledger.cache_creation_tokens * row.cache_write
    )
    per_category["output_tokens"] = Nanodollar(
        ledger.output_tokens * row.output
    )
    return per_category
