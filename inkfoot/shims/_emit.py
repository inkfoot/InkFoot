"""Shared per-call event-emit pipeline used by both shims.

The two shims (Anthropic + OpenAI) wrap different SDK signatures
but the post-translator pipeline is identical: dispatch ``after_call``
hooks, serialise the request/response for replay mode, write the
event. This module factors out that shared tail.
"""

from __future__ import annotations

import json
import logging
import threading
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
#
# Stored as plain ints rather than ``itertools.count`` so the
# increment is a single atomic step under ``_sequence_lock`` —
# itertools.count is thread-safe at the C level but the dict
# check-then-set wrapper around it isn't, which let two concurrent
# same-run calls land identical sequences (Finding #1 in the CL3
# review). The current shape is "lock, read-or-init, increment,
# return" — one critical section, no race.
#
# Cleanup is wired: ``_run_lifecycle._RunHandle.end`` (and the
# abandoned-run cleanup in ``_mark_abandoned_runs``) call
# :func:`_drop_sequence_counter` so this dict stays bounded.
_sequence_lock = threading.Lock()
_sequence_counters: dict[str, int] = {}


def _next_sequence(run_id: str) -> int:
    with _sequence_lock:
        nxt = _sequence_counters.get(run_id, 0) + 1
        _sequence_counters[run_id] = nxt
        return nxt


def _drop_sequence_counter(run_id: str) -> None:
    """Release the per-run counter when the run ends. Idempotent.

    Public so tests + the eventual ``end_run`` path can keep memory
    bounded. Calling this on an unknown ``run_id`` is a no-op.
    """
    with _sequence_lock:
        _sequence_counters.pop(run_id, None)


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
    _emit_policy_events(
        ctx=ctx,
        before_decisions=before_decisions,
        ended_at=ended_at,
        storage=storage,
        capture_mode=capture_mode,
    )


# Cap on serialised error messages so a giant provider trace doesn't
# blow out a JSON column. 1 KB matches the §9.3 privacy guidance
# for user-facing error text (only one place in Phase 0 stores it).
_MAX_ERROR_MESSAGE_CHARS = 1024


def emit_llm_call_error(
    *,
    ctx: "CallContext",
    exc: BaseException,
    started_at: int,
    ended_at: int,
    storage: "Storage",
    capture_mode: str,
    before_decisions: list["PolicyDecision"],
) -> None:
    """Emit an ``llm_call`` event for a *failed* SDK call.

    The user's call still raises (the shim re-raises after this);
    we record a failure event with a :class:`NeutralError` so
    reports can show ``run with N attempted calls, 1 failed``
    instead of a missing event. The ledger is left at the
    all-zeros default — there's no usage data on a failure.

    Without this, E4's runaway-retry-loop smell would under-count
    by exactly the failure rate.
    """
    from dataclasses import asdict

    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.normalise import NeutralCall, NeutralError

    message = ""
    try:
        message = str(exc)[:_MAX_ERROR_MESSAGE_CHARS]
    except Exception:  # pylint: disable=broad-except  # pragma: no cover
        message = ""

    neutral_call = NeutralCall(
        provider=ctx.provider,
        model=ctx.model,
        started_at=started_at,
        ended_at=ended_at,
        ledger=CausalTokenLedger(),
        sequence=_next_sequence(ctx.run_id),
        error=NeutralError(type=type(exc).__name__, message=message),
        cache_status="n/a",
    )

    payload_json = json.dumps(asdict(neutral_call), default=str)
    safely_run(
        storage.insert_event,
        event_id=str(ULID()),
        run_id=ctx.run_id,
        kind="llm_call",
        occurred_at=ended_at,
        sequence=neutral_call.sequence,
        payload_json=payload_json,
        # Errors never carry replay content even in replay mode —
        # there's no successful response to record. The gating in
        # storage.insert_event suppresses the event_contents row.
        capture_mode=capture_mode,
        hook_label="storage.insert_event(error)",
    )

    # Policy events still get a chance to land — a BudgetCap warning
    # raised pre-call should still be recorded even if the SDK then
    # blew up.
    _emit_policy_events(
        ctx=ctx,
        before_decisions=before_decisions,
        ended_at=ended_at,
        storage=storage,
        capture_mode=capture_mode,
    )


def _emit_policy_events(
    *,
    ctx: "CallContext",
    before_decisions: list["PolicyDecision"],
    ended_at: int,
    storage: "Storage",
    capture_mode: str,
) -> None:
    """Write one event row per non-``allow`` policy decision. The
    success and failure paths both call this so any policy ``warn``
    decision is recorded regardless of whether the SDK succeeded."""
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
            # Policy events never carry content; pass through the
            # current capture_mode for consistency.
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
