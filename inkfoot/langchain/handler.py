"""LangChain callback handler — captures chat-model calls made
through LangChain (and anything built on it) as Inkfoot events.

The handler hooks the LangChain callback lifecycle:

* ``on_chat_model_start`` — snapshots the message list, the bound
  tools array (``invocation_params["tools"]``), the run metadata,
  and the model name, keyed by the LangChain ``run_id``.
* ``on_llm_end`` — reads the LangChain-normalised
  ``usage_metadata`` off the result, resolves provider + model, and
  emits one ``llm_call`` event through the same pipeline the
  raw-SDK shims use (causal split, pricing, contracts, policies).
* ``on_llm_error`` — records a failure event, unless the raw-SDK
  shim for that provider is installed (the shim already saw the
  exception first-hand).

Every hook is isolation-wrapped: an Inkfoot bug logs a warning and
the user's chain continues untouched. When both this handler and a
raw-SDK shim observe the same call, the provider response id keeps
the ledger single-entry — see ``inkfoot.shims._emit``.

The handler is normally installed process-wide by
:func:`inkfoot.langchain.instrument` (itself wired into
``inkfoot.instrument``), but it is also a plain LangChain callback
handler — passing it explicitly via ``callbacks=[handler]`` works
the same way.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from inkfoot.normalise.langchain import (
    LangChainTranslator,
    map_provider,
    summarise_response,
)
from inkfoot.shims._emit import (
    build_call_context,
    emit_llm_call,
    emit_llm_call_error,
)
from inkfoot.shims._isolation import safely_run

__all__ = ["InkfootCallbackHandler"]

_LOG = logging.getLogger("inkfoot.langchain")


# Upper bound on in-flight call snapshots. A snapshot lives from
# ``on_chat_model_start`` to ``on_llm_end``/``on_llm_error``; the cap
# only matters when end callbacks are lost (crashed chains), where the
# oldest snapshot is evicted first.
_PENDING_LIMIT = 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class _PendingCall:
    """Request-side snapshot taken at ``on_chat_model_start`` /
    ``on_llm_start``, consumed by the matching end/error callback."""

    started_at: int
    messages: tuple[Any, ...] = ()
    tools: tuple[Any, ...] = ()
    serialized_name: str = ""
    invocation_params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class InkfootCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that emits Inkfoot ``llm_call``
    events.

    Stateless from LangChain's point of view (safe to share across
    chains and threads); internally it keeps a bounded, lock-guarded
    map of in-flight call snapshots keyed by the LangChain run id.

    The handler is inert until :func:`inkfoot.instrument` has run —
    without an Inkfoot storage backend there is nowhere to write, so
    callbacks become no-ops rather than warnings.
    """

    # Run inside the caller's context (not a callback thread pool) so
    # the handler sees the ambient Inkfoot run from the calling
    # context's contextvars.
    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, _PendingCall] = OrderedDict()
        self._translator = LangChainTranslator()
        self._active = True

    # -- activation ----------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._active

    def activate(self) -> None:
        self._active = True

    def deactivate(self) -> None:
        """Turn the handler into a no-op. LangChain has no global
        unregister API, so ``inkfoot.langchain.uninstrument`` flips
        this flag instead of removing the handler."""
        self._active = False

    # -- snapshot bookkeeping -------------------------------------------

    def _remember(self, run_id: str, pending: _PendingCall) -> None:
        with self._lock:
            self._pending.pop(run_id, None)
            self._pending[run_id] = pending
            while len(self._pending) > _PENDING_LIMIT:
                self._pending.popitem(last=False)

    def _take(self, run_id: str) -> Optional[_PendingCall]:
        with self._lock:
            return self._pending.pop(run_id, None)

    @staticmethod
    def _runtime() -> tuple[Any, str]:
        """Resolve the live storage backend and capture mode from the
        instrumented runtime. ``(None, ...)`` when Inkfoot isn't
        instrumented."""
        from inkfoot import _instrument

        storage = getattr(_instrument, "_STORAGE", None)
        capture_mode = getattr(_instrument, "_CAPTURE_MODE", None) or "standard"
        return storage, capture_mode

    # -- start callbacks -------------------------------------------------

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        safely_run(
            self._record_start,
            serialized,
            messages,
            run_id,
            metadata,
            kwargs,
            hook_label="InkfootCallbackHandler.on_chat_model_start",
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        # Completion-style models hand over rendered prompt strings;
        # recast them as single human messages so the causal split
        # has something to attribute.
        synthetic = [[{"type": "human", "content": p} for p in prompts or []]]
        safely_run(
            self._record_start,
            serialized,
            synthetic,
            run_id,
            metadata,
            kwargs,
            hook_label="InkfootCallbackHandler.on_llm_start",
        )

    def _record_start(
        self,
        serialized: Optional[dict[str, Any]],
        message_batches: list[list[Any]],
        run_id: UUID,
        metadata: Optional[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> None:
        if not self._active:
            return
        # The callback manager fires once per inner message list with
        # a fresh run_id, so there's normally exactly one batch;
        # flatten defensively in case a custom model fans differently.
        batches = message_batches or []
        if len(batches) == 1:
            msgs = list(batches[0] or [])
        else:
            msgs = [m for batch in batches for m in (batch or [])]

        invocation_params = kwargs.get("invocation_params") or {}
        if not isinstance(invocation_params, dict):
            invocation_params = {}
        raw_tools = invocation_params.get("tools") or []
        if not isinstance(raw_tools, (list, tuple)):
            raw_tools = []
        name = (serialized or {}).get("name")
        self._remember(
            str(run_id),
            _PendingCall(
                started_at=_now_ms(),
                messages=tuple(msgs),
                tools=tuple(t for t in raw_tools if isinstance(t, dict)),
                serialized_name=name if isinstance(name, str) else "",
                invocation_params=dict(invocation_params),
                metadata=dict(metadata or {}),
            ),
        )

    # -- end / error callbacks --------------------------------------------

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        safely_run(
            self._record_end,
            response,
            run_id,
            hook_label="InkfootCallbackHandler.on_llm_end",
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        safely_run(
            self._record_error,
            error,
            run_id,
            hook_label="InkfootCallbackHandler.on_llm_error",
        )

    def _record_end(self, response: Any, run_id: UUID) -> None:
        key = str(run_id)
        pending = self._take(key)
        if not self._active:
            return
        storage, capture_mode = self._runtime()
        if storage is None:
            _LOG.debug(
                "inkfoot not instrumented; dropping LangChain call %s", key
            )
            return

        ended_at = _now_ms()
        if pending is None:
            # End without a matching start (evicted snapshot or a
            # custom model that skipped the start callback). Emit
            # anyway — response-side numbers are still real.
            pending = _PendingCall(started_at=ended_at)

        summary = summarise_response(response)
        model, raw_provider, provider = self._identify(summary, pending)
        request = self._build_request(
            key, model, raw_provider, provider, pending
        )

        ctx = build_call_context(
            provider=provider,
            model=model,
            request_kwargs=request,
            storage=storage,
        )
        emit_llm_call(
            ctx=ctx,
            response=response,
            started_at=pending.started_at,
            ended_at=ended_at,
            storage=storage,
            capture_mode=capture_mode,
            translator=self._translator,
            before_decisions=[],
            response_id=summary.response_id,
        )

    def _record_error(self, error: BaseException, run_id: UUID) -> None:
        key = str(run_id)
        pending = self._take(key)
        if not self._active:
            return
        storage, capture_mode = self._runtime()
        if storage is None:
            _LOG.debug(
                "inkfoot not instrumented; dropping LangChain error %s", key
            )
            return

        ended_at = _now_ms()
        if pending is None:
            pending = _PendingCall(started_at=ended_at)

        model, raw_provider, provider = self._identify(None, pending)

        # Failed calls carry no provider response id; the dedup in
        # ``emit_llm_call_error`` keys on the exception's identity
        # (following ``__cause__``/``__context__``) instead. An SDK
        # failure the provider's raw shim already recorded is skipped
        # there; a failure raised above the SDK — tool binding, a
        # partner-package bug — never reached the shim, so it lands
        # through this emit.
        request = self._build_request(
            key, model, raw_provider, provider, pending
        )
        ctx = build_call_context(
            provider=provider,
            model=model,
            request_kwargs=request,
            storage=storage,
        )
        emit_llm_call_error(
            ctx=ctx,
            exc=error,
            started_at=pending.started_at,
            ended_at=ended_at,
            storage=storage,
            capture_mode=capture_mode,
            before_decisions=[],
        )

    # -- embeddings (forward stubs) ----------------------------------------

    def on_embeddings_start(self, *args: Any, **kwargs: Any) -> None:
        """Embedding-call capture is not implemented yet; the hook
        exists so registration is already in place when it is."""
        return None

    def on_embeddings_end(self, *args: Any, **kwargs: Any) -> None:
        """See :meth:`on_embeddings_start`."""
        return None

    # -- shared resolution helpers ------------------------------------------

    @staticmethod
    def _identify(
        summary: Any, pending: _PendingCall
    ) -> tuple[str, Optional[str], str]:
        """Resolve ``(model, raw_provider, provider)`` from the
        response summary (when there is one) with request-snapshot
        fallbacks: LangChain's standard ``ls_*`` tracing metadata,
        the invocation params, then the serialised class name."""
        candidates = [
            getattr(summary, "model_name", None),
            pending.invocation_params.get("model"),
            pending.invocation_params.get("model_name"),
            pending.metadata.get("ls_model_name"),
            pending.serialized_name,
        ]
        model = next(
            (c for c in candidates if isinstance(c, str) and c), ""
        )

        raw_provider = getattr(summary, "model_provider", None)
        if not isinstance(raw_provider, str) or not raw_provider:
            ls = pending.metadata.get("ls_provider")
            raw_provider = ls if isinstance(ls, str) and ls else None
        return model, raw_provider, map_provider(raw_provider, model)

    @staticmethod
    def _build_request(
        run_key: str,
        model: str,
        raw_provider: Optional[str],
        provider: str,
        pending: _PendingCall,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "captured_by": "langchain_handler",
            "langchain_run_id": run_key,
        }
        if raw_provider and raw_provider.lower() != provider:
            metadata["langchain_provider"] = raw_provider
        return {
            "provider": provider,
            "model": model,
            "messages": list(pending.messages),
            "tools": list(pending.tools),
            "metadata": metadata,
        }
