"""LangChain integration.

Importing this package requires ``langchain-core`` (``pip install
inkfoot[langchain]``). Two ways in:

* ``inkfoot.instrument()`` auto-detects ``langchain_core`` and calls
  :func:`instrument` here, so LangChain chat models are captured
  process-wide with zero extra code.
* Pass :class:`InkfootCallbackHandler` explicitly via LangChain's
  ``callbacks=[...]`` for per-chain opt-in instead.

Global registration uses LangChain's configure-hook mechanism (the
same one LangSmith tracing uses): every callback manager LangChain
builds — for any chain, agent, or bare chat model — picks the handler
up automatically. LangChain offers no unregister API, so
:func:`uninstrument` deactivates the handler in place; a later
re-instrument reactivates the same instance.
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from typing import Optional

from inkfoot.langchain.handler import InkfootCallbackHandler

__all__ = [
    "InkfootCallbackHandler",
    "get_handler",
    "instrument",
    "is_instrumented",
    "uninstrument",
]

_LOG = logging.getLogger("inkfoot.langchain")

_LOCK = threading.Lock()
_HANDLER: Optional[InkfootCallbackHandler] = None
_REGISTERED = False
# Keep a module reference so the registered ContextVar can't be
# garbage-collected out of langchain's hook list.
_HANDLER_VAR: Optional[ContextVar] = None


def instrument() -> InkfootCallbackHandler:
    """Register the Inkfoot callback handler with LangChain's global
    configure hooks. Idempotent — repeat calls reactivate and return
    the same handler instance."""
    global _HANDLER, _REGISTERED, _HANDLER_VAR
    with _LOCK:
        if _HANDLER is None:
            _HANDLER = InkfootCallbackHandler()
        _HANDLER.activate()
        if not _REGISTERED:
            from langchain_core.tracers.context import register_configure_hook

            # The ContextVar's *default* is what makes this global:
            # langchain reads the var in whatever context a chain
            # runs, and an unset var yields the default — the handler
            # — in every thread and task.
            _HANDLER_VAR = ContextVar(
                "inkfoot_callback_handler", default=_HANDLER
            )
            register_configure_hook(_HANDLER_VAR, inheritable=True)
            _REGISTERED = True
            _LOG.info(
                "Inkfoot LangChain callback handler registered for all "
                "callback managers"
            )
        else:
            _LOG.debug(
                "Inkfoot LangChain callback handler already registered; "
                "reactivated"
            )
        return _HANDLER


def uninstrument() -> None:
    """Deactivate the handler. It stays in LangChain's hook list
    (there is no removal API) but every callback becomes a no-op."""
    with _LOCK:
        if _HANDLER is not None:
            _HANDLER.deactivate()


def is_instrumented() -> bool:
    """True when the handler is registered *and* active."""
    with _LOCK:
        return (
            _REGISTERED and _HANDLER is not None and _HANDLER.is_active
        )


def get_handler() -> Optional[InkfootCallbackHandler]:
    """The process-wide handler instance, or ``None`` before the
    first :func:`instrument` call. Useful for passing the same
    instance explicitly via ``callbacks=[...]`` — LangChain
    deduplicates identical handler instances, so doing both is
    harmless."""
    return _HANDLER
