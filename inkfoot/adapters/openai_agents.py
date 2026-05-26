"""OpenAI Agents SDK adapter — Pattern-C wrap for ``Agent.run`` plus
the tool-dispatch layer.

Per phase-1-explain §4.1.2 + E1-S3 task list:

* Wrap ``Agent.run`` and ``Agent.run_async`` so the loop is scoped
  under an :func:`inkfoot.agent_run`.
* Emit a ``tool_dispatched`` event per tool invocation with
  ``tool_name``, ``tool_args_hash``, and ``dispatch_latency_ms``.
* Declare capability so Phase-2 modification policies that target
  ``Agent.run`` register cleanly.

Duck-typed against the SDK — no module-load-time import. The
adapter accepts either the ``Agent`` class (wraps all instances)
or a single instance.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from inkfoot.adapters._registry import AdapterRegistry

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy

_LOG = logging.getLogger("inkfoot.adapters.openai_agents")

_INSTRUMENTED_MARKER = "_inkfoot_openai_agents_instrumentation"

# Method names we look for when wrapping tool dispatch. Different
# Agents SDK builds use different internal names — we wrap whichever
# is present.
_TOOL_DISPATCH_CANDIDATES: tuple[str, ...] = (
    "_call_tool",
    "_dispatch_tool",
    "dispatch_tool",
    "call_tool",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stable_args_hash(args: Any) -> str:
    """Hash the tool arguments into a short hex digest. Used for
    cache-detection telemetry — two identical tool calls produce the
    same hash so a "tool result is being recomputed" smell can fire.

    Falls back to ``repr(args)`` when the args don't JSON-serialise
    (e.g. they hold a callable). The intent is *stability across
    runs that meant the same thing*, not cryptographic guarantee.
    """
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _emit_tool_dispatched(
    *,
    run_id: str,
    tool_name: str,
    args_hash: str,
    dispatch_latency_ms: int,
) -> None:
    """Write one ``tool_dispatched`` event."""
    from ulid import ULID

    from inkfoot._instrument import _STORAGE  # noqa: PLC0415
    from inkfoot.shims._emit import _next_sequence  # noqa: PLC0415

    storage = _STORAGE
    if storage is None:
        return
    try:
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="tool_dispatched",
            occurred_at=_now_ms(),
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(
                {
                    "tool_name": tool_name,
                    "tool_args_hash": args_hash,
                    "dispatch_latency_ms": dispatch_latency_ms,
                },
                default=str,
            ),
            capture_mode="metadata",
        )
    except Exception:  # pragma: no cover — defensive
        _LOG.warning(
            "adapters.openai_agents: tool_dispatched emit failed",
            exc_info=True,
        )


def _wrap_run_method(
    method: Callable[..., Any], *, task: Optional[str]
) -> Callable[..., Any]:
    """Wrap an ``Agent.run`` / ``Agent.run_async`` bound method (or
    function) so the body runs under an :func:`inkfoot.agent_run`
    scope.
    """
    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            from inkfoot._run_context import current_run_id  # noqa: PLC0415

            if current_run_id() is not None:
                return await method(*args, **kwargs)
            import inkfoot  # noqa: PLC0415

            async with inkfoot.agent_run(
                task=task, metadata={"agent_kind": "openai_agents"}
            ):
                return await method(*args, **kwargs)

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from inkfoot._run_context import current_run_id  # noqa: PLC0415

        if current_run_id() is not None:
            return method(*args, **kwargs)
        import inkfoot  # noqa: PLC0415

        with inkfoot.agent_run(
            task=task, metadata={"agent_kind": "openai_agents"}
        ):
            return method(*args, **kwargs)

    return wrapper


def _wrap_tool_dispatcher(
    method: Callable[..., Any]
) -> Callable[..., Any]:
    """Wrap the agent's tool-dispatch method so each invocation
    emits a ``tool_dispatched`` event.

    The dispatcher's signature varies across SDK builds; we accept
    whichever shape the user has and pull ``tool_name`` /
    ``tool_args`` out of the call args defensively.
    """

    def _extract(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, Any]:
        """Return ``(tool_name, tool_args)`` from a call. Tolerant
        of every signature shape we've seen."""
        tool_name = (
            kwargs.get("tool_name")
            or kwargs.get("name")
            or (
                getattr(args[0], "name", None)
                if args and not isinstance(args[0], (dict, str, int, float))
                else None
            )
        )
        if tool_name is None and args:
            first = args[0]
            if isinstance(first, str):
                tool_name = first
            elif isinstance(first, dict):
                tool_name = first.get("name")
        tool_args = kwargs.get("tool_args") or kwargs.get("args")
        if tool_args is None and len(args) >= 2:
            tool_args = args[1]
        return str(tool_name or "unknown"), tool_args

    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            from inkfoot._run_context import current_run_id  # noqa: PLC0415

            tool_name, tool_args = _extract(args, kwargs)
            args_hash = _stable_args_hash(tool_args)
            started = time.perf_counter()
            try:
                return await method(*args, **kwargs)
            finally:
                latency_ms = int((time.perf_counter() - started) * 1000)
                run_id = current_run_id()
                if run_id is not None:
                    _emit_tool_dispatched(
                        run_id=run_id,
                        tool_name=tool_name,
                        args_hash=args_hash,
                        dispatch_latency_ms=latency_ms,
                    )

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from inkfoot._run_context import current_run_id  # noqa: PLC0415

        tool_name, tool_args = _extract(args, kwargs)
        args_hash = _stable_args_hash(tool_args)
        started = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            run_id = current_run_id()
            if run_id is not None:
                _emit_tool_dispatched(
                    run_id=run_id,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    dispatch_latency_ms=latency_ms,
                )

    return wrapper


class _OpenAIAgentsInstrumentation:
    """Teardown handle returned by :meth:`OpenAIAgentsAdapter.instrument`."""

    def __init__(
        self,
        target: Any,
        restorers: list[Callable[[], None]],
    ) -> None:
        self._target = target
        self._restorers = restorers
        self._shutdown = False

    def shutdown(self) -> None:
        if self._shutdown:
            return
        for restorer in reversed(self._restorers):
            try:
                restorer()
            except Exception:  # pragma: no cover
                _LOG.warning("restore step raised", exc_info=True)
        try:
            delattr(self._target, _INSTRUMENTED_MARKER)
        except AttributeError:  # pragma: no cover
            pass
        self._shutdown = True


class OpenAIAgentsAdapter:
    """Pattern-C adapter for the OpenAI Agents SDK."""

    name = "openai_agents"

    def detect(self) -> bool:
        try:
            import agents  # noqa: F401, PLC0415  — pip name: openai-agents
        except ImportError:
            try:
                import openai_agents  # noqa: F401, PLC0415  — legacy
            except ImportError:
                return False
        return True

    def instrument(
        self,
        target: Any,
        *,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> _OpenAIAgentsInstrumentation:
        """Wrap ``target`` (an ``Agent`` class or instance) so its
        ``run`` / ``run_async`` calls scope an :func:`inkfoot.agent_run`
        and its tool dispatch emits ``tool_dispatched`` events.
        """
        existing = getattr(target, _INSTRUMENTED_MARKER, None)
        if isinstance(existing, _OpenAIAgentsInstrumentation):
            return existing

        restorers: list[Callable[[], None]] = []

        for method_name in ("run", "run_async"):
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = _wrap_run_method(original, task=task or "openai_agents")
            _install_attr(target, method_name, wrapped, restorers)

        for method_name in _TOOL_DISPATCH_CANDIDATES:
            original = getattr(target, method_name, None)
            if original is None or not callable(original):
                continue
            wrapped = _wrap_tool_dispatcher(original)
            _install_attr(target, method_name, wrapped, restorers)

        instrumentation = _OpenAIAgentsInstrumentation(target, restorers)
        try:
            setattr(target, _INSTRUMENTED_MARKER, instrumentation)
        except (AttributeError, TypeError):
            pass

        try:
            AdapterRegistry.set_active(self)
        except Exception:  # pragma: no cover
            _LOG.warning("activate failed", exc_info=True)

        return instrumentation

    def supported_policies(self) -> set[type["Policy"]]:
        """Phase 1: observation-only — Phase 2 modification policies
        (``LazyToolExposure``, ``CheapSummariser``) will enumerate
        here when they land. Empty set for now means the pattern-
        fallback path in :func:`register_policies` accepts the three
        Phase-0 observation policies cleanly."""
        return set()

    def shutdown(self) -> None:
        AdapterRegistry.clear_active()


_default_adapter = OpenAIAgentsAdapter()


def instrument(
    target: Any, *, task: Optional[str] = None, **kwargs: Any
) -> _OpenAIAgentsInstrumentation:
    """User-facing convenience — equivalent to
    ``OpenAIAgentsAdapter().instrument(target, task=task)``."""
    return _default_adapter.instrument(target, task=task, **kwargs)


# Helper shared with the LangGraph adapter shape — small enough we
# keep a local copy here rather than introduce a shared module.


def _install_attr(
    target: Any,
    name: str,
    wrapped: Any,
    restorers: list[Callable[[], None]],
) -> None:
    sentinel: Any = object()
    original = target.__dict__.get(name, sentinel) if hasattr(
        target, "__dict__"
    ) else sentinel
    try:
        setattr(target, name, wrapped)
    except (AttributeError, TypeError):  # pragma: no cover
        return

    def _restore() -> None:
        if original is sentinel:
            try:
                delattr(target, name)
            except AttributeError:  # pragma: no cover
                pass
        else:
            try:
                setattr(target, name, original)
            except (AttributeError, TypeError):  # pragma: no cover
                pass

    restorers.append(_restore)
