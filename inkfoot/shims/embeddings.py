"""``OpenAIEmbeddingsShim`` — opt-in capture of OpenAI embedding
calls as ``embedding_call`` events.

Wraps ``openai.resources.embeddings.Embeddings.create`` and
``AsyncEmbeddings.create``. Unlike the chat shims this one is **off by
default** — it installs only when the user passes
``instrument(embeddings=True)`` — because embeddings are accounted
separately from the causal token ledger and most callers don't want
the extra event stream.

The captured token count prefers the provider's own
``response.usage`` (exact); when the provider doesn't report usage we
fall back to the local tokeniser and flag the count as estimated. No
streaming, no policy hooks, no replay content: an embedding call is a
single request/response with no incremental output.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional

from inkfoot.shims._emit import emit_embedding_call
from inkfoot.shims._isolation import safely_run
from inkfoot.tokenisers import tokenise

_LOG = logging.getLogger("inkfoot.shims.embeddings")

_PROVIDER = "openai"


def _now_ms() -> int:
    return int(time.time() * 1000)


class OpenAIEmbeddingsShim:
    """Per-process OpenAI embeddings shim. Install via :meth:`install`,
    restore via :meth:`uninstall`. Opt-in: only constructed +
    installed when ``instrument(embeddings=True)``."""

    provider = _PROVIDER

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._installed = False
        self._original_sync: Optional[Callable[..., Any]] = None
        self._original_async: Optional[Callable[..., Any]] = None

    def install(self) -> bool:
        if self._installed:
            return True
        try:
            from openai.resources.embeddings import (  # type: ignore[import-not-found]
                AsyncEmbeddings,
                Embeddings,
            )
        except ImportError:
            _LOG.debug("openai SDK not importable; OpenAIEmbeddingsShim skipped")
            return False

        sync_target: Callable[..., Any] = Embeddings.create  # type: ignore[assignment]
        async_target: Callable[..., Any] = AsyncEmbeddings.create  # type: ignore[assignment]
        if getattr(sync_target, "__inkfoot_shim__", False):
            self._installed = True
            return True

        self._original_sync = sync_target
        self._original_async = async_target

        Embeddings.create = self._build_sync_wrapper(sync_target)  # type: ignore[assignment]
        if inspect.iscoroutinefunction(async_target):
            AsyncEmbeddings.create = self._build_async_wrapper(  # type: ignore[assignment]
                async_target
            )
        else:
            AsyncEmbeddings.create = self._build_sync_wrapper(  # type: ignore[assignment]
                async_target
            )

        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from openai.resources.embeddings import (  # type: ignore[import-not-found]
                AsyncEmbeddings,
                Embeddings,
            )
        except ImportError:  # pragma: no cover — defensive
            self._installed = False
            return
        if self._original_sync is not None:
            Embeddings.create = self._original_sync  # type: ignore[assignment]
        if self._original_async is not None:
            AsyncEmbeddings.create = self._original_async  # type: ignore[assignment]
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
            response = original(client_self, *args, **kwargs)
            shim._record(kwargs, response)
            return response

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _build_async_wrapper(
        self, original: Callable[..., Any]
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        async def wrapper(client_self: Any, *args: Any, **kwargs: Any) -> Any:
            response = await original(client_self, *args, **kwargs)
            shim._record(kwargs, response)
            return response

        wrapper.__inkfoot_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, kwargs: dict, response: Any) -> None:
        """Resolve token count + batch size and emit one
        ``embedding_call`` event. Fully isolation-wrapped so a bug
        here can never propagate into the user's embeddings call."""
        ended_at = _now_ms()
        safely_run(
            self._emit,
            kwargs,
            response,
            ended_at,
            hook_label="OpenAIEmbeddingsShim._emit",
        )

    def _emit(self, kwargs: dict, response: Any, ended_at: int) -> None:
        model = kwargs.get("model") or ""
        input_value = kwargs.get("input")

        reported = _reported_input_tokens(response)
        if reported is not None:
            input_tokens = reported
            token_count_estimated = False
            batch_size = _batch_size(input_value)
        else:
            input_tokens, batch_size, token_count_estimated = _count_input(
                input_value, model
            )

        emit_embedding_call(
            provider=_PROVIDER,
            model=model,
            input_tokens=input_tokens,
            batch_size=batch_size,
            storage=self._storage,
            occurred_at=ended_at,
            token_count_estimated=token_count_estimated,
            # Signal so a LangChain ``OpenAIEmbeddings`` wrapper higher
            # on the stack doesn't double-count this same call.
            signal_raw=True,
        )


# ----------------------------------------------------------------------
# Input + usage helpers (module-level so they're unit-testable
# without instantiating the shim)
# ----------------------------------------------------------------------


def _reported_input_tokens(response: Any) -> Optional[int]:
    """Provider-reported input-token count off an embeddings response.

    OpenAI returns ``usage.prompt_tokens`` (== ``total_tokens`` for
    embeddings). Handles both the SDK object and a plain dict. Returns
    ``None`` when no usage is present so the caller falls back to the
    local tokeniser."""
    if response is None:
        return None
    usage = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return None
    for key in ("prompt_tokens", "total_tokens"):
        value = (
            usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        )
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _batch_size(input_value: Any) -> int:
    """Number of inputs in one embeddings call.

    A bare string or a single pre-tokenised int sequence is one input;
    a list of strings (or a list of int sequences) is one per element.
    """
    if input_value is None:
        return 0
    if isinstance(input_value, str):
        return 1
    if isinstance(input_value, (list, tuple)):
        if not input_value:
            return 0
        # A flat list of ints is a single pre-tokenised input.
        if all(isinstance(item, int) and not isinstance(item, bool) for item in input_value):
            return 1
        return len(input_value)
    return 1


def _count_input(input_value: Any, model: str) -> tuple[int, int, bool]:
    """Tokeniser fallback when the provider didn't report usage.

    Returns ``(input_tokens, batch_size, estimated)``. ``estimated``
    is always ``True`` on this path — the count is the local
    tokeniser's, not the provider's billed number."""
    if input_value is None:
        return 0, 0, True
    if isinstance(input_value, str):
        return tokenise(input_value, model).value, 1, True
    if isinstance(input_value, (list, tuple)):
        if not input_value:
            return 0, 0, True
        # Flat list of ints → already tokenised, a single input.
        if all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in input_value
        ):
            return len(input_value), 1, True
        total = 0
        batch = 0
        for item in input_value:
            batch += 1
            if isinstance(item, str):
                total += tokenise(item, model).value
            elif isinstance(item, (list, tuple)):
                # Pre-tokenised sequence — token count is its length.
                total += len(item)
        return total, batch, True
    return 0, 1, True
