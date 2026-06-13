"""``OpenAIResponsesShim`` — Responses-API sibling of
:class:`OpenAIShim`.

Wraps ``openai.resources.responses.Responses.create`` and
``AsyncResponses.create`` with the same lifecycle (install,
uninstall, sync+async, hook isolation). Azure OpenAI needs no
separate shim: ``AzureOpenAI`` clients route their Responses calls
through the same patched classes.

The translator is the Responses one
(:class:`~inkfoot.normalise.openai_responses.OpenAIResponsesTranslator`);
everything else flows through the shared
:mod:`inkfoot.shims._emit` pipeline — including cross-layer dedup,
which keys on the wire response's ``id`` (``resp_...``) so a call
observed by both this shim and the LangChain callback handler lands
exactly once.

SDKs that predate the Responses API simply don't expose the target
classes; :meth:`install` then reports ``False`` and the rest of
instrumentation proceeds untouched.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.contracts.runtime import enforce_before_call
from inkfoot.normalise.openai_responses import OpenAIResponsesTranslator
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import (
    build_call_context,
    emit_llm_call,
    emit_llm_call_error,
)
from inkfoot.shims._isolation import safely_run

_LOG = logging.getLogger("inkfoot.shims.openai_responses")

_PROVIDER = "openai"
_DEFAULT_MODEL = ""


class OpenAIResponsesShim:
    """Per-process Responses-API shim. Install via :meth:`install`,
    restore via :meth:`uninstall`."""

    provider = _PROVIDER

    def __init__(self, storage: Any, capture_mode_getter: Callable[[], str]) -> None:
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None
        self._translator = OpenAIResponsesTranslator()

    def install(self) -> bool:
        if self._installed:
            return True
        try:
            from openai.resources.responses import (  # type: ignore[import-not-found]
                AsyncResponses,
                Responses,
            )
        except ImportError:
            _LOG.debug(
                "openai SDK without a Responses surface; "
                "OpenAIResponsesShim skipped"
            )
            return False

        sync_target: Callable[..., Any] = Responses.create  # type: ignore[assignment]
        async_target: Callable[..., Any] = AsyncResponses.create  # type: ignore[assignment]
        if getattr(sync_target, "__inkfoot_shim__", False):
            self._installed = True
            return True

        self._original_sync = sync_target
        self._original_async = async_target

        Responses.create = self._build_sync_wrapper(sync_target)  # type: ignore[assignment]
        if inspect.iscoroutinefunction(async_target):
            AsyncResponses.create = self._build_async_wrapper(  # type: ignore[assignment]
                async_target
            )
        else:
            AsyncResponses.create = self._build_sync_wrapper(  # type: ignore[assignment]
                async_target
            )

        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from openai.resources.responses import (  # type: ignore[import-not-found]
                AsyncResponses,
                Responses,
            )
        except ImportError:  # pragma: no cover — defensive
            self._installed = False
            return
        if self._original_sync is not None:
            Responses.create = self._original_sync  # type: ignore[assignment]
        if self._original_async is not None:
            AsyncResponses.create = self._original_async  # type: ignore[assignment]
        self._original_sync = None
        self._original_async = None
        self._installed = False

    # ------------------------------------------------------------------
    # Wrappers + dispatch — same shape as OpenAIShim
    # ------------------------------------------------------------------

    def _build_sync_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            return shim._dispatch_sync(original, client_self, args, kwargs)

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
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

    def _dispatch_sync(
        self,
        original: Callable[..., Any],
        client_self: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        before_decisions, ctx, started_at = self._before(kwargs)
        # Outside isolation: a contract ``block`` raises straight to
        # the caller; ``switch_to_cheap_model`` rewrites the model in
        # ``kwargs`` before the call is made.
        enforce_before_call(ctx)
        # Provider exceptions propagate; we record a NeutralError
        # event first so reports don't under-count failures. The
        # re-raised exception is identical to what the user would
        # have seen without instrumentation.
        try:
            response = original(client_self, *args, **kwargs)
        except Exception as exc:
            ended_at = int(time.time() * 1000)
            self._on_provider_error(
                ctx, exc, before_decisions, started_at, ended_at
            )
            raise
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
        enforce_before_call(ctx)
        try:
            response = await original(client_self, *args, **kwargs)
        except Exception as exc:
            ended_at = int(time.time() * 1000)
            self._on_provider_error(
                ctx, exc, before_decisions, started_at, ended_at
            )
            raise
        ended_at = int(time.time() * 1000)
        self._after(ctx, response, before_decisions, started_at, ended_at)
        return response

    def _on_provider_error(
        self,
        ctx: Any,
        exc: Exception,
        before_decisions: list,
        started_at: int,
        ended_at: int,
    ) -> None:
        """Wrap the failure-event emit in ``safely_run`` so a bug
        inside our own error path still can't propagate to the
        user."""
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

    def _before(self, kwargs: dict):
        started_at = int(time.time() * 1000)
        ctx = safely_run(
            build_call_context,
            provider=_PROVIDER,
            model=kwargs.get("model", _DEFAULT_MODEL),
            request_kwargs=kwargs,
            storage=self._storage,
            hook_label="openai_responses.build_call_context",
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
            response_id=self._response_id(response),
            hook_label="emit_llm_call",
        )
        safely_run(
            PolicyRegistry.after_call,
            ctx,
            response,
            hook_label="PolicyRegistry.after_call",
        )

    @staticmethod
    def _response_id(response: Any) -> Optional[str]:
        """The wire response's ``id`` (``resp_...``) — the
        cross-layer dedup key shared with the LangChain handler,
        which reads the same id off ``response_metadata``."""
        if response is None:
            return None
        if isinstance(response, dict):
            candidate = response.get("id")
        else:
            candidate = getattr(response, "id", None)
        if isinstance(candidate, str) and candidate:
            return candidate
        return None
