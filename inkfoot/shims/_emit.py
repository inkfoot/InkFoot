"""Shared per-call event-emit pipeline used by both shims.

The two shims (Anthropic + OpenAI) wrap different SDK signatures
but the post-translator pipeline is identical: dispatch ``after_call``
hooks, serialise the request/response for replay mode, write the
event. This module factors out that shared tail.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from ulid import ULID  # python-ulid

from inkfoot._run_context import (
    current_run_id,
    ensure_active_run,
    get_or_create_run_state,
)
from inkfoot.shims._isolation import safely_run

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext, PolicyDecision
    from inkfoot.run import InMemoryRunState
    from inkfoot.storage import Storage


_LOG = logging.getLogger("inkfoot.shims")

# Process-wide monotonic sequence-per-run counter — guarantees event
# ordering within a run even when wall-clock ms collide.
_sequence_counters: dict[str, itertools.count] = {}


def _next_sequence(run_id: str) -> int:
    counter = _sequence_counters.get(run_id)
    if counter is None:
        counter = itertools.count(1)
        _sequence_counters[run_id] = counter
    return next(counter)


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_call_context(
    *,
    provider: str,
    model: str,
    request_kwargs: dict[str, Any],
    storage: "Storage",
) -> "CallContext":
    """Resolve the active run (or create the ambient one) and build
    a fresh :class:`CallContext`."""
    from inkfoot.policy import CallContext  # noqa: PLC0415

    run_id = ensure_active_run(storage, now_ms=_now_ms())
    return CallContext(
        provider=provider,
        model=model,
        run_id=run_id,
        request_kwargs=request_kwargs,
    )


def emit_llm_call(
    *,
    ctx: "CallContext",
    response: Any,
    started_at: int,
    ended_at: int,
    storage: "Storage",
    capture_mode: str,
    translator: Any,
    before_decisions: list["PolicyDecision"],
) -> None:
    """Build the neutral payload and write the event(s) for one
    LLM call. Honors :data:`capture_mode` — replay mode writes the
    sibling ``event_contents`` row.

    Side-effects:

    * One ``llm_call`` event row.
    * Zero-or-more policy events (one per ``warn``/``block``
      decision from before_call).
    * Optional ``event_contents`` row when ``capture_mode='replay'``.
    """
    from dataclasses import asdict

    state = get_or_create_run_state(ctx.run_id)
    neutral_call = safely_run(
        translator.translate,
        request=ctx.request_kwargs,
        response=response,
        run_state=state,
        started_at=started_at,
        ended_at=ended_at,
        sequence=_next_sequence(ctx.run_id),
        hook_label=f"{type(translator).__name__}.translate",
    )
    if neutral_call is None:
        # Translator raised; isolation absorbed it. Drop the event
        # rather than write a partial row.
        _LOG.warning("translator returned None for run %s; skipping emit", ctx.run_id)
        return

    # Stash the cost on the CallContext so the post-call policy
    # hooks (e.g. BudgetCap) see the freshly-computed number.
    if neutral_call.estimated_nanodollars is not None:
        ctx.estimated_nanodollars = neutral_call.estimated_nanodollars

    event_id = str(ULID())
    payload_json = json.dumps(asdict(neutral_call), default=str)

    request_json: Optional[str] = None
    response_json: Optional[str] = None
    if capture_mode == "replay":
        try:
            request_json = json.dumps(ctx.request_kwargs, default=str)
        except (TypeError, ValueError):
            request_json = None
        try:
            response_json = _serialise_response(response)
        except Exception:  # pragma: no cover — defensive
            response_json = None

    safely_run(
        storage.insert_event,
        event_id=event_id,
        run_id=ctx.run_id,
        kind="llm_call",
        occurred_at=ended_at,
        sequence=neutral_call.sequence,
        payload_json=payload_json,
        capture_mode=capture_mode,
        request_json=request_json,
        response_json=response_json,
        hook_label="storage.insert_event",
    )

    # Write any policy events the before_call decisions produced.
    for decision in before_decisions:
        if decision.action == "allow" or not decision.emit_event_kind:
            continue
        policy_payload = {
            "action": decision.action,
            "reason": decision.reason,
            "metadata": decision.metadata,
        }
        safely_run(
            storage.insert_event,
            event_id=str(ULID()),
            run_id=ctx.run_id,
            kind=decision.emit_event_kind,
            occurred_at=ended_at,
            sequence=_next_sequence(ctx.run_id),
            payload_json=json.dumps(policy_payload, default=str),
            capture_mode=capture_mode,
            hook_label="storage.insert_event(policy)",
        )


def _serialise_response(response: Any) -> Optional[str]:
    """Best-effort JSON serialisation of a provider response. SDK
    objects are usually pydantic models; we try ``model_dump``,
    ``dict()``, and ``__dict__`` in turn before falling back to
    ``str(response)``."""
    if response is None:
        return None
    if isinstance(response, dict):
        return json.dumps(response, default=str)
    for attr in ("model_dump", "dict", "to_dict"):
        candidate = getattr(response, attr, None)
        if callable(candidate):
            try:
                return json.dumps(candidate(), default=str)
            except Exception:  # pylint: disable=broad-except
                continue
    if hasattr(response, "__dict__"):
        try:
            return json.dumps(response.__dict__, default=str)
        except Exception:  # pylint: disable=broad-except
            pass
    return json.dumps({"_repr": str(response)}, default=str)
