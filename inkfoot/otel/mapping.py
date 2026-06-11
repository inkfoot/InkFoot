"""Bidirectional mapping between :class:`NeutralCall` and OTel attributes.

Round-trip-safe: a
``NeutralCall`` → attrs → ``NeutralCall`` cycle preserves every
field this mapping covers. Fields not present in the OTel GenAI
spec (``tools_offered``, ``tools_called``, ``parent_run_id``,
``cache_status``, ``metadata``, ``error``) are left at their
:class:`NeutralCall` defaults on inbound mapping; outbound the
mapper carries every ledger value but only the spec'd top-level
fields go through.

There are no exceptions raised on missing values — partial OTel
spans (a collector that strips half the attrs) yield a partial
:class:`NeutralCall` with the missing categories at zero. The
caller is responsible for deciding what to do with partial
fidelity.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.otel.conventions import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_CAUSE_FIELDS,
    INKFOOT_ESTIMATED_NANODOLLARS,
    INKFOOT_ESTIMATION_FLAGS,
    INKFOOT_EVENT_KIND,
    INKFOOT_RUN_ID,
    INKFOOT_SEQUENCE,
    cause_attr,
)


# Type alias for the OTel attribute dict. OTel attrs are
# string-keyed, value-typed (string / number / bool / array).
AttrMap = Dict[str, Any]


def neutral_call_to_attrs(
    call: NeutralCall,
    *,
    operation_name: str = "chat",
    response_id: Optional[str] = None,
    run_id: Optional[str] = None,
    sequence: Optional[int] = None,
) -> AttrMap:
    """Render ``call`` as the OTel GenAI attribute dict.

    ``operation_name`` defaults to ``"chat"`` — the only flavour
    Inkfoot emits. ``response_id`` ought to be the provider's
    response id when available; defaulting to ``None`` (omitted)
    rather than to the event id keeps the wire format honest about
    what the upstream system actually saw.

    ``run_id`` / ``sequence`` are inkfoot-extension provenance
    attrs that let an ingest receiver recover the run grouping on
    the other side. Optional — omit when forwarding spans that
    didn't originate from inkfoot.
    """
    attrs: AttrMap = {
        GEN_AI_SYSTEM: call.provider,
        GEN_AI_REQUEST_MODEL: call.model,
        GEN_AI_USAGE_INPUT_TOKENS: call.ledger.input_total
        + int(call.ledger.cache_read_tokens)
        + int(call.ledger.cache_creation_tokens),
        GEN_AI_USAGE_OUTPUT_TOKENS: int(call.ledger.output_tokens),
        GEN_AI_OPERATION_NAME: operation_name,
    }
    if response_id is not None:
        attrs[GEN_AI_RESPONSE_ID] = response_id

    # Per-cause breakdown — the inkfoot.cause.* extension namespace.
    for field_name in INKFOOT_CAUSE_FIELDS:
        attrs[cause_attr(field_name)] = int(getattr(call.ledger, field_name) or 0)

    # CSV-encoded so a backend that only stores strings doesn't
    # truncate the array. Round-trips losslessly via str.split.
    if call.estimation_flags:
        attrs[INKFOOT_ESTIMATION_FLAGS] = ",".join(call.estimation_flags)
    if call.estimated_nanodollars is not None:
        attrs[INKFOOT_ESTIMATED_NANODOLLARS] = int(call.estimated_nanodollars)
    if run_id is not None:
        attrs[INKFOOT_RUN_ID] = run_id
    if sequence is not None:
        attrs[INKFOOT_SEQUENCE] = int(sequence)
    return attrs


def attrs_to_neutral_call(
    attrs: Mapping[str, Any],
    *,
    started_at: int,
    ended_at: int,
    sequence: int = 0,
) -> NeutralCall:
    """Inverse of :func:`neutral_call_to_attrs`.

    Span timestamps (start / end) live outside the attribute dict
    in OTel; the caller supplies them in Unix ms. ``sequence`` is
    similarly out-of-band — the ingest layer fills it in via the
    per-run sequence counter when it persists.

    Missing per-cause attrs default to zero. Missing ``provider`` /
    ``model`` strings default to ``"unknown"`` so a malformed span
    can still be persisted (and surfaced) rather than dropped on
    the floor.
    """
    provider = str(attrs.get(GEN_AI_SYSTEM) or "unknown")
    model = str(attrs.get(GEN_AI_REQUEST_MODEL) or "unknown")

    ledger_kwargs: Dict[str, int] = {}
    for field_name in INKFOOT_CAUSE_FIELDS:
        val = attrs.get(cause_attr(field_name), 0)
        try:
            ledger_kwargs[field_name] = int(val) if val is not None else 0
        except (TypeError, ValueError):
            ledger_kwargs[field_name] = 0
    # Output total — provider-reported, comes through unchanged.
    out_val = attrs.get(GEN_AI_USAGE_OUTPUT_TOKENS, 0)
    try:
        ledger_kwargs["output_tokens"] = int(out_val) if out_val is not None else 0
    except (TypeError, ValueError):
        ledger_kwargs["output_tokens"] = 0
    ledger = CausalTokenLedger(**ledger_kwargs)

    flags_raw = attrs.get(INKFOOT_ESTIMATION_FLAGS)
    if isinstance(flags_raw, str) and flags_raw:
        estimation_flags = tuple(f for f in flags_raw.split(",") if f)
    elif isinstance(flags_raw, (list, tuple)):
        estimation_flags = tuple(str(f) for f in flags_raw if f)
    else:
        estimation_flags = ()

    nano_raw = attrs.get(INKFOOT_ESTIMATED_NANODOLLARS)
    try:
        estimated_nd: Optional[int] = (
            int(nano_raw) if nano_raw is not None else None
        )
    except (TypeError, ValueError):
        estimated_nd = None

    return NeutralCall(
        provider=provider,
        model=model,
        started_at=int(started_at),
        ended_at=int(ended_at),
        ledger=ledger,
        estimated_nanodollars=estimated_nd,
        estimation_flags=estimation_flags,
        sequence=int(sequence),
    )


__all__ = [
    "AttrMap",
    "neutral_call_to_attrs",
    "attrs_to_neutral_call",
]
