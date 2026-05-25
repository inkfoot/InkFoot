"""The Causal Token Ledger — 13 input-side cause categories + the
output total.

Every billed token gets attributed to exactly one of the 13 input-side
categories or to ``output_tokens``. The ledger is the load-bearing
diagnostic surface of the whole product: ``inkfoot report`` slices
the bar chart along these fields; smells look at ratios between them;
contracts assert on them; the Cloud dashboard rolls them up across
runs.

See ``phase-0-classify.md`` §5.3 + the §5.4 class diagram for the
authoritative shape. The field-name suffix (``_tokens``) follows the
class diagram; the epic doc's prose mixes suffixed and bare names —
the class diagram wins.

**Categories vs. total.** The ledger has *14 fields*: 13 input-side
cause categories that account for billed input tokens, plus
``output_tokens`` which is billed separately. We say "13 causal
categories" throughout the docs to refer to the input attribution
surface. The 13 categories sum to the *total billed input*, which on
Anthropic is ``usage.input_tokens + cache_read_input_tokens +
cache_creation_input_tokens`` (each priced differently — see
``pricing.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields


# The 13 input-side categories, in canonical reporting order. Used
# by `inkfoot report` to render the bar chart and by smells to slice
# the ledger. Kept here (not on the class) so external code that
# wants to iterate "the input categories" doesn't reach into
# dataclass internals.
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
    "cache_creation_tokens",
    "cache_read_tokens",
)

# Acceptable tokeniser slop when validating attribution against the
# provider's reported usage (§5.3). 2% covers Anthropic's fallback
# tokeniser at typical context sizes; OpenAI with tiktoken should be
# under 0.5% in practice but the bar is per-provider.
INPUT_INVARIANT_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class CausalTokenLedger:
    """Frozen attribution of one LLM call's billed tokens across 14
    categories.

    All fields default to 0 so partial population is safe — a
    translator that can only compute, say, ``output_tokens`` and
    ``cache_read_tokens`` from a provider response leaves the rest at
    zero rather than guessing. The validation invariant catches that
    later if the attribution didn't add up.
    """

    # Input-side cause categories (13).
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
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    # Output (1).
    output_tokens: int = 0

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    @property
    def input_total(self) -> int:
        """Sum of the 13 input-side cause categories.

        Includes ``cache_read_tokens`` and ``cache_creation_tokens`` —
        those are *also* billed input tokens, just at different
        rates. See ``pricing.estimate_nanodollars`` for how the
        billing splits."""
        return sum(getattr(self, name) for name in INPUT_CATEGORIES)

    @property
    def output_total(self) -> int:
        """Alias for ``output_tokens``. Exists so reporting code can
        symmetrically pair ``ledger.input_total`` with
        ``ledger.output_total`` without remembering which field is
        the output."""
        return self.output_tokens


def validate_against_usage(
    ledger: CausalTokenLedger,
    *,
    raw_input: int,
    raw_output: int,
    tolerance: float = INPUT_INVARIANT_TOLERANCE,
) -> None:
    """Assert the ledger's totals match the provider-reported usage
    within tolerance (§5.3 validation invariant).

    ``raw_input`` is the **total billed input**: for Anthropic that's
    ``usage.input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens``; for OpenAI it's
    ``usage.prompt_tokens`` (which already aggregates cached + fresh).

    ``raw_output`` is the provider-reported output tokens. The output
    check is exact — we read it from the response, never estimate.

    Raises :class:`AssertionError` with a diagnostic message on
    mismatch. Phase 0 CI runs this against a fixture corpus.
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
        if rel_err > tolerance:
            raise AssertionError(
                f"ledger.input_total ({actual_input}) deviates "
                f"{rel_err * 100:.2f}% from raw_input ({raw_input}); "
                f"exceeds tolerance {tolerance * 100:.1f}%"
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
