"""Shared helpers for the Agent-SDK-shaped adapters
(:mod:`inkfoot.adapters.openai_agents`,
:mod:`inkfoot.adapters.anthropic_agent`).

Both SDKs expose an ``Agent`` class with ``run`` / ``run_async``
plus an internal tool-dispatch method whose exact name varies by
build. The wrapping primitives live here so neither sibling adapter
has to import a leading-underscore name from the other: the
helpers are promoted to a shared module the package treats as
its own private surface.

The module is leading-underscore-prefixed so it stays out of the
public adapter surface — :mod:`inkfoot.adapters` deliberately
doesn't re-export anything from here.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
from typing import Any, Callable

_LOG = logging.getLogger("inkfoot.adapters.shared")


# Method names we look for when wrapping tool dispatch. Different
# Agents SDK builds use different internal names — we wrap whichever
# is present.
TOOL_DISPATCH_CANDIDATES: tuple[str, ...] = (
    "_call_tool",
    "_dispatch_tool",
    "dispatch_tool",
    "call_tool",
)


def now_ms() -> int:
    """Wall-clock millisecond timestamp."""
    return int(time.time() * 1000)


def stable_args_hash(args: Any) -> str:
    """Hash tool arguments into a 16-hex-char digest.

    Used as the ``tool_args_hash`` field on ``tool_dispatched``
    events — two identical tool calls produce the same hash so a
    "tool result is being recomputed" smell can fire. Falls back to
    ``repr(args)`` when the args don't JSON-serialise (e.g. they
    hold a callable).
    """
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def extract_tool_call(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[str, Any]:
    """Return ``(tool_name, tool_args)`` from a wrapped tool-dispatch
    call. Tolerant of every signature shape we've seen across SDK
    builds — kwargs first (``tool_name=`` / ``name=``), then
    positional ``(name, args)``, then a tool object exposing
    ``.name`` / ``.args``, then the OpenAI-style ``{"name": ...,
    "arguments": ...}`` dict.

    Falls back to ``("unknown", None)`` rather than raising — a
    badly-shaped call would otherwise break the host SDK because the
    wrapper sits in front of every tool dispatch.
    """
    tool_name = kwargs.get("tool_name") or kwargs.get("name")
    tool_args = kwargs.get("tool_args") or kwargs.get("args")

    if tool_name is None and args:
        first = args[0]
        if isinstance(first, str):
            tool_name = first
        elif isinstance(first, dict):
            tool_name = first.get("name") or first.get("tool_name")
            if tool_args is None:
                tool_args = first.get("arguments") or first.get("args")
        else:
            attr_name = getattr(first, "name", None) or getattr(
                first, "tool_name", None
            )
            if attr_name is not None:
                tool_name = attr_name
            if tool_args is None:
                tool_args = getattr(first, "args", None) or getattr(
                    first, "arguments", None
                )

    if tool_args is None and len(args) >= 2:
        tool_args = args[1]

    return str(tool_name or "unknown"), tool_args


def emit_tool_dispatched(
    *,
    run_id: str,
    tool_name: str,
    args_hash: str,
    dispatch_latency_ms: int,
) -> None:
    """Write one ``tool_dispatched`` event. No-op when storage is
    not yet initialised (e.g. tests driving the helper directly)."""
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
            occurred_at=now_ms(),
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
            "adapters.shared: tool_dispatched emit failed", exc_info=True
        )


def wrap_run_method(
    method: Callable[..., Any], *, task: str
) -> Callable[..., Any]:
    """Wrap an ``Agent.run`` / ``Agent.run_async`` so the body runs
    under an :func:`inkfoot.agent_run` scope. Reentrant: when an
    outer run is already active, no new run is opened — the method
    runs against the caller's scope."""
    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            from inkfoot._run_context import current_run_id  # noqa: PLC0415

            if current_run_id() is not None:
                return await method(*args, **kwargs)
            import inkfoot  # noqa: PLC0415

            async with inkfoot.agent_run(
                task=task, metadata={"agent_kind": task}
            ):
                return await method(*args, **kwargs)

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from inkfoot._run_context import current_run_id  # noqa: PLC0415

        if current_run_id() is not None:
            return method(*args, **kwargs)
        import inkfoot  # noqa: PLC0415

        with inkfoot.agent_run(task=task, metadata={"agent_kind": task}):
            return method(*args, **kwargs)

    return wrapper


def wrap_tool_dispatcher(
    method: Callable[..., Any]
) -> Callable[..., Any]:
    """Wrap a tool-dispatch method so each invocation emits a
    ``tool_dispatched`` event. Tolerant of multiple SDK shapes via
    :func:`extract_tool_call`."""
    if asyncio.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            from inkfoot._run_context import current_run_id  # noqa: PLC0415

            tool_name, tool_args = extract_tool_call(args, kwargs)
            args_hash = stable_args_hash(tool_args)
            started = time.perf_counter()
            try:
                return await method(*args, **kwargs)
            finally:
                latency_ms = int((time.perf_counter() - started) * 1000)
                run_id = current_run_id()
                if run_id is not None:
                    emit_tool_dispatched(
                        run_id=run_id,
                        tool_name=tool_name,
                        args_hash=args_hash,
                        dispatch_latency_ms=latency_ms,
                    )

        return async_wrapper

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from inkfoot._run_context import current_run_id  # noqa: PLC0415

        tool_name, tool_args = extract_tool_call(args, kwargs)
        args_hash = stable_args_hash(tool_args)
        started = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            run_id = current_run_id()
            if run_id is not None:
                emit_tool_dispatched(
                    run_id=run_id,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    dispatch_latency_ms=latency_ms,
                )

    return wrapper


def install_attr(
    target: Any,
    name: str,
    wrapped: Any,
    restorers: list[Callable[[], None]],
) -> None:
    """Stash ``wrapped`` as ``target.name`` and register a restorer
    that reverses the install on shutdown.

    Uses a sentinel to distinguish "the attr was an instance
    attribute" from "the attr lived on the class" — only the former
    is deleted on shutdown, so a class-level method isn't
    accidentally removed.
    """
    sentinel: Any = object()
    original = (
        target.__dict__.get(name, sentinel)
        if hasattr(target, "__dict__")
        else sentinel
    )
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
