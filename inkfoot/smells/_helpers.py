"""Shared helpers for the current implementation smell detectors.

Smells consume LLM-call events emitted by the shim. Each event's
``payload_json`` is a JSON-serialised :class:`NeutralCall` (i.e. the
output of ``dataclasses.asdict(neutral_call)``); the ledger lives
under ``payload["ledger"]``. These helpers turn that JSON soup into
the typed shapes detectors actually want and keep error handling
consistent across the built-in smells.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional

# ``ledger_from_payload`` lives in :mod:`inkfoot.ledger` (its natural
# home — pure ledger deserialisation, shared with the storage
# aggregator). Re-exported here so smell detectors keep importing it from
# ``inkfoot.smells._helpers``.
from inkfoot.ledger import ledger_from_payload  # noqa: F401
from inkfoot.pricing import PRICING_ND_PER_TOKEN, PriceRow, _lookup_row


def price_row_for(payload: dict[str, Any]) -> Optional[PriceRow]:
    """Look up the pricing row for the call's (provider, model) —
    the exact row first, then the provider's ``"*"`` wildcard row
    (how OpenAI-compat models price at $0). Returns ``None`` when
    neither is in the table — smell detectors then report
    ``estimated_cost_impact_nd=0`` rather than guessing."""
    provider = payload.get("provider")
    model = payload.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    return _lookup_row(provider, model)


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
    if row is None:  # pragma: no cover — the current implementation always has Haiku
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
