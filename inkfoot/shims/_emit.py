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
from contextvars import ContextVar
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

# CallContext.metadata key that marks a call as a summariser helper
# call (set by ``CheapSummariser.before_call`` when its re-entrancy
# guard sees its own sub-call). Defined here — not on the policy — so
# the emit hot path can check it without importing the policy package.
SUMMARISER_CALL_METADATA_KEY = "summariser_call"

# Process-wide monotonic sequence-per-run counter — guarantees event
# ordering within a run even when wall-clock ms collide.
#
# Stored as plain ints rather than ``itertools.count`` so the
# increment is a single atomic step under ``_sequence_lock`` —
# itertools.count is thread-safe at the C level but the dict
# check-then-set wrapper around it isn't, which let two concurrent
# same-run calls land identical sequences. The current shape is "lock, read-or-init, increment,
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
    response_id: Optional[str] = None,
    skip_dedup: bool = False,
    extra_estimation_flags: tuple[str, ...] = (),
) -> None:
    """Build the neutral payload and write the event(s) for one
    LLM call. Honors :data:`capture_mode` — replay mode writes the
    sibling ``event_contents`` row.

    One call can be observed by more than one capture layer (a
    raw-SDK shim *and* the LangChain callback handler). The provider
    response id — passed as ``response_id`` or read off the response
    object — is recorded per run before anything is written; a
    second sighting of the same id skips the emit entirely, so the
    layer that observed the call first (the shim, which fires inside
    the SDK call) keeps its richer event. Calls with no extractable
    id are never deduplicated — fail open, double-count rather than
    drop.

    Invariant: the gate is first-emit-wins, not directional. "The
    shim's richer event survives" holds because the shim records the
    id *before* the handler's ``on_llm_end``. For a non-streaming
    call that is automatic — the shim emits synchronously inside the
    SDK call. A streamed call's event is only complete at
    stream-close, which can land *after* ``on_llm_end``; the
    streaming recorder therefore *claims* the id at the first chunk
    that exposes it (always before the handler runs) and then emits
    with ``skip_dedup=True``, so the gate stays effectively
    directional without this function needing to know who is calling.

    ``extra_estimation_flags`` are merged onto the translated event,
    order-preserving and de-duplicated — the streaming path uses them
    to mark a tokeniser-estimated output (``stream_no_usage`` /
    ``stream_options_off``).

    Side-effects:

    * One ``llm_call`` event row.
    * Zero-or-more policy events (one per ``warn``/``block``
      decision from before_call).
    * Optional ``event_contents`` row when ``capture_mode='replay'``.
    """
    from dataclasses import asdict

    if not skip_dedup:
        dedup_id = response_id or _response_dedup_id(response)
        if dedup_id:
            from inkfoot._run_lifecycle import (  # noqa: PLC0415
                _record_emitted_response_id,
            )

            first_sighting = safely_run(
                _record_emitted_response_id,
                ctx.run_id,
                dedup_id,
                fallback=True,
                hook_label="_record_emitted_response_id",
            )
            if not first_sighting:
                _LOG.debug(
                    "response %s already recorded for run %s; "
                    "skipping duplicate emit",
                    dedup_id,
                    ctx.run_id,
                )
                return

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

    if extra_estimation_flags:
        neutral_call = _merge_estimation_flags(
            neutral_call, extra_estimation_flags
        )

    if ctx.metadata.get(SUMMARISER_CALL_METADATA_KEY):
        neutral_call = _fold_into_summariser_tokens(neutral_call)

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

    # Fold the call's actuals back into the contract enforcer so the
    # running spend and the per-task output moving average stay current.
    # Best-effort: never breaks the emit path.
    from inkfoot.contracts.runtime import record_call as _record_contract_call

    _record_contract_call(ctx, neutral_call)


# Cross-layer embedding dedup signal. One embedding call can be seen
# by two layers at once: the raw OpenAI shim (``embeddings.create``)
# and the LangChain embeddings shim (``OpenAIEmbeddings.embed_*``,
# which calls the same SDK underneath). The raw shim runs *inside* the
# LangChain method, so it sets this flag when it emits; the LangChain
# wrapper resets it before the call and skips its own emit if the raw
# shim already captured the call. The raw layer wins because it has the
# provider's exact reported usage. A contextvar (not a threadlocal)
# keeps the signal correct across async embed paths.
_raw_embedding_captured: ContextVar[bool] = ContextVar(
    "inkfoot_raw_embedding_captured", default=False
)


def signal_raw_embedding_captured() -> None:
    """Mark that the raw-SDK embeddings shim emitted for the call
    currently on the stack. Read by the LangChain embeddings shim to
    suppress a duplicate emit. No-op outside a LangChain capture
    scope."""
    try:
        _raw_embedding_captured.set(True)
    except Exception:  # pragma: no cover — defensive
        pass


def raw_embedding_captured_reset(value: bool = False):
    """Reset the cross-layer signal to ``value`` and return the token
    for :func:`raw_embedding_captured_restore`. Used by the LangChain
    embeddings shim to bracket one ``embed_*`` call."""
    return _raw_embedding_captured.set(value)


def raw_embedding_captured_get() -> bool:
    return _raw_embedding_captured.get()


def raw_embedding_captured_restore(token) -> None:
    try:
        _raw_embedding_captured.reset(token)
    except Exception:  # pragma: no cover — defensive
        pass


def emit_embedding_call(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    batch_size: int,
    storage: "Storage",
    occurred_at: Optional[int] = None,
    token_count_estimated: bool = False,
    run_id: Optional[str] = None,
    signal_raw: bool = False,
) -> None:
    """Write one ``embedding_call`` event.

    Embeddings are accounted *separately* from the causal token
    ledger: this event kind never contributes to ``llm_call`` totals,
    the per-category attribution chart, or the projected ``runs.total_*``
    columns (the aggregator skips it). The payload carries the
    input-token count, the batch size (number of inputs in the call),
    the resolved provider/model, and an estimated cost in nanodollars
    (``None`` when the model isn't priced).

    ``token_count_estimated`` records whether the token count came
    from the tokeniser fallback (the provider didn't report usage) so
    reports can be honest about approximate numbers.

    ``signal_raw`` is set by the raw-SDK embeddings shim so the
    LangChain embeddings shim can dedup a call both layers observed.

    Callers wrap this in :func:`safely_run` — a failure here logs and
    is swallowed, never reaching the user's embeddings call.
    """
    from inkfoot.pricing import (  # noqa: PLC0415
        estimate_embedding_nanodollars,
    )

    if signal_raw:
        signal_raw_embedding_captured()

    rid = run_id or ensure_active_run(storage, now_ms=_now_ms())
    at = occurred_at if occurred_at is not None else _now_ms()
    safe_input = max(0, int(input_tokens))
    estimated_nd = estimate_embedding_nanodollars(provider, model, safe_input)
    payload = {
        "provider": provider,
        "model": model,
        "input_tokens": safe_input,
        "batch_size": max(0, int(batch_size)),
        "estimated_nanodollars": (
            int(estimated_nd) if estimated_nd is not None else None
        ),
        "token_count_estimated": bool(token_count_estimated),
    }
    storage.insert_event(
        event_id=str(ULID()),
        run_id=rid,
        kind="embedding_call",
        occurred_at=at,
        sequence=_next_sequence(rid),
        payload_json=json.dumps(payload, default=str),
        capture_mode="metadata",
    )


def _response_dedup_id(response: Any) -> Optional[str]:
    """Best-effort provider response id off a raw SDK response.

    Anthropic ``Message.id`` and OpenAI ``ChatCompletion.id`` (attr
    or dict key) are the shapes this needs to catch; anything
    without an id simply opts out of cross-layer dedup."""
    if response is None:
        return None
    if isinstance(response, dict):
        candidate = response.get("id") or response.get("response_id")
    else:
        candidate = getattr(response, "id", None) or getattr(
            response, "response_id", None
        )
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _merge_estimation_flags(neutral_call: Any, extra: tuple[str, ...]) -> Any:
    """Append ``extra`` flags to a translated event, preserving order
    and dropping anything already present (a category the translator
    flagged shouldn't appear twice)."""
    from dataclasses import replace

    merged = list(neutral_call.estimation_flags)
    seen = set(merged)
    for flag in extra:
        if flag not in seen:
            merged.append(flag)
            seen.add(flag)
    return replace(neutral_call, estimation_flags=tuple(merged))


def _fold_into_summariser_tokens(neutral_call: Any) -> Any:
    """Re-attribute a summariser helper call's structural input to the
    ledger's ``summariser_tokens`` category.

    The translator attributes the sub-call's prompt like any other
    request (the oversized tool result it is condensing lands in
    ``user_input_tokens``), but the *cause* of every one of those
    tokens is the summariser. Folding the full structural input into
    ``summariser_tokens`` preserves ``input_total`` — so pricing and
    the attribution invariant are untouched — while the report's bar
    chart shows the overhead under the category that explains it.
    ``output_tokens`` and the cache overlays stay where they are; the
    event's ``metadata`` flags the call so reports can roll up the
    sub-call's full input + output cost.
    """
    from dataclasses import replace

    from inkfoot.ledger import CausalTokenLedger

    old = neutral_call.ledger
    folded = CausalTokenLedger(
        summariser_tokens=old.input_total,
        cache_creation_tokens=old.cache_creation_tokens,
        cache_read_tokens=old.cache_read_tokens,
        output_tokens=old.output_tokens,
    )
    metadata = dict(neutral_call.metadata)
    metadata[SUMMARISER_CALL_METADATA_KEY] = True
    return replace(neutral_call, ledger=folded, metadata=metadata)


# Cap on serialised error messages so a giant provider trace doesn't
# blow out a JSON column.
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

    Failures carry no provider response id, so the cross-layer
    dedup here keys on the exception's *identity* instead: the
    first layer to emit records the exception object per run, and a
    later sighting of the same exception — or of a wrapper whose
    ``__cause__``/``__context__`` chain contains it — is skipped.
    An SDK failure is observed by the shim first (it wraps the SDK
    call), so its event wins; a failure raised above the SDK never
    reaches the shim, and the handler's event passes through.

    Without this, the runaway-retry-loop smell would under-count
    by exactly the failure rate.
    """
    from dataclasses import asdict

    from inkfoot._run_lifecycle import _record_emitted_error  # noqa: PLC0415
    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.normalise import NeutralCall, NeutralError

    first_sighting = safely_run(
        _record_emitted_error,
        ctx.run_id,
        exc,
        fallback=True,
        hook_label="_record_emitted_error",
    )
    if not first_sighting:
        _LOG.debug(
            "error %s already recorded for run %s; "
            "skipping duplicate error emit",
            type(exc).__name__,
            ctx.run_id,
        )
        return

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
