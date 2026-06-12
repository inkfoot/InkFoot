"""Anthropic attribution recipe — translate one ``messages.create``
request + response pair into a :class:`NeutralCall`.

Direct-from-response fields (no tokeniser needed) come from
:meth:`inkfoot.providers.anthropic.AnthropicProvider.map_usage`:

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
* ``retrieved_context_tokens`` — the current implementation leaves at 0; ``tag_retrieval``
  can populate this from explicit user markers.

Bookkeeping (left zero by the translator):

* ``summariser_tokens`` — always 0 here; when a call is a
  ``CheapSummariser`` helper call the emit path re-attributes its
  structural input to this category after translation (see
  ``inkfoot.shims._emit._fold_into_summariser_tokens``).
* ``guardrail_tokens`` — wired when guardrails are connected.
* ``retry_overhead_tokens`` — populated by the retry classifier.

The translator is *pure*: given a fixed request, response, and
:class:`InMemoryRunState` snapshot, the output is deterministic. The
``InMemoryRunState`` is mutated as a side-effect (the stable prefix
shortens) so subsequent calls see the updated state.
"""

from __future__ import annotations

from typing import Any, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import (
    NeutralCall,
    NeutralError,
    _collect_runtime_metadata,
    update_stable_prefix,
)
from inkfoot.pricing import estimate_nanodollars
from inkfoot.providers.anthropic import AnthropicProvider, response_content
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

_PROVIDER = AnthropicProvider.PROVIDER_TYPE
_PROVIDER_IMPL = AnthropicProvider()


def _extract_system_block(request: dict[str, Any]) -> str:
    """Anthropic accepts ``system`` as a top-level string, or as a
    list of content blocks (each ``{"type": "text", "text": "..."}``).
    Both shapes collapse to a single string for tokenisation /
    stable-prefix detection.

    TODO(future/smells): when the cache_control smell lands, this
    helper (or a sibling) will need to expose the ``cache_control``
    markers on individual system blocks — they identify the cached
    span and let the smell flag misplaced or absent boundaries. For
    The current implementation only needs the text content; markers are intentionally
    dropped here.
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


_BLOCK_TEXT_MAX_DEPTH = 4


def _block_text(block: Any, *, depth: int = 0) -> str:
    """Pull the text content off a single message block. Handles the
    common shapes; unknown shapes yield empty string rather than
    raising.

    The optional ``depth`` parameter guards against pathological
    nesting in ``tool_result.content`` arrays — the Anthropic SDK
    will never produce more than 2 levels in practice, but
    :func:`dict_to_neutral_call` accepts arbitrary dicts and a
    self-referential payload would otherwise stack-overflow. We
    short-circuit past ``_BLOCK_TEXT_MAX_DEPTH``.
    """
    if depth > _BLOCK_TEXT_MAX_DEPTH:
        return ""
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
            return "".join(
                _block_text(b, depth=depth + 1) for b in content
            )
    return ""


def _current_user_text(request: dict[str, Any]) -> str:
    """Concatenate text content blocks from the *last* user message
    in the messages array — that's "the current turn's
    user input".

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

        usage = _PROVIDER_IMPL.map_usage(response)
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

        output_tokens = usage.output_tokens
        cache_read = usage.cache_read_tokens
        cache_create = usage.cache_creation_tokens
        reasoning = usage.reasoning_tokens

        # Run lifecycle: consume any pending tag_retrieval marker. The user
        # called ``inkfoot.tag_retrieval(text)`` before this LLM
        # call; the resulting token count rides on *this* call's
        # ledger as retrieved_context_tokens and the pending
        # counter resets to zero so the next call gets a fresh slate.
        retrieved = int(getattr(run_state, "pending_retrieved_context_tokens", 0) or 0)
        run_state.pending_retrieved_context_tokens = 0

        ledger = CausalTokenLedger(
            system_static_tokens=sys_static.value,
            system_dynamic_tokens=sys_dynamic.value,
            user_input_tokens=user_input.value,
            tool_schema_tokens=tool_schema.value,
            tool_result_tokens=tool_result.value,
            memory_tokens=memory.value,
            retrieved_context_tokens=retrieved,
            retry_overhead_tokens=0,  # populated when retry classifier ships
            summariser_tokens=0,  # re-attributed post-translation for summariser calls
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

        # Empty-string / missing tool names are dropped *silently*
        # rather than surfaced — a tool with no name can't actually
        # be called, so it has no business appearing on the bar
        # chart. If a future contributor wants to flag misconfigured
        # tools, that's a smell-engine job, not a translator job.
        tools_offered = tuple(
            t.get("name", "") if isinstance(t, dict) else ""
            for t in (request.get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        )

        # Pattern-C metadata pass-through (framework metadata contract). When a
        # framework adapter or :func:`inkfoot.tag_node` has set
        # ``run_state.node_name``, attach it to the neutral payload
        # so ``inkfoot report --group-by node`` can slice the ledger.
        metadata = _collect_runtime_metadata(run_state)

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
            cache_status=usage.cache_status,
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
            metadata=metadata,
        )


def _tool_calls_in_response(response: Any) -> tuple[str, ...]:
    """Names of tools the assistant invoked in this response. Each
    ``tool_use`` content block carries a ``name``."""
    content = response_content(response)
    if not isinstance(content, list):
        return ()
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return tuple(names)
