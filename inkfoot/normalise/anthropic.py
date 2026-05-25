"""Anthropic attribution recipe — translate one ``messages.create``
request + response pair into a :class:`NeutralCall`.

Direct-from-response fields (no tokeniser needed):

* ``output_tokens`` — ``response.usage.output_tokens``
* ``cache_read_tokens`` — ``response.usage.cache_read_input_tokens``
* ``cache_creation_tokens`` — ``response.usage.cache_creation_input_tokens``
* ``reasoning_tokens`` — sum of token counts on ``thinking`` content
  blocks (Anthropic surfaces these as part of the assistant message
  on extended-thinking models)

Re-tokenised against the request:

* ``system_static_tokens`` / ``system_dynamic_tokens`` — split by
  the longest-stable-prefix from :class:`InMemoryRunState`.
* ``user_input_tokens`` — current turn's user message.
* ``tool_schema_tokens`` — serialised ``tools`` array.
* ``tool_result_tokens`` — content of every ``tool_result`` block
  in the messages array (Anthropic embeds tool results as content
  blocks on ``user`` role messages).
* ``memory_tokens`` — every prior assistant/user turn that isn't
  the current-turn user message and isn't a tool result.
* ``retrieved_context_tokens`` — Phase 0 leaves at 0; ``tag_retrieval``
  lands in E5 and will populate this from explicit user markers.

Bookkeeping (left zero in Phase 0):

* ``summariser_tokens`` — Phase 2's modification policies will lift
  this.
* ``guardrail_tokens`` — wired when guardrails are connected.
* ``retry_overhead_tokens`` — populated by E4 when retry events fire.

The translator is *pure*: given a fixed request, response, and
:class:`InMemoryRunState` snapshot, the output is deterministic. The
``InMemoryRunState`` is mutated as a side-effect (the stable prefix
shortens) so subsequent calls see the updated state.
"""

from __future__ import annotations

from typing import Any, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall, NeutralError, update_stable_prefix
from inkfoot.pricing import estimate_nanodollars
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

_PROVIDER = "anthropic"


def _extract_system_block(request: dict[str, Any]) -> str:
    """Anthropic accepts ``system`` as a top-level string, or as a
    list of content blocks (each ``{"type": "text", "text": "..."}``).
    Both shapes collapse to a single string for tokenisation /
    stable-prefix detection.
    """
    raw = request.get("system")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    return ""


def _block_text(block: Any) -> str:
    """Pull the text content off a single message block. Handles the
    common shapes; unknown shapes yield empty string rather than
    raising."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        txt = block.get("text", "")
        return txt if isinstance(txt, str) else ""
    if block.get("type") == "tool_result":
        content = block.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(_block_text(b) for b in content)
    return ""


def _current_user_text(request: dict[str, Any]) -> str:
    """Concatenate text content blocks from the *last* user message
    in the messages array — that's "the current turn's user input"
    per the §5.3 recipe.

    Tool-result blocks (which ride on user-role messages in
    Anthropic's API) are excluded — those count toward
    ``tool_result_tokens`` instead.
    """
    messages = request.get("messages") or []
    last_user: Optional[dict[str, Any]] = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = msg
    if not last_user:
        return ""
    content = last_user.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                continue
            parts.append(_block_text(block))
        return "".join(parts)
    return ""


def _tool_results_text(request: dict[str, Any]) -> str:
    """Every ``tool_result`` block across the messages array,
    concatenated. Multi-turn runs accumulate these — that's the
    point: tool results are typically the largest, fastest-growing
    input-side cost contributor on real agents."""
    parts: list[str] = []
    messages = request.get("messages") or []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(_block_text(block))
    return "".join(parts)


def _memory_text(request: dict[str, Any]) -> str:
    """Every prior turn that isn't the current-turn user message
    and isn't a tool-result block: prior assistant turns + prior
    user turns. Returns the concatenated text."""
    messages = request.get("messages") or []
    if not messages:
        return ""
    # Drop the last user message — that's "current input".
    last_user_idx: Optional[int] = None
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
    parts: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if i == last_user_idx:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    continue
                parts.append(_block_text(block))
    return "".join(parts)


def _reasoning_token_count(response: Any) -> int:
    """Sum of token counts on ``thinking`` content blocks from the
    assistant response — Anthropic's extended-thinking models surface
    these alongside text blocks. Zero on models without thinking."""
    if response is None:
        return 0
    usage = _usage(response)
    # Some Anthropic SDKs surface a top-level usage.thinking_tokens.
    thinking = usage.get("thinking_tokens") if isinstance(usage, dict) else None
    if isinstance(thinking, int) and thinking >= 0:
        return thinking
    # Otherwise sum token counts across ``thinking`` content blocks.
    content = _response_content(response)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            tokens = block.get("tokens")
            if isinstance(tokens, int) and tokens >= 0:
                total += tokens
    return total


def _usage(response: Any) -> dict[str, Any]:
    """Accept both attribute-access (real SDK) and dict-access (test
    fixtures) shapes."""
    if response is None:
        return {}
    if isinstance(response, dict):
        usage = response.get("usage", {})
    else:
        usage = getattr(response, "usage", {})
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    # SDK object — read fields via getattr with defaults.
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(
            usage, "cache_read_input_tokens", 0
        ),
        "cache_creation_input_tokens": getattr(
            usage, "cache_creation_input_tokens", 0
        ),
        "thinking_tokens": getattr(usage, "thinking_tokens", None),
    }


def _response_content(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("content")
    return getattr(response, "content", None)


def _cache_status(usage: dict[str, Any]) -> str:
    """Coarse cache classification from usage. ``hit`` when any
    cache_read, ``partial`` when both read + write, ``miss`` when
    only write, ``n/a`` when neither."""
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    if read > 0 and write > 0:
        return "partial"
    if read > 0:
        return "hit"
    if write > 0:
        return "miss"
    return "n/a"


class AnthropicTranslator:
    """Stateless translator. Pass the same instance across calls for
    a single run so the stable-prefix detector tracks history via
    the :class:`InMemoryRunState` argument."""

    provider = _PROVIDER

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
        """Build the :class:`NeutralCall` for one Anthropic call.

        ``request`` is the kwargs the user passed to
        ``messages.create`` — at minimum ``model`` and ``messages``;
        optionally ``system`` and ``tools``.

        ``response`` is the SDK return value or a dict-like fixture
        with at least ``usage`` (and ``content`` if reasoning tokens
        matter).

        Side-effect: updates ``run_state.stable_system_prefix``
        before computing the static/dynamic split.
        """
        model = request.get("model") or ""
        if not isinstance(model, str) or not model:
            raise ValueError("AnthropicTranslator.translate: model is required")

        usage = _usage(response)
        system_text = _extract_system_block(request)
        run_state.stable_system_prefix = update_stable_prefix(
            run_state.stable_system_prefix, system_text
        )
        static_text = run_state.stable_system_prefix
        dynamic_text = system_text[len(static_text):] if system_text else ""

        sys_static = tokenise_with_flags(static_text, model)
        sys_dynamic = tokenise_with_flags(dynamic_text, model)
        user_input = tokenise_with_flags(_current_user_text(request), model)
        tool_schema = tokenise_tools(request.get("tools") or [], model)
        tool_result = tokenise_with_flags(_tool_results_text(request), model)
        memory = tokenise_with_flags(_memory_text(request), model)

        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        reasoning = _reasoning_token_count(response)

        ledger = CausalTokenLedger(
            system_static_tokens=sys_static.value,
            system_dynamic_tokens=sys_dynamic.value,
            user_input_tokens=user_input.value,
            tool_schema_tokens=tool_schema.value,
            tool_result_tokens=tool_result.value,
            memory_tokens=memory.value,
            retrieved_context_tokens=0,  # E5
            retry_overhead_tokens=0,  # E4
            summariser_tokens=0,  # Phase 2
            reasoning_tokens=reasoning,
            guardrail_tokens=0,
            cache_creation_tokens=cache_create,
            cache_read_tokens=cache_read,
            output_tokens=output_tokens,
        )

        flags: list[str] = []
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
            t.get("name", "") if isinstance(t, dict) else ""
            for t in (request.get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        )

        return NeutralCall(
            provider=_PROVIDER,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            ledger=ledger,
            estimated_nanodollars=estimate_nanodollars(
                _PROVIDER, model, ledger
            ),
            tools_offered=tools_offered,
            tools_called=_tool_calls_in_response(response),
            error=error,
            cache_status=_cache_status(usage),
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
        )


def _tool_calls_in_response(response: Any) -> tuple[str, ...]:
    """Names of tools the assistant invoked in this response. Each
    ``tool_use`` content block carries a ``name``."""
    content = _response_content(response)
    if not isinstance(content, list):
        return ()
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return tuple(names)
