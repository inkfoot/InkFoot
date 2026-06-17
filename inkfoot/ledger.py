"""The Causal Token Ledger — 11 structural cause categories, 2 cache
billing overlays, and the output total.

Every billed token gets attributed. The ledger is the load-bearing
diagnostic surface of the whole product: ``inkfoot report`` slices
the bar chart along these fields; smells look at ratios between
them; contracts assert on them; the Cloud dashboard rolls them up
across runs.

**Field roles.** The ledger has 14 fields:

1. **11 structural cause categories** (``INPUT_CATEGORIES``):
   ``system_static_tokens``, ``system_dynamic_tokens``,
   ``user_input_tokens``, ``tool_schema_tokens``,
   ``tool_result_tokens``, ``retrieved_context_tokens``,
   ``memory_tokens``, ``retry_overhead_tokens``,
   ``summariser_tokens``, ``reasoning_tokens``,
   ``guardrail_tokens``. These are tokeniser-derived counts of the
   pieces that make up the full request body the provider saw.
   :attr:`CausalTokenLedger.input_total` sums them.

2. **2 cache billing overlays** (``CACHE_CATEGORIES``):
   ``cache_read_tokens``, ``cache_creation_tokens``. These come
   directly from ``response.usage`` and report how the provider
   *billed* the input. They are **not** added on top of the
   structural categories — that would double-count the cached
   portion of the request (Anthropic's API keeps cached blocks in
   the request body, so the structural tokenisation already covers
   them).

3. **1 output total** (``output_tokens``): provider-reported,
   billed separately at the output rate.

**Why the split.** Anthropic surfaces ``cache_read_input_tokens`` /
``cache_creation_input_tokens`` separately from ``input_tokens``;
all three together are the "total billed input". The structural
tokenisation walks the full request, so its sum ≈ total billed
input ≈ ``usage.input_tokens + cache_read + cache_creation``. The
billing overlays let the cost estimator price the cached portion at
the (cheaper) cache rates without re-introducing double-counting.
On OpenAI, ``usage.prompt_tokens`` already aggregates cached + fresh,
so ``input_total`` ≈ ``prompt_tokens``; ``cache_read_tokens`` then
reports the cached portion lifted from
``prompt_tokens_details.cached_tokens``.

We still **say "13 input-side cause categories" in prose** to refer
to the diagnostic surface ("which 42% of cost is fixable?"). When
the docs cite a sum, they mean the 11 structural categories — see
:func:`validate_against_usage` for the precise invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping


# The 11 structural cause categories, in canonical reporting order.
# These are tokeniser-derived from the request body and SUM to the
# total billed input. Cache fields are NOT included — they're billing
# overlays, accounted separately by :func:`pricing.estimate_nanodollars`.
INPUT_CATEGORIES: tuple[str, ...] = (
    "system_static_tokens",
    "system_dynamic_tokens",
    "user_input_tokens",
    "tool_schema_tokens",
    "tool_result_tokens",
    "retrieved_context_tokens",
    "memory_tokens",
    "retry_overhead_tokens",
    "summariser_tokens",
    "reasoning_tokens",
    "guardrail_tokens",
)

# The 2 cache billing overlays. Provider-reported counts; not
# additive with INPUT_CATEGORIES.
CACHE_CATEGORIES: tuple[str, ...] = (
    "cache_read_tokens",
    "cache_creation_tokens",
)

# Acceptable tokeniser slop when validating attribution against the
# provider's reported usage. 2% covers Anthropic's fallback
# tokeniser at typical context sizes; OpenAI with tiktoken should be
# under 0.5% in practice.
INPUT_INVARIANT_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class CausalTokenLedger:
    """Frozen attribution of one LLM call's billed tokens across 14
    fields (see the module docstring for the structural-vs-billing
    split).

    All fields default to 0 so partial population is safe — a
    translator that can only compute, say, ``output_tokens`` and
    ``cache_read_tokens`` from a provider response leaves the rest
    at zero rather than guessing. The validation invariant catches
    that later if the attribution didn't add up.
    """

    # 11 structural cause categories.
    system_static_tokens: int = 0
    system_dynamic_tokens: int = 0
    user_input_tokens: int = 0
    tool_schema_tokens: int = 0
    tool_result_tokens: int = 0
    retrieved_context_tokens: int = 0
    memory_tokens: int = 0
    retry_overhead_tokens: int = 0
    summariser_tokens: int = 0
    reasoning_tokens: int = 0
    guardrail_tokens: int = 0

    # 2 cache billing overlays (provider-reported; not additive).
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    # 1 output total.
    output_tokens: int = 0

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    @property
    def input_total(self) -> int:
        """Sum of the 11 structural cause categories.

        On Anthropic this approximates ``usage.input_tokens +
        cache_read_input_tokens + cache_creation_input_tokens`` —
        i.e. the *total billed input* (the cached portion is
        included because the structural tokenisation walks the full
        request body, which the API still ships including cached
        blocks). On OpenAI it approximates ``usage.prompt_tokens``
        (which already aggregates cached + fresh).

        Cache fields are explicitly **not** in this sum: they are
        billing overlays — see the module docstring + replay-mode storage contract
        comments in :mod:`inkfoot.pricing`."""
        return sum(getattr(self, name) for name in INPUT_CATEGORIES)

    @property
    def output_total(self) -> int:
        """Alias for ``output_tokens``. Exists so reporting code can
        symmetrically pair ``ledger.input_total`` with
        ``ledger.output_total``."""
        return self.output_tokens


def validate_against_usage(
    ledger: CausalTokenLedger,
    *,
    raw_input: int,
    raw_output: int,
    tolerance: float = INPUT_INVARIANT_TOLERANCE,
) -> None:
    """Assert the ledger's structural sum matches the provider-
    reported usage within tolerance (the ledger validation invariant).

    ``raw_input`` is the **total billed input** the provider charged
    for. Per-provider mapping:

    * Anthropic: ``usage.input_tokens + cache_read_input_tokens +
      cache_creation_input_tokens`` (three separate fields summed).
    * OpenAI: ``usage.prompt_tokens`` (a single field that already
      aggregates the cached + fresh portions).

    The reason we don't compare against ``usage.input_tokens`` alone
    on Anthropic: the structural tokenisation walks the *full*
    request body (the API still ships the cached prefix in the
    request, even when it's served from cache), so the structural
    sum naturally includes the cached tokens. Comparing against
    only the fresh portion would always fail by a wide margin on
    any cache-hit call. See the module docstring for the broader
    rationale.

    ``raw_output`` is the provider-reported output tokens. The
    output check is exact — we read it from the response, never
    estimate.

    The relative-error gate is strict (``< 0.02``) — equal to
    tolerance is rejected. Pass
    ``tolerance=0.02 + epsilon`` if you want inclusive.

    Raises :class:`AssertionError` with a diagnostic message on
    mismatch. CI runs this against a fixture corpus.
    """
    if raw_input < 0:
        raise ValueError(f"raw_input must be non-negative, got {raw_input}")
    if raw_output < 0:
        raise ValueError(f"raw_output must be non-negative, got {raw_output}")
    if tolerance < 0:
        raise ValueError(f"tolerance must be non-negative, got {tolerance}")

    actual_input = ledger.input_total
    if raw_input == 0:
        # A "zero raw input" call (e.g. cache-only continuation) is
        # rare but valid. Fail closed: the ledger must also be zero.
        if actual_input != 0:
            raise AssertionError(
                f"raw_input is 0 but ledger.input_total = {actual_input}; "
                f"can't compute relative error against zero"
            )
    else:
        rel_err = abs(actual_input - raw_input) / raw_input
        if rel_err >= tolerance:
            raise AssertionError(
                f"ledger.input_total ({actual_input}) deviates "
                f"{rel_err * 100:.2f}% from raw_input ({raw_input}); "
                f"meets-or-exceeds tolerance {tolerance * 100:.1f}%"
            )

    if ledger.output_total != raw_output:
        raise AssertionError(
            f"ledger.output_total ({ledger.output_total}) must equal "
            f"raw_output ({raw_output}) exactly"
        )


def field_names() -> tuple[str, ...]:
    """Names of every ledger field in declaration order. Useful for
    serialisation tests + report rendering."""
    return tuple(f.name for f in fields(CausalTokenLedger))


def ledger_from_payload(payload: Mapping[str, Any]) -> CausalTokenLedger:
    """Reconstruct a :class:`CausalTokenLedger` from an ``llm_call``
    event payload's ``ledger`` sub-dict — the shape ``emit_llm_call``
    writes via ``dataclasses.asdict(neutral_call)``.

    Defensive by design: a missing or non-mapping ``ledger``, or any
    missing / non-int field, falls back to 0, so a single corrupt row
    never cascades. Field names are taken from the dataclass itself
    (:func:`field_names`), so adding or renaming a ledger field flows
    through here automatically rather than silently dropping to 0.
    """
    ledger_dict = payload.get("ledger") or {}
    if not isinstance(ledger_dict, Mapping):
        return CausalTokenLedger()
    values: dict[str, int] = {}
    for name in field_names():
        value = ledger_dict.get(name, 0)
        # bool is an int subclass but never a real token count.
        if isinstance(value, bool) or not isinstance(value, int):
            value = 0
        values[name] = value
    return CausalTokenLedger(**values)
