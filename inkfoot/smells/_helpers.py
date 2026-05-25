"""Shared helpers for Phase 0 smell detectors.

Smells consume LLM-call events emitted by E3's shim. Each event's
``payload_json`` is a JSON-serialised :class:`NeutralCall` (i.e. the
output of ``dataclasses.asdict(neutral_call)``); the ledger lives
under ``payload["ledger"]``. These helpers turn that JSON soup into
the typed shapes detectors actually want and keep error handling
consistent across the five smells.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import PRICING_ND_PER_TOKEN, PriceRow


def ledger_from_payload(payload: dict[str, Any]) -> CausalTokenLedger:
    """Reconstruct a :class:`CausalTokenLedger` from a NeutralCall
    payload's ``ledger`` sub-dict.

    Defensive: missing or non-int fields fall back to 0. Smells
    treat each call independently so a corrupt single row should
    never cascade.
    """
    ledger_dict = payload.get("ledger") or {}
    if not isinstance(ledger_dict, dict):
        return CausalTokenLedger()

    fields: dict[str, int] = {}
    for name in (
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
        "output_tokens",
    ):
        value = ledger_dict.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            value = 0
        fields[name] = value
    return CausalTokenLedger(**fields)


def price_row_for(payload: dict[str, Any]) -> Optional[PriceRow]:
    """Look up the pricing row for the call's (provider, model).
    Returns ``None`` when the model isn't in the table — smell
    detectors then report ``estimated_cost_impact_nd=0`` rather
    than guessing."""
    provider = payload.get("provider")
    model = payload.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    return PRICING_ND_PER_TOKEN.get((provider, model))


def cache_write_premium(price: PriceRow) -> int:
    """Return the *premium* a cache write costs vs. fresh input —
    i.e. how much extra you pay (or save) per cached token. Clamped
    at 0 so the smell never reports a negative impact when a
    provider charges *less* for cache writes than fresh input."""
    return max(0, price.cache_write - price.input)


def haiku_output_price_nd() -> int:
    """The Haiku 4.5 output rate the
    ``expensive-model-low-entropy`` smell uses as the "what if you
    had used a cheap model" anchor. Pulled from the pricing table at
    import time so a future price update flows through automatically.
    """
    row = PRICING_ND_PER_TOKEN.get(("anthropic", "claude-haiku-4-5"))
    if row is None:  # pragma: no cover — Phase 0 always has Haiku
        return 0
    return row.output


def iter_llm_call_payloads(
    events: Iterable[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Convenience wrapper around :func:`inkfoot.smells.iter_llm_calls`.

    Re-exported here so each smell can ``from inkfoot.smells._helpers
    import iter_llm_call_payloads`` without circulating through the
    package ``__init__``."""
    from inkfoot.smells import iter_llm_calls

    yield from iter_llm_calls(events)
