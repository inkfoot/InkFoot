"""LangChain embeddings capture.

LangChain has no callback for embedding calls (the callback manager
only dispatches chat/LLM/tool/retriever events), so unlike chat
models, embeddings can't be captured through the callback handler.
Instead — when the user opts in with ``instrument(embeddings=True)``
— this shim patches the embedding methods on every concrete
``langchain_core.embeddings.Embeddings`` subclass, the same way the
raw-SDK shims patch a provider's client. That covers the providers
with no raw-SDK embeddings shim of their own: Gemini, Bedrock, Voyage,
Cohere, and friends.

Each wrapped ``embed_documents`` / ``embed_query`` (and their async
variants) records one ``embedding_call`` event, accounted separately
from the causal token ledger. When the OpenAI raw shim also observes
the same call (``OpenAIEmbeddings`` drives the OpenAI SDK underneath),
the raw layer wins — it has the provider's exact reported usage — and
this shim's wrapper suppresses its own emit via the cross-layer dedup
signal.

**Coverage note:** the shim wraps subclasses that exist when
``instrument()`` runs, plus any created afterwards (a lightweight
subclass hook). Import your embeddings classes before — or after —
instrumenting; either order is captured.
"""

from __future__ import annotations

import functools
import logging
from contextvars import ContextVar
from typing import Any, Callable, Optional

from inkfoot.normalise.langchain import map_provider
from inkfoot.shims._emit import (
    emit_embedding_call,
    raw_embedding_captured_get,
    raw_embedding_captured_reset,
    raw_embedding_captured_restore,
)
from inkfoot.shims._isolation import safely_run
from inkfoot.tokenisers import tokenise

_LOG = logging.getLogger("inkfoot.langchain.embeddings")

_PROVIDER_LABEL = "langchain"

# Embedding methods we wrap on each concrete subclass. ``embed_query``
# embeds a single string; ``embed_documents`` a batch. The async
# variants are wrapped when a subclass defines its own (a subclass that
# inherits the base async default routes through the wrapped sync
# method instead, so wrapping the inherited default would double-count).
_SYNC_METHODS = ("embed_documents", "embed_query")
_ASYNC_METHODS = ("aembed_documents", "aembed_query")

# Substrings of the LangChain embeddings class name → provider key
# (normalised through ``map_provider``). First match wins.
_EMBEDDING_CLASS_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("azureopenai", "azure"),
    ("openai", "openai"),
    ("bedrock", "bedrock"),
    ("vertex", "google_vertexai"),
    ("google", "google_genai"),
    ("voyage", "voyage"),
    ("cohere", "cohere"),
    ("ollama", "ollama"),
)

# Re-entrancy guard: a subclass whose ``embed_documents`` internally
# calls ``self.embed_query`` (or whose native async calls sync) must
# still produce exactly one event — only the outermost wrapped call
# emits. ContextVar (not threadlocal) so the depth is correct across
# async embed paths.
_embed_depth: ContextVar[int] = ContextVar(
    "inkfoot_lc_embed_depth", default=0
)


class LangChainEmbeddingsShim:
    """Patches embedding methods on concrete ``Embeddings`` subclasses.

    Opt-in: constructed + installed only when
    ``instrument(embeddings=True)`` and ``langchain_core`` is
    importable."""

    provider = _PROVIDER_LABEL

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._installed = False
        # (class, method_name, original_callable) for clean uninstall.
        self._patched: list[tuple[type, str, Callable[..., Any]]] = []
        self._orig_init_subclass: Optional[Callable[..., Any]] = None
        self._base: Optional[type] = None

    def install(self) -> bool:
        if self._installed:
            return True
        try:
            from langchain_core.embeddings import (  # type: ignore[import-not-found]
                Embeddings,
            )
        except ImportError:
            _LOG.debug(
                "langchain-core not importable; LangChainEmbeddingsShim skipped"
            )
            return False

        self._base = Embeddings
        for cls in _all_subclasses(Embeddings):
            self._patch_class(cls)
        self._install_subclass_hook(Embeddings)
        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        for cls, name, original in reversed(self._patched):
            try:
                setattr(cls, name, original)
            except (AttributeError, TypeError):  # pragma: no cover — defensive
                pass
        self._patched.clear()
        self._remove_subclass_hook()
        self._installed = False

    # ------------------------------------------------------------------
    # Patching
    # ------------------------------------------------------------------

    def _patch_class(self, cls: type) -> None:
        """Wrap the embedding methods a subclass defines itself.

        Only methods in ``cls.__dict__`` are wrapped — inherited
        methods are wrapped on the class that actually defines them, so
        we never double-wrap and never touch the abstract base."""
        for name in _SYNC_METHODS:
            self._patch_method(cls, name, is_async=False)
        for name in _ASYNC_METHODS:
            self._patch_method(cls, name, is_async=True)

    def _patch_method(self, cls: type, name: str, *, is_async: bool) -> None:
        original = cls.__dict__.get(name)
        if original is None or not callable(original):
            return
        if getattr(original, "__isabstractmethod__", False):
            return
        if getattr(original, "__inkfoot_embedding_shim__", False):
            return
        wrapped = (
            self._build_async_wrapper(original, name)
            if is_async
            else self._build_sync_wrapper(original, name)
        )
        try:
            setattr(cls, name, wrapped)
        except (AttributeError, TypeError):  # pragma: no cover — defensive
            return
        self._patched.append((cls, name, original))

    def _build_sync_wrapper(
        self, original: Callable[..., Any], name: str
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        def wrapper(self_obj: Any, payload: Any, *args: Any, **kwargs: Any) -> Any:
            depth = _embed_depth.get()
            dtok = _embed_depth.set(depth + 1)
            rtok = raw_embedding_captured_reset(False) if depth == 0 else None
            try:
                result = original(self_obj, payload, *args, **kwargs)
                raw_seen = raw_embedding_captured_get() if depth == 0 else True
            finally:
                _embed_depth.reset(dtok)
                if rtok is not None:
                    raw_embedding_captured_restore(rtok)
            if depth == 0 and not raw_seen:
                shim._record(self_obj, payload, name)
            return result

        wrapper.__inkfoot_embedding_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _build_async_wrapper(
        self, original: Callable[..., Any], name: str
    ) -> Callable[..., Any]:
        shim = self

        @functools.wraps(original)
        async def wrapper(
            self_obj: Any, payload: Any, *args: Any, **kwargs: Any
        ) -> Any:
            depth = _embed_depth.get()
            dtok = _embed_depth.set(depth + 1)
            rtok = raw_embedding_captured_reset(False) if depth == 0 else None
            try:
                result = await original(self_obj, payload, *args, **kwargs)
                raw_seen = raw_embedding_captured_get() if depth == 0 else True
            finally:
                _embed_depth.reset(dtok)
                if rtok is not None:
                    raw_embedding_captured_restore(rtok)
            if depth == 0 and not raw_seen:
                shim._record(self_obj, payload, name)
            return result

        wrapper.__inkfoot_embedding_shim__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    def _record(self, instance: Any, payload: Any, method_name: str) -> None:
        """Resolve provider/model + token count and emit one event.
        Isolation-wrapped so a recording bug never reaches the user's
        embeddings call."""
        safely_run(
            self._emit,
            instance,
            payload,
            method_name,
            hook_label="LangChainEmbeddingsShim._emit",
        )

    def _emit(self, instance: Any, payload: Any, method_name: str) -> None:
        model = _resolve_model(instance)
        provider = _resolve_provider(instance, model)
        texts = _payload_texts(payload, method_name)
        input_tokens = sum(
            tokenise(t, model).value for t in texts if isinstance(t, str)
        )
        emit_embedding_call(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            batch_size=len(texts),
            storage=self._storage,
            # The LangChain layer never sees provider-reported usage —
            # the tokeniser estimate is the best we have here.
            token_count_estimated=True,
        )

    # ------------------------------------------------------------------
    # Future-subclass hook
    # ------------------------------------------------------------------

    def _install_subclass_hook(self, base: type) -> None:
        """Wrap embedding methods on subclasses created *after* install.

        Guarded so it can never break subclass creation (some
        embeddings classes are pydantic models with their own
        ``__init_subclass__`` machinery — we chain to whatever was
        there and swallow any error from our own patch step)."""
        shim = self
        previous = base.__dict__.get("__init_subclass__")
        self._orig_init_subclass = previous

        def _hook(cls: type, **kwargs: Any) -> None:
            # Chain to the original __init_subclass__ first so model
            # machinery runs untouched.
            try:
                if previous is not None:
                    previous.__func__(cls, **kwargs)  # type: ignore[attr-defined]
                else:
                    super(base, cls).__init_subclass__(**kwargs)
            except Exception:  # pragma: no cover — defensive
                pass
            try:
                shim._patch_class(cls)
            except Exception:  # pragma: no cover — defensive
                _LOG.debug("subclass patch failed for %r", cls, exc_info=True)

        try:
            base.__init_subclass__ = classmethod(_hook)  # type: ignore[assignment]
        except (AttributeError, TypeError):  # pragma: no cover — defensive
            self._orig_init_subclass = None

    def _remove_subclass_hook(self) -> None:
        if self._base is None:
            return
        try:
            if self._orig_init_subclass is not None:
                self._base.__init_subclass__ = self._orig_init_subclass  # type: ignore[assignment]
            else:
                # Restore the default by dropping our override.
                if "__init_subclass__" in self._base.__dict__:
                    delattr(self._base, "__init_subclass__")
        except (AttributeError, TypeError):  # pragma: no cover — defensive
            pass
        self._orig_init_subclass = None


# ----------------------------------------------------------------------
# Helpers (module-level so they're unit-testable)
# ----------------------------------------------------------------------


def _all_subclasses(base: type) -> list[type]:
    """Every concrete subclass of ``base``, recursively, de-duplicated
    by identity."""
    seen: set[int] = set()
    out: list[type] = []

    def _rec(cls: type) -> None:
        for sub in cls.__subclasses__():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            out.append(sub)
            _rec(sub)

    _rec(base)
    return out


def _resolve_model(instance: Any) -> str:
    """Embeddings classes name the model attribute inconsistently —
    ``model`` (OpenAI, Voyage, Cohere), ``model_id`` (Bedrock), or
    ``model_name``. Probe each.

    The ``models/`` prefix Google's SDK reports (e.g.
    ``models/text-embedding-004``) is stripped to the bare name the
    pricing table is keyed on — mirroring the Gemini chat shim — so
    Gemini embedding costs resolve instead of falling through to
    "unpriced"."""
    for attr in ("model", "model_id", "model_name"):
        value = getattr(instance, attr, None)
        if isinstance(value, str) and value:
            return value.removeprefix("models/")
    return ""


def _resolve_provider(instance: Any, model: str) -> str:
    class_name = type(instance).__name__.lower()
    raw_provider: Optional[str] = None
    for needle, key in _EMBEDDING_CLASS_PROVIDERS:
        if needle in class_name:
            raw_provider = key
            break
    if raw_provider is None and model.lower().startswith("text-embedding"):
        raw_provider = "openai"
    return map_provider(raw_provider, model)


def _payload_texts(payload: Any, method_name: str) -> list[str]:
    """Normalise the call payload to a list of texts. ``embed_query``
    takes a single string; ``embed_documents`` a list."""
    if "query" in method_name:
        return [payload] if isinstance(payload, str) else []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, (list, tuple)):
        return [t for t in payload if isinstance(t, str)]
    return []
