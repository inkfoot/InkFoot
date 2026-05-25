"""Hook isolation invariant (ADR-0-3).

Inkfoot is a third-party library wrapping a critical path (LLM
calls). Any uncaught exception in our code would crash the user's
agent — unacceptable. Every callback, hook, and bookkeeping step we
run inside a shim goes through :func:`safely_run` or wears the
:func:`isolated_hook` decorator: an exception logs at ``WARNING``
to ``inkfoot.errors`` and the function returns ``None`` (or the
``fallback`` value) instead of propagating.

The user's LLM call always completes with the unmodified provider
response.

There's deliberately no "strict mode" yet — ADR-0-3 calls out a
future ``inkfoot --strict`` env flag for dev that would re-raise.
Phase 0 is trust-establishment, so the default catches everything.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")
_LOG = logging.getLogger("inkfoot.errors")


def safely_run(
    fn: Callable[..., _T],
    *args: Any,
    hook_label: str = "",
    fallback: _T | None = None,
    **kwargs: Any,
) -> _T | None:
    """Run ``fn(*args, **kwargs)`` with hook-isolation guarantees.

    Returns the function's return value on success, or ``fallback``
    when the call raises. The exception is logged at ``WARNING``
    with ``hook_label`` for diagnosability — never re-raised.

    ``BaseException`` subclasses (``KeyboardInterrupt``,
    ``SystemExit``, ``GeneratorExit``) are intentionally **not**
    caught — those signal "the program is shutting down" and
    swallowing them would break Ctrl-C / cleanup. We only absorb
    ``Exception``.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:  # pylint: disable=broad-except
        label = hook_label or getattr(fn, "__qualname__", repr(fn))
        _LOG.warning("inkfoot hook %s raised; isolated", label, exc_info=True)
        return fallback


def isolated_hook(
    fallback: Any = None, *, label: str | None = None
) -> Callable[[Callable[..., _T]], Callable[..., _T | Any]]:
    """Decorator form of :func:`safely_run`. Wraps a callable so
    *any* exception is logged + swallowed.

    Use this on small "do one thing" helpers the shim calls. For
    larger flows (the shim's pre-call / post-call blocks), prefer
    explicit ``try / except`` around the section so the trace tells
    you where the failure was.
    """

    def decorator(fn: Callable[..., _T]) -> Callable[..., _T | Any]:
        hook_label = label or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> _T | Any:
            try:
                return fn(*args, **kwargs)
            except Exception:  # pylint: disable=broad-except
                _LOG.warning(
                    "inkfoot hook %s raised; isolated",
                    hook_label,
                    exc_info=True,
                )
                return fallback

        return wrapper

    return decorator
