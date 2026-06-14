"""``AnthropicShim`` — monkey-patches Anthropic's
``messages.create`` (sync + async) so every call emits an Inkfoot
event without touching the user-visible behaviour.

Guarantees:

* Original function pointer is preserved as
  ``_original_messages_create`` / ``_original_async_messages_create``;
  :meth:`uninstall` restores them.
* The wrapper runs all policy ``before_call`` hooks under hook
  isolation, invokes the original (no try/except — provider
  errors must bubble), runs the translator + post-call hooks, and
  returns the **unmodified** original response.
* Async variants are detected at install time via
  ``inspect.iscoroutinefunction`` and wrapped accordingly.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.contracts.runtime import enforce_before_call
from inkfoot.normalise.anthropic import AnthropicTranslator
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import (
    build_call_context,
    emit_llm_call,
    emit_llm_call_error,
)
from inkfoot.shims._isolation import safely_run
from inkfoot.shims._streaming import (
    _AnthropicStreamProbe,
    _AsyncStreamedCallObserver,
    _AsyncStreamManagerProxy,
    _StreamedCallObserver,
    _StreamManagerProxy,
    _StreamRecorder,
)

_LOG = logging.getLogger("inkfoot.shims.anthropic")

_PROVIDER = "anthropic"
_PROVIDER_BEDROCK = "anthropic_bedrock"
_DEFAULT_MODEL = ""


def _bedrock_client_types() -> tuple[type, ...]:
    """The ``AnthropicBedrock`` client classes, or ``()`` when the
    ``anthropic[bedrock]`` extra isn't importable.

    Resolved per call rather than cached so the result always tracks
    the live ``anthropic`` module — by the time a wrapped
    ``messages.create`` runs, the import is a ``sys.modules`` hit.
    Any failure (extra absent, boto3 broken) collapses to ``()`` so a
    user without Bedrock sees the direct-API provider and zero change.
    """
    try:
        import anthropic  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover — anthropic is importable here
        return ()
    found: list[type] = []
    for name in ("AnthropicBedrock", "AsyncAnthropicBedrock"):
        cls = getattr(anthropic, name, None)
        if isinstance(cls, type):
            found.append(cls)
    return tuple(found)


def _resolve_provider(client_self: Any) -> str:
    """``"anthropic_bedrock"`` when the wrapped call came from an
    ``AnthropicBedrock`` client, else ``"anthropic"``.

    The patched method is ``Messages.create``; ``client_self`` is the
    ``Messages`` resource, which the SDK builds with a back-reference
    to its owning client in ``_client``. Detecting the Bedrock client
    by identity lets the single ``Messages`` patch serve both the
    direct and Bedrock clients (they share the resource class). Never
    raises — an unrecognised shape resolves to the direct provider.
    """
    bedrock_types = _bedrock_client_types()
    if not bedrock_types:
        return _PROVIDER
    client = getattr(client_self, "_client", None)
    if client is not None and isinstance(client, bedrock_types):
        return _PROVIDER_BEDROCK
    return _PROVIDER


class AnthropicShim:
    """Per-process Anthropic shim. Install via :meth:`install`,
    restore via :meth:`uninstall`. The shim holds references to the
    original method pointers so uninstall is precise."""

    provider = _PROVIDER

    def __init__(self, storage: Any, capture_mode_getter: Callable[[], str]) -> None:
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None
        self._original_stream: Optional[Callable[..., Any]] = None
        self._original_async_stream: Optional[Callable[..., Any]] = None
        self._translator = AnthropicTranslator()
        self._bedrock_translator = AnthropicTranslator(
            provider=_PROVIDER_BEDROCK
        )

    def _translator_for(self, ctx: Any) -> AnthropicTranslator:
        """Pick the translator whose provider matches the call
        context so a Bedrock call is tagged and priced under
        ``anthropic_bedrock`` while direct calls stay ``anthropic``."""
        if getattr(ctx, "provider", _PROVIDER) == _PROVIDER_BEDROCK:
            return self._bedrock_translator
        return self._translator

    # ------------------------------------------------------------------
    # Install / uninstall
    # ------------------------------------------------------------------

    def install(self) -> bool:
        """Return ``True`` if the shim was installed (anthropic was
        importable + the target attributes existed). ``False`` is a
        clean "SDK not installed; skip"."""
        if self._installed:
            return True
        try:
            from anthropic.resources.messages import (  # type: ignore[import-not-found]
                AsyncMessages,
                Messages,
            )
        except ImportError:
            _LOG.debug("anthropic SDK not importable; AnthropicShim skipped")
            return False

        # Capture originals before wrapping so uninstall can put them back.
        sync_target: Callable[..., Any] = Messages.create  # type: ignore[assignment]
        async_target: Callable[..., Any] = AsyncMessages.create  # type: ignore[assignment]
        if getattr(sync_target, "__inkfoot_shim__", False):
            # Already shimmed by something else (e.g. a leftover
            # uninstall). Don't double-wrap.
            self._installed = True
            return True

        self._original_sync = sync_target
        self._original_async = async_target

        Messages.create = self._build_sync_wrapper(sync_target)  # type: ignore[assignment]
        if inspect.iscoroutinefunction(async_target):
            AsyncMessages.create = self._build_async_wrapper(  # type: ignore[assignment]
                async_target
            )
        else:
            # Some old SDK versions implement async via factory; fall
            # back to a sync wrapper rather than break.
            AsyncMessages.create = self._build_sync_wrapper(async_target)  # type: ignore[assignment]

        # The ergonomic ``messages.stream()`` helpers post directly
        # rather than routing through ``create``, so they need their own
        # patch. The lookups are guarded so an SDK build without the
        # helper can't break install.
        self._install_stream_helpers(Messages, AsyncMessages)

        self._installed = True
        return True

    def _install_stream_helpers(
        self, messages_cls: Any, async_messages_cls: Any
    ) -> None:
        sync_stream = getattr(messages_cls, "stream", None)
        if callable(sync_stream) and not getattr(
            sync_stream, "__inkfoot_shim__", False
        ):
            self._original_stream = sync_stream
            messages_cls.stream = self._build_stream_wrapper(
                sync_stream, async_mode=False
            )
        async_stream = getattr(async_messages_cls, "stream", None)
        if callable(async_stream) and not getattr(
            async_stream, "__inkfoot_shim__", False
        ):
            self._original_async_stream = async_stream
            async_messages_cls.stream = self._build_stream_wrapper(
                async_stream, async_mode=True
            )

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from anthropic.resources.messages import (  # type: ignore[import-not-found]
                AsyncMessages,
                Messages,
            )
        except ImportError:  # pragma: no cover — defensive
            self._installed = False
            return
        if self._original_sync is not None:
            Messages.create = self._original_sync  # type: ignore[assignment]
        if self._original_async is not None:
            AsyncMessages.create = self._original_async  # type: ignore[assignment]
        if self._original_stream is not None:
            Messages.stream = self._original_stream  # type: ignore[assignment]
        if self._original_async_stream is not None:
            AsyncMessages.stream = self._original_async_stream  # type: ignore[assignment]
        self._original_sync = None
        self._original_async = None
        self._original_stream = None
        self._original_async_stream = None
        self._installed = False

    # ------------------------------------------------------------------
    # Wrappers
    # ------------------------------------------------------------------

    def _build_sync_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            return shim._dispatch_sync(original, client_self, args, kwargs)

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        # functools.wraps already sets __wrapped__; the line below is
        # a no-op but kept explicit for grep'ability.
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _build_async_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        async def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            return await shim._dispatch_async(
                original, client_self, args, kwargs
            )

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_sync(
        self,
        original: Callable[..., Any],
        client_self: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        provider = safely_run(
            _resolve_provider,
            client_self,
            fallback=_PROVIDER,
            hook_label="anthropic._resolve_provider",
        )
        before_decisions, ctx, started_at = self._before(kwargs, provider)
        # Contract enforcement runs outside isolation so a ``block``
        # decision can raise ``PolicyBlocked`` straight to the caller
        # and the SDK request is never made. A ``switch_to_cheap_model``
        # decision mutates ``kwargs`` in place before the call.
        enforce_before_call(ctx)
        streaming = bool(kwargs.get("stream"))
        # Provider exceptions must propagate to the user — but we
        # record a NeutralError event first so the run shows
        # "N attempted, 1 failed" instead of a silent gap. Re-raise after the emit so user
        # call sites see the exact same exception they would have
        # without instrumentation.
        try:
            response = original(client_self, *args, **kwargs)
        except Exception as exc:
            ended_at = int(time.time() * 1000)
            self._on_provider_error(
                ctx, exc, before_decisions, started_at, ended_at
            )
            raise
        if streaming and ctx is not None:
            # The usage only exists once the caller drains the stream;
            # tee it and emit at close instead of now.
            return _StreamedCallObserver(
                response,
                self._make_recorder(ctx, before_decisions, started_at),
            )
        ended_at = int(time.time() * 1000)
        self._after(ctx, response, before_decisions, started_at, ended_at)
        return response

    async def _dispatch_async(
        self,
        original: Callable[..., Any],
        client_self: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        provider = safely_run(
            _resolve_provider,
            client_self,
            fallback=_PROVIDER,
            hook_label="anthropic._resolve_provider",
        )
        before_decisions, ctx, started_at = self._before(kwargs, provider)
        enforce_before_call(ctx)
        streaming = bool(kwargs.get("stream"))
        try:
            response = await original(client_self, *args, **kwargs)
        except Exception as exc:
            ended_at = int(time.time() * 1000)
            self._on_provider_error(
                ctx, exc, before_decisions, started_at, ended_at
            )
            raise
        if streaming and ctx is not None:
            return _AsyncStreamedCallObserver(
                response,
                self._make_recorder(ctx, before_decisions, started_at),
            )
        ended_at = int(time.time() * 1000)
        self._after(ctx, response, before_decisions, started_at, ended_at)
        return response

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _make_recorder(
        self, ctx: Any, before_decisions: list, started_at: int
    ) -> _StreamRecorder:
        return _StreamRecorder(
            ctx=ctx,
            probe=_AnthropicStreamProbe(model=ctx.model),
            started_at=started_at,
            storage=self._storage,
            capture_mode_getter=self._capture_mode_getter,
            translator=self._translator_for(ctx),
            before_decisions=before_decisions,
        )

    def _build_stream_wrapper(
        self, original: Callable[..., Any], *, async_mode: bool
    ) -> Callable[..., Any]:
        """Wrap ``messages.stream()`` / ``astream()``. Both return a
        context manager synchronously (the request fires on enter), so
        the wrapper itself is synchronous in both cases; the proxy
        handles the sync-vs-async ``with`` protocol."""
        shim = self

        @functools.wraps(original)
        def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            provider = safely_run(
                _resolve_provider,
                client_self,
                fallback=_PROVIDER,
                hook_label="anthropic._resolve_provider",
            )
            before_decisions, ctx, started_at = shim._before(kwargs, provider)
            enforce_before_call(ctx)
            manager = original(client_self, *args, **kwargs)
            if ctx is None:
                return manager

            def make_recorder() -> _StreamRecorder:
                return shim._make_recorder(ctx, before_decisions, started_at)

            proxy_cls = (
                _AsyncStreamManagerProxy
                if async_mode
                else _StreamManagerProxy
            )
            return proxy_cls(manager, make_recorder)

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _on_provider_error(
        self,
        ctx: Any,
        exc: Exception,
        before_decisions: list,
        started_at: int,
        ended_at: int,
    ) -> None:
        """Best-effort failure-event emit. Wrapped in ``safely_run``
        so a bug *in our error-emit path* still can't propagate."""
        if ctx is None:
            return
        safely_run(
            emit_llm_call_error,
            ctx=ctx,
            exc=exc,
            started_at=started_at,
            ended_at=ended_at,
            storage=self._storage,
            capture_mode=self._capture_mode_getter(),
            before_decisions=before_decisions,
            hook_label="emit_llm_call_error",
        )

    def _before(self, kwargs: dict, provider: str = _PROVIDER):
        started_at = int(time.time() * 1000)
        ctx = safely_run(
            build_call_context,
            provider=provider,
            model=kwargs.get("model", _DEFAULT_MODEL),
            request_kwargs=kwargs,
            storage=self._storage,
            hook_label="anthropic.build_call_context",
        )
        if ctx is None:
            return [], None, started_at
        before_decisions = safely_run(
            PolicyRegistry.before_call, ctx,
            hook_label="PolicyRegistry.before_call",
            fallback=[],
        ) or []
        return before_decisions, ctx, started_at

    def _after(
        self,
        ctx: Any,
        response: Any,
        before_decisions: list,
        started_at: int,
        ended_at: int,
    ) -> None:
        if ctx is None:
            return
        emit_llm_call_args = dict(
            ctx=ctx,
            response=response,
            started_at=started_at,
            ended_at=ended_at,
            storage=self._storage,
            capture_mode=self._capture_mode_getter(),
            translator=self._translator_for(ctx),
            before_decisions=before_decisions,
        )
        safely_run(emit_llm_call, **emit_llm_call_args, hook_label="emit_llm_call")
        safely_run(
            PolicyRegistry.after_call,
            ctx,
            response,
            hook_label="PolicyRegistry.after_call",
        )
