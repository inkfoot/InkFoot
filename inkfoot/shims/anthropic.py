"""``AnthropicShim`` — monkey-patches Anthropic's
``messages.create`` (sync + async) so every call emits an Inkfoot
event without touching the user-visible behaviour.

Per §5.2:

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

import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.normalise.anthropic import AnthropicTranslator
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import build_call_context, emit_llm_call
from inkfoot.shims._isolation import safely_run

_LOG = logging.getLogger("inkfoot.shims.anthropic")

_PROVIDER = "anthropic"
_DEFAULT_MODEL = ""


class AnthropicShim:
    """Per-process Anthropic shim. Install via :meth:`install`,
    restore via :meth:`uninstall`. The shim holds references to the
    original method pointers so uninstall is precise."""

    def __init__(self, storage: Any, capture_mode_getter: Callable[[], str]) -> None:
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None
        self._translator = AnthropicTranslator()

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

        self._installed = True
        return True

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
        self._original_sync = None
        self._original_async = None
        self._installed = False

    # ------------------------------------------------------------------
    # Wrappers
    # ------------------------------------------------------------------

    def _build_sync_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

        def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            return shim._dispatch_sync(original, client_self, args, kwargs)

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _build_async_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

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
        before_decisions, ctx, started_at = self._before(kwargs)
        # Invoke original — provider exceptions must propagate.
        response = original(client_self, *args, **kwargs)
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
        before_decisions, ctx, started_at = self._before(kwargs)
        response = await original(client_self, *args, **kwargs)
        ended_at = int(time.time() * 1000)
        self._after(ctx, response, before_decisions, started_at, ended_at)
        return response

    def _before(self, kwargs: dict):
        started_at = int(time.time() * 1000)
        ctx = safely_run(
            build_call_context,
            provider=_PROVIDER,
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
            translator=self._translator,
            before_decisions=before_decisions,
        )
        safely_run(emit_llm_call, **emit_llm_call_args, hook_label="emit_llm_call")
        safely_run(
            PolicyRegistry.after_call,
            ctx,
            response,
            hook_label="PolicyRegistry.after_call",
        )
