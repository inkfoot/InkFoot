"""LangChain attribution recipe — translate one chat-model invocation
captured by :class:`inkfoot.langchain.InkfootCallbackHandler` into a
:class:`NeutralCall`.

LangChain normalises every provider's usage block into
``AIMessage.usage_metadata`` (``input_tokens`` / ``output_tokens`` /
``input_token_details`` / ``output_token_details``), so one recipe
covers ChatAnthropic, ChatOpenAI (chat and Responses), AzureChatOpenAI,
ChatGoogleGenerativeAI, and ChatBedrock. Direct-from-response fields:

* ``output_tokens`` — ``usage_metadata["output_tokens"]``
* ``cache_read_tokens`` — ``input_token_details["cache_read"]``
* ``cache_creation_tokens`` — ``input_token_details["cache_creation"]``
* ``reasoning_tokens`` — ``output_token_details["reasoning"]``

Older integrations that predate ``usage_metadata`` ship the counters
as ``response_metadata["token_usage"]`` (``prompt_tokens`` /
``completion_tokens``); that shape is honoured as a fallback. When
*neither* is present the call still emits — with an all-zero usage
overlay and ``estimation_flags`` carrying
:data:`USAGE_METADATA_MISSING_FLAG` so reports can say so honestly.

The request side is re-tokenised from the LangChain message list
(``BaseMessage`` objects, duck-typed on ``.type`` / ``.content`` so
dict fixtures work too) using the same causal split as the raw-SDK
recipes:

* ``system_static_tokens`` / ``system_dynamic_tokens`` — system
  messages, split by the longest-stable-prefix from
  :class:`InMemoryRunState`.
* ``user_input_tokens`` — the *last* human message (minus any
  embedded tool-result blocks).
* ``tool_schema_tokens`` — the bound ``tools`` array from
  ``invocation_params``.
* ``tool_result_tokens`` — every tool message, plus tool-result
  blocks embedded in human messages (Anthropic-style payloads).
* ``memory_tokens`` — every prior turn that isn't the current user
  message and isn't a tool result.
* ``retrieved_context_tokens`` — consumed from the run's pending
  :func:`inkfoot.tag_retrieval` marker, like every other recipe.

This module deliberately does **not** import ``langchain_core`` —
everything is duck-typed, so the translator stays importable (and
unit-testable) without the optional dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import (
    NeutralCall,
    NeutralError,
    _collect_runtime_metadata,
    update_stable_prefix,
)
from inkfoot.pricing import estimate_nanodollars
from inkfoot.providers.base import TokenUsage, coerce_token_count
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

__all__ = [
    "LangChainTranslator",
    "ResponseSummary",
    "USAGE_METADATA_MISSING_FLAG",
    "map_provider",
    "summarise_response",
    "usage_overlay",
]


# Estimation flag stamped on events whose response carried no usage
# counters in any known shape. Reports surface it as "usage numbers
# are missing for this call", distinct from the per-category
# tokeniser-fallback flags.
USAGE_METADATA_MISSING_FLAG = "usage_metadata_missing"


# LangChain provider identifiers (``response_metadata["model_provider"]``
# and the ``ls_provider`` tracing metadata key) → Inkfoot's provider
# vocabulary, which is what the pricing table and reports key on.
# Azure deployments serve OpenAI models at OpenAI-shaped prices, so
# they map onto ``openai``; the raw LangChain identifier is preserved
# on ``NeutralCall.metadata["langchain_provider"]`` whenever the
# mapping renames it.
_PROVIDER_ALIASES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure": "openai",
    "azure_openai": "openai",
    "azure-openai": "openai",
    "google_genai": "gemini",
    "google-genai": "gemini",
    "google_vertexai": "gemini",
    "google-vertexai": "gemini",
    "gemini": "gemini",
    "bedrock": "bedrock",
    "amazon_bedrock": "bedrock",
    "bedrock_converse": "bedrock",
}


def map_provider(raw: Optional[str], model: str = "") -> str:
    """Map a LangChain provider identifier onto Inkfoot's provider
    vocabulary. Unknown identifiers pass through verbatim (the
    pricing lookup simply misses); when no identifier is available
    at all, fall back to a conservative model-name sniff."""
    if isinstance(raw, str) and raw.strip():
        key = raw.strip().lower()
        return _PROVIDER_ALIASES.get(key, key)
    lowered = (model or "").lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt-", "o1", "o3", "chatgpt-")):
        return "openai"
    if lowered.startswith(("gemini", "models/gemini")):
        return "gemini"
    return "unknown"


# ----------------------------------------------------------------------
# Message-list helpers (duck-typed over BaseMessage | dict)
# ----------------------------------------------------------------------

_ROLE_TO_KIND = {
    "system": "system",
    "human": "human",
    "user": "human",
    "ai": "ai",
    "assistant": "ai",
    "tool": "tool",
    "function": "tool",
}


def _message_kind(msg: Any) -> str:
    """Classify one message as ``system`` / ``human`` / ``ai`` /
    ``tool`` (empty string for unrecognised shapes). Reads
    ``BaseMessage.type`` or the ``type`` / ``role`` key of a dict."""
    if isinstance(msg, dict):
        raw = msg.get("type") or msg.get("role") or ""
    else:
        raw = getattr(msg, "type", "") or ""
    return _ROLE_TO_KIND.get(str(raw).lower(), "")


def _message_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _content_text(content: Any, *, skip_tool_results: bool = True) -> str:
    """Pull the text off a message ``content`` value. Handles the
    plain-string form and content-block lists (string entries plus
    ``{"type": "text", "text": ...}`` blocks). Tool-result blocks
    are skipped by default — they're counted separately."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            if not skip_tool_results:
                parts.append(_tool_result_block_text(block))
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _tool_result_block_text(block: dict[str, Any]) -> str:
    inner = block.get("content", "")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return _content_text(inner, skip_tool_results=False)
    return ""


def _last_human_index(messages: list[Any]) -> Optional[int]:
    last: Optional[int] = None
    for i, msg in enumerate(messages):
        if _message_kind(msg) == "human":
            last = i
    return last


def _system_text(messages: list[Any]) -> str:
    return "".join(
        _content_text(_message_content(m))
        for m in messages
        if _message_kind(m) == "system"
    )


def _user_input_text(messages: list[Any]) -> str:
    idx = _last_human_index(messages)
    if idx is None:
        return ""
    return _content_text(_message_content(messages[idx]))


def _tool_result_text(messages: list[Any]) -> str:
    """Every tool message, plus any tool-result blocks riding on
    human messages (the Anthropic content-block convention)."""
    parts: list[str] = []
    for msg in messages:
        kind = _message_kind(msg)
        content = _message_content(msg)
        if kind == "tool":
            parts.append(_content_text(content, skip_tool_results=False))
        elif kind == "human" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    parts.append(_tool_result_block_text(block))
    return "".join(parts)


def _memory_text(messages: list[Any]) -> str:
    """Every prior conversational turn: ai messages plus human
    messages other than the last one. System, tool, and tool-result
    content is excluded — those have their own categories."""
    idx = _last_human_index(messages)
    parts: list[str] = []
    for i, msg in enumerate(messages):
        kind = _message_kind(msg)
        if kind not in ("ai", "human") or i == idx:
            continue
        parts.append(_content_text(_message_content(msg)))
    return "".join(parts)


def _tool_name(tool: Any) -> str:
    """Tool name from either the flat shape (``{"name": ...}``,
    Anthropic-style) or the nested OpenAI function shape
    (``{"type": "function", "function": {"name": ...}}``)."""
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if isinstance(name, str) and name:
        return name
    fn = tool.get("function")
    if isinstance(fn, dict):
        inner = fn.get("name")
        if isinstance(inner, str):
            return inner
    return ""


# ----------------------------------------------------------------------
# Response-side extraction
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResponseSummary:
    """Everything the handler and translator read off one
    ``LLMResult`` (or a dict fixture of the same shape).

    ``usage_metadata`` is ``None`` only when no usage counters were
    found in *any* known shape — the translator turns that into the
    :data:`USAGE_METADATA_MISSING_FLAG` estimation flag.
    """

    usage_metadata: Optional[dict[str, Any]]
    response_metadata: dict[str, Any] = field(default_factory=dict)
    model_name: Optional[str] = None
    model_provider: Optional[str] = None
    response_id: Optional[str] = None
    tool_calls: tuple[str, ...] = ()


def _first_generation_message(response: Any) -> Any:
    if isinstance(response, dict):
        generations = response.get("generations")
    else:
        generations = getattr(response, "generations", None)
    if not generations or not generations[0]:
        return None
    gen = generations[0][0]
    if isinstance(gen, dict):
        return gen.get("message")
    return getattr(gen, "message", None)


def _read(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def summarise_response(response: Any) -> ResponseSummary:
    """Extract the usage overlay, provider/model identifiers, the
    provider response id, and the tool-call names from one
    ``LLMResult``. Tolerant: missing pieces come back as ``None`` /
    empty rather than raising."""
    message = _first_generation_message(response)
    response_metadata = _read(message, "response_metadata", {}) or {}
    if not isinstance(response_metadata, dict):
        response_metadata = {}
    llm_output = _read(response, "llm_output", {}) or {}
    if not isinstance(llm_output, dict):
        llm_output = {}

    usage = _read(message, "usage_metadata")
    if not isinstance(usage, dict) or not usage:
        usage = None
    if usage is None:
        # Older integrations: response_metadata["token_usage"] /
        # llm_output["token_usage"] with prompt/completion naming.
        token_usage = response_metadata.get("token_usage") or llm_output.get(
            "token_usage"
        )
        if isinstance(token_usage, dict) and token_usage:
            usage = {
                "input_tokens": coerce_token_count(
                    token_usage.get("prompt_tokens")
                ),
                "output_tokens": coerce_token_count(
                    token_usage.get("completion_tokens")
                ),
            }

    model_name = response_metadata.get("model_name") or llm_output.get(
        "model_name"
    )
    if not isinstance(model_name, str) or not model_name:
        model_name = None

    model_provider = response_metadata.get("model_provider")
    if not isinstance(model_provider, str) or not model_provider:
        model_provider = None

    response_id = response_metadata.get("id") or response_metadata.get(
        "response_id"
    )
    if not isinstance(response_id, str) or not response_id:
        # Most partner packages also stamp the provider response id
        # on the message itself.
        message_id = _read(message, "id")
        response_id = (
            message_id
            if isinstance(message_id, str) and message_id
            else None
        )

    raw_tool_calls = _read(message, "tool_calls", []) or []
    tool_calls = tuple(
        tc["name"]
        for tc in raw_tool_calls
        if isinstance(tc, dict) and isinstance(tc.get("name"), str) and tc["name"]
    )

    return ResponseSummary(
        usage_metadata=usage,
        response_metadata=response_metadata,
        model_name=model_name,
        model_provider=model_provider,
        response_id=response_id,
        tool_calls=tool_calls,
    )


def _cache_status(cache_read: int, cache_creation: int) -> str:
    """Same coarse classification the raw-SDK recipes use: ``hit``
    when only reads, ``partial`` when reads and writes, ``miss``
    when only writes, ``n/a`` when neither."""
    if cache_read > 0 and cache_creation > 0:
        return "partial"
    if cache_read > 0:
        return "hit"
    if cache_creation > 0:
        return "miss"
    return "n/a"


def usage_overlay(usage_metadata: Optional[dict[str, Any]]) -> TokenUsage:
    """Fold a LangChain ``usage_metadata`` dict into the neutral
    :class:`TokenUsage` overlay. ``input_tokens`` is already
    inclusive of the cached portion in LangChain's normalisation,
    matching the overlay's contract. ``None`` yields all zeros."""
    if not usage_metadata:
        return TokenUsage()
    details_in = usage_metadata.get("input_token_details") or {}
    details_out = usage_metadata.get("output_token_details") or {}
    if not isinstance(details_in, dict):
        details_in = {}
    if not isinstance(details_out, dict):
        details_out = {}
    cache_read = coerce_token_count(details_in.get("cache_read"))
    cache_creation = coerce_token_count(details_in.get("cache_creation"))
    return TokenUsage(
        input_tokens=coerce_token_count(usage_metadata.get("input_tokens")),
        output_tokens=coerce_token_count(usage_metadata.get("output_tokens")),
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=coerce_token_count(details_out.get("reasoning")),
        cache_status=_cache_status(cache_read, cache_creation),
    )


# ----------------------------------------------------------------------
# Translator
# ----------------------------------------------------------------------


class LangChainTranslator:
    """Stateless translator for handler-captured calls.

    ``request`` is the dict the callback handler assembles from its
    ``on_chat_model_start`` snapshot:

    * ``provider`` — already mapped to Inkfoot vocabulary.
    * ``model`` — resolved model name.
    * ``messages`` — the LangChain message list (``BaseMessage``
      objects or equivalent dicts).
    * ``tools`` — the bound tools array from ``invocation_params``.
    * ``metadata`` — optional extra entries merged onto
      ``NeutralCall.metadata`` (the handler stamps the LangChain
      run id and capture source here).

    ``response`` is the ``LLMResult`` (or a dict fixture).
    """

    provider = "langchain"

    def translate(
        self,
        *,
        request: dict[str, Any],
        response: Any,
        run_state: InMemoryRunState,
        started_at: int,
        ended_at: int,
        sequence: int = 0,
        parent_run_id: Optional[str] = None,
        error: Optional[NeutralError] = None,
    ) -> NeutralCall:
        model = request.get("model") or ""
        if not isinstance(model, str):
            model = ""
        provider = request.get("provider") or map_provider(None, model)
        messages = request.get("messages") or []
        if not isinstance(messages, (list, tuple)):
            messages = []
        messages = list(messages)
        raw_tools = request.get("tools") or []
        tools = [t for t in raw_tools if isinstance(t, dict)] if isinstance(
            raw_tools, (list, tuple)
        ) else []

        summary = summarise_response(response)
        usage = usage_overlay(summary.usage_metadata)

        system_text = _system_text(messages)
        run_state.stable_system_prefix = update_stable_prefix(
            run_state.stable_system_prefix, system_text
        )
        static_text = run_state.stable_system_prefix
        dynamic_text = system_text[len(static_text):] if system_text else ""

        sys_static = tokenise_with_flags(static_text, model)
        sys_dynamic = tokenise_with_flags(dynamic_text, model)
        user_input = tokenise_with_flags(_user_input_text(messages), model)
        tool_schema = tokenise_tools(tools, model)
        tool_result = tokenise_with_flags(_tool_result_text(messages), model)
        memory = tokenise_with_flags(_memory_text(messages), model)

        retrieved = int(
            getattr(run_state, "pending_retrieved_context_tokens", 0) or 0
        )
        run_state.pending_retrieved_context_tokens = 0

        ledger = CausalTokenLedger(
            system_static_tokens=sys_static.value,
            system_dynamic_tokens=sys_dynamic.value,
            user_input_tokens=user_input.value,
            tool_schema_tokens=tool_schema.value,
            tool_result_tokens=tool_result.value,
            memory_tokens=memory.value,
            retrieved_context_tokens=retrieved,
            retry_overhead_tokens=0,
            summariser_tokens=0,
            reasoning_tokens=usage.reasoning_tokens,
            guardrail_tokens=0,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
        )

        flags: list[str] = []
        if summary.usage_metadata is None:
            flags.append(USAGE_METADATA_MISSING_FLAG)
        for name, tc in (
            ("system_static_tokens", sys_static),
            ("system_dynamic_tokens", sys_dynamic),
            ("user_input_tokens", user_input),
            ("tool_schema_tokens", tool_schema),
            ("tool_result_tokens", tool_result),
            ("memory_tokens", memory),
        ):
            if tc.estimated:
                flags.append(name)

        tools_offered = tuple(
            name for name in (_tool_name(t) for t in tools) if name
        )

        metadata = _collect_runtime_metadata(run_state)
        extra = request.get("metadata")
        if isinstance(extra, dict):
            metadata.update(
                (k, v) for k, v in extra.items() if isinstance(k, str)
            )

        return NeutralCall(
            provider=provider,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            ledger=ledger,
            estimated_nanodollars=estimate_nanodollars(
                provider, model, ledger
            ),
            tools_offered=tools_offered,
            tools_called=summary.tool_calls,
            error=error,
            cache_status=usage.cache_status,
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
            metadata=metadata,
        )
