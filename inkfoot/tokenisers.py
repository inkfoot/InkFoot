"""Tokeniser dispatch — exact counts for OpenAI; best-effort with
estimation flag for Anthropic.

ADR-0-8 commits to ``tiktoken`` as the exact tokeniser for OpenAI
models and a flagged fallback for Anthropic (``anthropic.tokenize``
when importable, otherwise ``tiktoken`` with the ``o200k_base``
encoding). The estimation flag propagates into
``NeutralCall.estimation_flags`` so ``inkfoot report`` can surface
"these numbers are approximate" honestly.

The model prefix dispatch is conservative: we recognise the obvious
prefixes (``gpt-*``, ``o1*``, ``claude-*``). Unknown providers fall
through to the Anthropic-fallback path with the flag set — better an
estimate than nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional


_LOG = logging.getLogger("inkfoot.tokenisers")


# Encoding tiktoken uses for the best-effort Anthropic fallback. The
# ``o200k_base`` encoding is GPT-4o-era and produces the closest
# token counts to Anthropic's actual tokeniser in practice; expect
# 2-5% drift on typical English prose.
_ANTHROPIC_FALLBACK_ENCODING = "o200k_base"


# Encoding tiktoken associates with a model when ``encoding_for_model``
# doesn't have a registered mapping yet (typical for brand-new OpenAI
# models). We fall back to ``o200k_base`` rather than failing.
_DEFAULT_OPENAI_ENCODING = "o200k_base"


@dataclass(frozen=True, slots=True)
class TokenCount:
    """One tokeniser result. ``estimated=True`` means the count came
    from the fallback path; the caller should lift the flag into
    ``NeutralCall.estimation_flags``.
    """

    value: int
    estimated: bool


def _model_provider(model: str) -> str:
    """Heuristic provider routing from model name. Returns
    ``"anthropic"``, ``"openai"``, or ``"unknown"``."""
    lowered = (model or "").lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "text-", "chatgpt-")):
        return "openai"
    return "unknown"


def _tiktoken_encoding(model: str):
    """Return a tiktoken encoding for ``model``, falling back to
    ``o200k_base`` if the model isn't registered with
    ``encoding_for_model`` yet (common for fresh OpenAI releases)."""
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        _LOG.debug(
            "tiktoken has no mapping for %r; falling back to %s",
            model,
            _DEFAULT_OPENAI_ENCODING,
        )
        return tiktoken.get_encoding(_DEFAULT_OPENAI_ENCODING)


def _anthropic_tokenise(text: str) -> Optional[int]:
    """Try the official Anthropic tokeniser. Returns the count on
    success, ``None`` if the SDK isn't importable or doesn't expose a
    tokeniser API. The SDK's tokeniser is *not* a guaranteed
    interface — we probe what's actually there and degrade
    gracefully."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None

    # The shape of anthropic's tokeniser has shifted across SDK
    # versions. Probe a couple of likely entry points.
    fn = getattr(anthropic, "tokenize", None)
    if callable(fn):
        try:
            result = fn(text)
        except Exception:  # pragma: no cover — defensive
            return None
        if isinstance(result, int):
            return result
        if hasattr(result, "tokens"):
            try:
                return len(result.tokens)
            except TypeError:  # pragma: no cover
                return None
    return None


def tokenise(text: str, model: str) -> TokenCount:
    """Count tokens in ``text`` for the given ``model``.

    Dispatch:

    * OpenAI-family models — ``tiktoken.encoding_for_model(model)``.
      Exact count, ``estimated=False``.
    * Anthropic-family models — ``anthropic.tokenize`` when
      importable; otherwise ``tiktoken`` with the ``o200k_base``
      encoding. The fallback case sets ``estimated=True``.
    * Unknown provider — fall through to the Anthropic-fallback
      tokeniser with ``estimated=True``.

    Empty string returns ``TokenCount(0, False)`` regardless of
    provider. ``None`` raises ``TypeError`` at the boundary.
    """
    if text is None:
        raise TypeError("tokenise: text must be str, got None")
    if not isinstance(text, str):
        raise TypeError(
            f"tokenise: text must be str, got {type(text).__name__}"
        )
    if text == "":
        return TokenCount(0, False)

    provider = _model_provider(model)

    if provider == "openai":
        encoding = _tiktoken_encoding(model)
        return TokenCount(len(encoding.encode(text)), False)

    if provider == "anthropic":
        exact = _anthropic_tokenise(text)
        if exact is not None:
            return TokenCount(exact, False)
        # Fallback — flagged.
        import tiktoken

        enc = tiktoken.get_encoding(_ANTHROPIC_FALLBACK_ENCODING)
        return TokenCount(len(enc.encode(text)), True)

    # Unknown provider — use the same fallback path as Anthropic but
    # always flag.
    import tiktoken

    enc = tiktoken.get_encoding(_ANTHROPIC_FALLBACK_ENCODING)
    return TokenCount(len(enc.encode(text)), True)


def tokenise_with_flags(text: str, model: str) -> TokenCount:
    """Alias for :func:`tokenise` kept for spec-doc alignment
    (E2-S3 T2). Returns the full :class:`TokenCount` so callers can
    propagate the estimation flag without re-checking."""
    return tokenise(text, model)


def tokenise_tools(
    tools: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    model: str,
) -> TokenCount:
    """Estimate the token count of a serialised tool-schema array.

    Different providers ship slightly different tool-schema shapes
    over the wire (Anthropic's ``tools`` vs OpenAI's
    ``functions``/``tools`` with nested ``function``), and the
    provider's own tokeniser sees the post-serialisation form. For
    Phase 0 we approximate by JSON-encoding the array (sorted keys
    for determinism) and tokenising the resulting string.

    The result is a :class:`TokenCount` whose ``estimated`` flag
    reflects the underlying tokeniser's flag — Anthropic fallback
    propagates the flag, OpenAI exact does not. Acceptance bar (the
    spec): ±5% of provider-reported tool-schema counts on a typical
    5-tool array.

    Returns ``TokenCount(0, False)`` for an empty tools list.
    """
    if tools is None:
        raise TypeError("tokenise_tools: tools must not be None")
    if not isinstance(tools, (list, tuple)):
        raise TypeError(
            f"tokenise_tools: tools must be list or tuple, "
            f"got {type(tools).__name__}"
        )
    if len(tools) == 0:
        return TokenCount(0, False)

    serialised = json.dumps(
        list(tools),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return tokenise(serialised, model)
