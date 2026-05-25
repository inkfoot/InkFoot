"""``OpenAIShim`` — mirror of :class:`AnthropicShim` for the OpenAI
SDK.

Wraps ``openai.resources.chat.completions.Completions.create`` and
``AsyncCompletions.create`` per §5.2. Same lifecycle (install,
uninstall, sync+async, hook isolation). The translator is the
OpenAI one; everything else flows through the shared
:mod:`inkfoot.shims._emit` pipeline.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.normalise.openai import OpenAITranslator
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import build_call_context, emit_llm_call
from inkfoot.shims._isolation import safely_run

_LOG = logging.getLogger("inkfoot.shims.openai")

_PROVIDER = "openai"
_DEFAULT_MODEL = ""


class OpenAIShim:
    """Per-process OpenAI shim. Install via :meth:`install`,
    restore via :meth:`uninstall`."""

    def __init__(self, storage: Any, capture_mode_getter: Callable[[], str]) -> None:
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None
        self._translator = OpenAITranslator()

    def install(self) -> bool:
        if self._installed:
            return True
        try:
            from openai.resources.chat.completions import (  # type: ignore[import-not-found]
                AsyncCompletions,
                Completions,
            )
        except ImportError:
            _LOG.debug("openai SDK not importable; OpenAIShim skipped")
            return False

        sync_target: Callable[..., Any] = Completions.create  # type: ignore[assignment]
        async_target: Callable[..., Any] = AsyncCompletions.create  # type: ignore[assignment]
        if getattr(sync_target, "__inkfoot_shim__", False):
            self._installed = True
            return True

        self._original_sync = sync_target
        self._original_async = async_target

        Completions.create = self._build_sync_wrapper(sync_target)  # type: ignore[assignment]
        if inspect.iscoroutinefunction(async_target):
            AsyncCompletions.create = self._build_async_wrapper(  # type: ignore[assignment]
                async_target
            )
        else:
            AsyncCompletions.create = self._build_sync_wrapper(  # type: ignore[assignment]
                async_target
            )

        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from openai.resources.chat.completions import (  # type: ignore[import-not-found]
                AsyncCompletions,
                Completions,
            )
        except ImportError:  # pragma: no cover — defensive
            self._installed = False
            return
        if self._original_sync is not None:
            Completions.create = self._original_sync  # type: ignore[assignment]
        if self._original_async is not None:
            AsyncCompletions.create = self._original_async  # type: ignore[assignment]
        self._original_sync = None
        self._original_async = None
        self._installed = False

    # ------------------------------------------------------------------
    # Wrappers + dispatch — same shape as AnthropicShim
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

    def _dispatch_sync(
        self,
        original: Callable[..., Any],
        client_self: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        before_decisions, ctx, started_at = self._before(kwargs)
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
            hook_label="openai.build_call_context",
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
        safely_run(
            emit_llm_call,
            ctx=ctx,
            response=response,
            started_at=started_at,
            ended_at=ended_at,
            storage=self._storage,
            capture_mode=self._capture_mode_getter(),
            translator=self._translator,
            before_decisions=before_decisions,
            hook_label="emit_llm_call",
        )
        safely_run(
            PolicyRegistry.after_call,
            ctx,
            response,
            hook_label="PolicyRegistry.after_call",
        )
