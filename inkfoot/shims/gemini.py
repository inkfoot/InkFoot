"""``GeminiShim`` — monkey-patches
``GenerativeModel.generate_content`` (sync + async) so every Gemini
call emits an Inkfoot event without touching the user-visible
behaviour.

Two Gemini-specific wrinkles relative to the Anthropic/OpenAI shims:

* **Request synthesis.** Gemini binds ``system_instruction`` and
  ``tools`` to the model object at construction time and takes only
  ``contents`` per call. The wrapper reads those off the model
  instance and synthesises a flat request dict (``model`` /
  ``contents`` / ``system_instruction`` / ``tools`` + the per-call
  kwargs) so the translator and policies see one uniform shape.
* **Cache-resource rebinding.** When the cache-resource arm of
  ``CacheControlPlacer`` attached a ``CachedContent`` handle to the
  call context, the wrapper dispatches the original method against a
  model *rebound* to that resource
  (``GenerativeModel.from_cached_content``) — that's how a Gemini
  request "references" a cached prefix; there is no per-request
  marker. Any failure to rebind falls back to the user's original
  model object, so the call always goes through.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.contracts.runtime import enforce_before_call
from inkfoot.normalise.gemini import GeminiTranslator
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import (
    build_call_context,
    emit_llm_call,
    emit_llm_call_error,
)
from inkfoot.shims._isolation import safely_run

_LOG = logging.getLogger("inkfoot.shims.gemini")

_PROVIDER = "gemini"
_DEFAULT_MODEL = ""

# CallContext.metadata key under which the cache-resource arm of
# CacheControlPlacer hands the shim a CachedContent resource to bind
# the call to. Defined here — not on the policy — so the dispatch hot
# path can read it without importing the policy package.
CACHED_CONTENT_METADATA_KEY = "gemini_cached_content"


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        txt = part.get("text", "")
        return txt if isinstance(txt, str) else ""
    txt = getattr(part, "text", "")
    return txt if isinstance(txt, str) else ""


def _system_instruction_text(client_self: Any) -> str:
    """Flatten the model-bound system instruction to a plain string.
    The SDK normalises whatever the user passed into a Content
    object (``.parts`` of ``.text``-bearing parts); fixtures may keep
    the raw string / dict shapes."""
    raw = getattr(client_self, "_system_instruction", None)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    elif isinstance(raw, dict):
        parts = raw.get("parts") or []
    else:
        parts = getattr(raw, "parts", None) or []
    return "".join(_part_text(p) for p in parts)


def _model_name(client_self: Any) -> str:
    raw = getattr(client_self, "model_name", "") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    return raw.removeprefix("models/")


class GeminiShim:
    """Per-process Gemini shim. Install via :meth:`install`, restore
    via :meth:`uninstall`. The shim holds references to the original
    method pointers so uninstall is precise."""

    provider = _PROVIDER

    def __init__(self, storage: Any, capture_mode_getter: Callable[[], str]) -> None:
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None
        self._translator = GeminiTranslator()

    # ------------------------------------------------------------------
    # Install / uninstall
    # ------------------------------------------------------------------

    def install(self) -> bool:
        """Return ``True`` if the shim was installed
        (google-generativeai was importable + the target attributes
        existed). ``False`` is a clean "SDK not installed; skip"."""
        if self._installed:
            return True
        try:
            from google.generativeai.generative_models import (  # type: ignore[import-not-found]
                GenerativeModel,
            )
        except ImportError:
            _LOG.debug(
                "google-generativeai SDK not importable; GeminiShim skipped"
            )
            return False

        sync_target: Optional[Callable[..., Any]] = getattr(
            GenerativeModel, "generate_content", None
        )
        if sync_target is None:
            _LOG.debug(
                "GenerativeModel.generate_content missing; GeminiShim skipped"
            )
            return False
        if getattr(sync_target, "__inkfoot_shim__", False):
            self._installed = True
            return True

        self._original_sync = sync_target
        GenerativeModel.generate_content = self._build_sync_wrapper(  # type: ignore[assignment]
            sync_target
        )

        # Unlike the Anthropic/OpenAI SDKs the async variant lives on
        # the *same* class; older SDK builds may not have it at all.
        async_target: Optional[Callable[..., Any]] = getattr(
            GenerativeModel, "generate_content_async", None
        )
        if async_target is not None:
            self._original_async = async_target
            if inspect.iscoroutinefunction(async_target):
                GenerativeModel.generate_content_async = (  # type: ignore[assignment]
                    self._build_async_wrapper(async_target)
                )
            else:
                GenerativeModel.generate_content_async = (  # type: ignore[assignment]
                    self._build_sync_wrapper(async_target)
                )

        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from google.generativeai.generative_models import (  # type: ignore[import-not-found]
                GenerativeModel,
            )
        except ImportError:  # pragma: no cover — defensive
            self._installed = False
            return
        if self._original_sync is not None:
            GenerativeModel.generate_content = self._original_sync  # type: ignore[assignment]
        if self._original_async is not None:
            GenerativeModel.generate_content_async = self._original_async  # type: ignore[assignment]
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
        before_decisions, ctx, started_at = self._before(
            client_self, args, kwargs
        )
        enforce_before_call(ctx)
        target = self._cache_bound_model(ctx, client_self, kwargs)
        try:
            response = original(target, *args, **kwargs)
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
        before_decisions, ctx, started_at = self._before(
            client_self, args, kwargs
        )
        enforce_before_call(ctx)
        target = self._cache_bound_model(ctx, client_self, kwargs)
        try:
            response = await original(target, *args, **kwargs)
        except Exception as exc:
            ended_at = int(time.time() * 1000)
            self._on_provider_error(
                ctx, exc, before_decisions, started_at, ended_at
            )
            raise
        ended_at = int(time.time() * 1000)
        self._after(ctx, response, before_decisions, started_at, ended_at)
        return response

    # ------------------------------------------------------------------
    # Cache-resource rebinding
    # ------------------------------------------------------------------

    def _cache_bound_model(
        self, ctx: Any, client_self: Any, kwargs: dict
    ) -> Any:
        """The model object to dispatch against. When a policy
        attached a ``CachedContent`` resource for this call, rebind
        to it; on any failure (or when per-call ``tools`` /
        ``system_instruction`` overrides make a cache-bound model
        invalid) fall back to the user's original object."""
        if ctx is None:
            return client_self
        resource = ctx.metadata.get(CACHED_CONTENT_METADATA_KEY)
        if resource is None:
            return client_self
        if "tools" in kwargs or "system_instruction" in kwargs:
            # A cache-bound model carries the cached system/tools;
            # Gemini rejects passing them again. Per-call overrides
            # win — skip the rebind for this call.
            return client_self
        rebound = safely_run(
            self._rebind,
            client_self,
            resource,
            hook_label="gemini.from_cached_content",
        )
        return rebound if rebound is not None else client_self

    @staticmethod
    def _rebind(client_self: Any, resource: Any) -> Any:
        factory = getattr(type(client_self), "from_cached_content", None)
        if factory is None:
            return None
        extra: dict[str, Any] = {}
        generation_config = getattr(client_self, "_generation_config", None)
        if generation_config:
            extra["generation_config"] = generation_config
        safety_settings = getattr(client_self, "_safety_settings", None)
        if safety_settings:
            extra["safety_settings"] = safety_settings
        return factory(cached_content=resource, **extra)

    # ------------------------------------------------------------------
    # Before / after / error
    # ------------------------------------------------------------------

    def _build_request(
        self, client_self: Any, args: tuple, kwargs: dict
    ) -> dict:
        """Synthesise the flat request dict the translator + policies
        consume. Per-call kwargs win over model-bound state."""
        request = dict(kwargs)
        contents = kwargs.get("contents")
        if contents is None and args:
            contents = args[0]
        if contents is not None:
            request["contents"] = contents
        request["model"] = _model_name(client_self) or _DEFAULT_MODEL
        if "system_instruction" not in request:
            system_text = _system_instruction_text(client_self)
            if system_text:
                request["system_instruction"] = system_text
        if "tools" not in request:
            tools = getattr(client_self, "_tools", None)
            if tools is not None:
                request["tools"] = tools
        return request

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

    def _before(self, client_self: Any, args: tuple, kwargs: dict):
        started_at = int(time.time() * 1000)
        request = safely_run(
            self._build_request,
            client_self,
            args,
            kwargs,
            hook_label="gemini.build_request",
        )
        if request is None:
            request = {**kwargs, "model": _DEFAULT_MODEL}
        ctx = safely_run(
            build_call_context,
            provider=_PROVIDER,
            model=request.get("model") or _DEFAULT_MODEL,
            request_kwargs=request,
            storage=self._storage,
            hook_label="gemini.build_call_context",
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
