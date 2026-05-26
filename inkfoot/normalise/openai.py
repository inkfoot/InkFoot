"""OpenAI attribution recipe — mirror of the Anthropic translator
for ``chat.completions.create``.

Key differences from Anthropic:

* ``usage.prompt_tokens`` aggregates *fresh + cached* input tokens
  (no separate ``cache_read`` line item like Anthropic). Cached
  input lives inside ``usage.prompt_tokens_details.cached_tokens``;
  we lift that into ``cache_read_tokens`` per the §5.3 recipe.
* OpenAI doesn't bill cache *writes* — ``cache_creation_tokens`` is
  always 0 here. The pricing module has ``cache_write=0`` for
  OpenAI rows to match.
* Reasoning tokens (o-series) live in
  ``usage.completion_tokens_details.reasoning_tokens``.
* Tools live in the request as ``tools`` (each ``{"type":
  "function", "function": {"name": ..., ...}}``). We unwrap the
  inner ``function`` dict for tokenisation so the shape we hash
  matches what OpenAI's tokeniser sees.
* Messages are flat (no nested content blocks unless tool calls /
  tool results are involved). Tool-result messages have
  ``role="tool"``.
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
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

_PROVIDER = "openai"


def _usage(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, dict):
        return response.get("usage") or {}
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }
    pt_details = getattr(usage, "prompt_tokens_details", None)
    if pt_details is not None:
        out["prompt_tokens_details"] = {
            "cached_tokens": getattr(pt_details, "cached_tokens", 0),
        }
    ct_details = getattr(usage, "completion_tokens_details", None)
    if ct_details is not None:
        out["completion_tokens_details"] = {
            "reasoning_tokens": getattr(ct_details, "reasoning_tokens", 0),
        }
    return out


def _extract_system_text(request: dict[str, Any]) -> str:
    """OpenAI's ``system`` lives as the first message with
    ``role="system"`` (or ``"developer"`` on newer models)."""
    for msg in request.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in {"system", "developer"}:
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
    return ""


def _current_user_text(request: dict[str, Any]) -> str:
    last_user_text = ""
    for msg in request.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                last_user_text = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
    return last_user_text


def _tool_results_text(request: dict[str, Any]) -> str:
    parts: list[str] = []
    for msg in request.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "".join(parts)


def _memory_text(request: dict[str, Any]) -> str:
    """Every prior assistant/user message (excluding the current
    user turn and the system block) plus assistant turns. Tool-result
    messages are accounted under ``tool_result_tokens``."""
    messages = request.get("messages") or []
    last_user_idx: Optional[int] = None
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
    parts: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in {"system", "developer", "tool"}:
            continue
        if i == last_user_idx:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "".join(parts)


def _normalise_tools_for_tokenisation(
    tools: Any,
) -> list[dict[str, Any]]:
    """OpenAI's tools array nests the schema under ``"function"``.
    For tokenisation we collapse to the inner dict so the bytes look
    closer to what the provider's tokeniser sees."""
    if not isinstance(tools, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            fn = tool.get("function")
            if isinstance(fn, dict):
                out.append(fn)
                continue
        out.append(tool)
    return out


def _tool_names(tools: Any) -> tuple[str, ...]:
    names: list[str] = []
    if not isinstance(tools, (list, tuple)):
        return ()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            fn = tool.get("function") or {}
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                names.append(fn["name"])
        elif isinstance(tool.get("name"), str):
            names.append(tool["name"])
    return tuple(names)


def _tool_calls_in_response(response: Any) -> tuple[str, ...]:
    """OpenAI surfaces tool calls under ``choices[0].message.tool_calls``."""
    if isinstance(response, dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", []) or []
    if not choices:
        return ()
    first = choices[0]
    msg = first.get("message") if isinstance(first, dict) else getattr(
        first, "message", None
    )
    if msg is None:
        return ()
    tool_calls = (
        msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
    )
    if not tool_calls:
        return ()
    out: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
        if fn is None:
            continue
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
        if isinstance(name, str) and name:
            out.append(name)
    return tuple(out)


def _cache_status(usage: dict[str, Any]) -> str:
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    if cached > 0:
        return "hit"
    return "n/a"


class OpenAITranslator:
    """Stateless OpenAI translator; usage shape per
    ``chat.completions``. Reasoning/tool-call extraction handles
    o-series + the standard tools API."""

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
        model = request.get("model") or ""
        if not isinstance(model, str) or not model:
            raise ValueError("OpenAITranslator.translate: model is required")

        usage = _usage(response)
        system_text = _extract_system_text(request)
        run_state.stable_system_prefix = update_stable_prefix(
            run_state.stable_system_prefix, system_text
        )
        static_text = run_state.stable_system_prefix
        dynamic_text = system_text[len(static_text):] if system_text else ""

        sys_static = tokenise_with_flags(static_text, model)
        sys_dynamic = tokenise_with_flags(dynamic_text, model)
        user_input = tokenise_with_flags(_current_user_text(request), model)
        normalised_tools = _normalise_tools_for_tokenisation(
            request.get("tools")
        )
        tool_schema = tokenise_tools(normalised_tools, model)
        tool_result = tokenise_with_flags(_tool_results_text(request), model)
        memory = tokenise_with_flags(_memory_text(request), model)

        completion_tokens = int(usage.get("completion_tokens") or 0)
        reasoning = int(
            (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            )
            or 0
        )
        cache_read = int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )

        # E5: consume any pending tag_retrieval marker — see
        # AnthropicTranslator.translate for the full rationale.
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
            retry_overhead_tokens=0,
            summariser_tokens=0,
            reasoning_tokens=reasoning,
            guardrail_tokens=0,
            cache_creation_tokens=0,  # OpenAI: no billed cache writes
            cache_read_tokens=cache_read,
            output_tokens=completion_tokens,
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

        # ADR-1-1 — Pattern-C metadata pass-through (mirror of the
        # Anthropic translator's handling).
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
            tools_offered=_tool_names(request.get("tools")),
            tools_called=_tool_calls_in_response(response),
            error=error,
            cache_status=_cache_status(usage),
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
            metadata=metadata,
        )
