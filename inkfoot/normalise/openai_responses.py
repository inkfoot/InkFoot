"""OpenAI Responses API attribution recipe.

The Responses API renames almost every field the Chat Completions
translator keys on, so it gets its own recipe rather than a shim
into :mod:`inkfoot.normalise.openai`:

* The system block is the top-level ``instructions`` string (plus
  any ``system``/``developer``-role items in ``input``), not a
  ``messages[role=system]`` entry.
* ``input`` is either a plain string (one user turn) or a list of
  typed items — role-bearing messages, ``function_call`` echoes,
  and ``function_call_output`` tool results.
* Tools are flat (``{"type": "function", "name": ..., "parameters":
  ...}``) — no nested ``function`` dict to unwrap.
* Usage counters are ``usage.input_tokens`` / ``usage.output_tokens``
  with cache reads under ``input_tokens_details.cached_tokens`` and
  reasoning under ``output_tokens_details.reasoning_tokens``.
* Tool calls come back as ``output[]`` items with
  ``type="function_call"`` instead of
  ``choices[0].message.tool_calls``.

The Responses API surface is still growing, so the translator is
deliberately forgiving: a response with top-level keys it doesn't
recognise is flagged (``responses_shape_unknown:<key>``) and
translated anyway — never dropped, never raised on.
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
from inkfoot.providers.base import TokenUsage, coerce_token_count
from inkfoot.providers.openai import OpenAIProvider
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

__all__ = [
    "OpenAIResponsesTranslator",
    "extract_usage",
    "unknown_response_keys",
]

# Responses-API events price as OpenAI calls; there is no separate
# pricing provider for the surface.
_PROVIDER = OpenAIProvider.PROVIDER_TYPE

# Top-level keys of the Responses API response object. Anything
# outside this set is a shape we haven't mapped yet — flagged, not
# fatal (see :func:`unknown_response_keys`).
_KNOWN_RESPONSE_KEYS = frozenset(
    {
        "background",
        "billing",
        "conversation",
        "created_at",
        "error",
        "id",
        "incomplete_details",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "model",
        "object",
        "output",
        "output_text",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "status",
        "store",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "usage",
        "user",
    }
)

_SYSTEM_ROLES = frozenset({"system", "developer"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a dict (test fixtures) or an attribute
    (real SDK pydantic models)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _item_text(content: Any) -> str:
    """Collapse an item's ``content`` to its text. Content is either
    a plain string or a list of typed parts (``input_text`` /
    ``output_text`` carry a ``text`` field; image and file parts
    carry none and contribute nothing)."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            text = _get(part, "text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _input_items(request: dict[str, Any]) -> list[Any]:
    """The request ``input`` normalised to a list of items. A plain
    string is shorthand for a single user message."""
    raw = request.get("input")
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _system_text(request: dict[str, Any]) -> str:
    """``instructions`` plus any ``system``/``developer``-role items
    embedded in ``input``."""
    parts: list[str] = []
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        parts.append(instructions)
    for item in _input_items(request):
        if _get(item, "role") in _SYSTEM_ROLES:
            parts.append(_item_text(_get(item, "content")))
    return "".join(parts)


def _last_user_index(items: list[Any]) -> Optional[int]:
    last: Optional[int] = None
    for i, item in enumerate(items):
        if _get(item, "role") == "user":
            last = i
    return last


def _current_user_text(request: dict[str, Any]) -> str:
    items = _input_items(request)
    idx = _last_user_index(items)
    if idx is None:
        return ""
    return _item_text(_get(items[idx], "content"))


def _tool_results_text(request: dict[str, Any]) -> str:
    """``function_call_output`` items are the Responses-API form of
    tool-result messages."""
    parts: list[str] = []
    for item in _input_items(request):
        if _get(item, "type") != "function_call_output":
            continue
        output = _get(item, "output")
        if isinstance(output, str):
            parts.append(output)
        else:
            parts.append(_item_text(output))
    return "".join(parts)


def _memory_text(request: dict[str, Any]) -> str:
    """Prior role-bearing turns: everything except the current user
    item and the system/developer items. ``function_call`` /
    ``function_call_output`` items are excluded — the latter are
    accounted under ``tool_result_tokens``, and tool-call echoes are
    skipped for parity with the Chat Completions recipe."""
    items = _input_items(request)
    last_user = _last_user_index(items)
    parts: list[str] = []
    for i, item in enumerate(items):
        role = _get(item, "role")
        if role is None or role in _SYSTEM_ROLES:
            continue
        if i == last_user:
            continue
        parts.append(_item_text(_get(item, "content")))
    return "".join(parts)


def _normalise_tools_for_tokenisation(tools: Any) -> list[dict[str, Any]]:
    """Responses-API tools are already flat — keep them verbatim so
    the bytes we count match the wire shape."""
    if not isinstance(tools, (list, tuple)):
        return []
    return [tool for tool in tools if isinstance(tool, dict)]


def _tool_names(tools: Any) -> tuple[str, ...]:
    """Offered-tool identifiers: function tools carry a ``name``;
    built-in tools (web search, file search, ...) are identified by
    their ``type``."""
    names: list[str] = []
    if not isinstance(tools, (list, tuple)):
        return ()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
            continue
        tool_type = tool.get("type")
        if isinstance(tool_type, str) and tool_type and tool_type != "function":
            names.append(tool_type)
    return tuple(names)


def _tool_calls_in_response(response: Any) -> tuple[str, ...]:
    """Tool calls are ``output[]`` items with
    ``type="function_call"``; a response can carry several."""
    output = _get(response, "output") or []
    if not isinstance(output, (list, tuple)):
        return ()
    names: list[str] = []
    for item in output:
        if _get(item, "type") != "function_call":
            continue
        name = _get(item, "name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _response_keys(response: Any) -> Optional[set[str]]:
    """Enumerable top-level keys of the response, or ``None`` when
    the object can't be enumerated (attribute-only duck types)."""
    if isinstance(response, dict):
        return set(response)
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # pylint: disable=broad-except
            return None
        if isinstance(dumped, dict):
            return set(dumped)
    return None


def unknown_response_keys(response: Any) -> tuple[str, ...]:
    """Top-level response keys outside the mapped Responses-API
    shape, sorted. Used to flag — never to reject — new shapes."""
    keys = _response_keys(response)
    if keys is None:
        return ()
    return tuple(sorted(keys - _KNOWN_RESPONSE_KEYS))


def extract_usage(response: Any) -> dict[str, Any]:
    """The ``usage`` block as a plain dict, accepting both
    attribute-access (real SDK) and dict-access (fixtures) shapes."""
    if response is None:
        return {}
    usage = _get(response, "usage")
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }
    input_details = getattr(usage, "input_tokens_details", None)
    if input_details is not None:
        out["input_tokens_details"] = {
            "cached_tokens": getattr(input_details, "cached_tokens", 0),
        }
    output_details = getattr(usage, "output_tokens_details", None)
    if output_details is not None:
        out["output_tokens_details"] = {
            "reasoning_tokens": getattr(
                output_details, "reasoning_tokens", 0
            ),
        }
    return out


def _details(usage: dict[str, Any], key: str) -> dict[str, Any]:
    details = usage.get(key)
    return details if isinstance(details, dict) else {}


def map_usage(response: Any) -> TokenUsage:
    """Responses-API usage onto the neutral overlay.

    ``usage.input_tokens`` is inclusive of the cached portion (same
    billing meaning as Chat Completions' ``prompt_tokens``), and
    OpenAI bills no cache writes — so ``cache_creation_tokens`` is
    always 0 and cache status is only ever ``hit`` or ``n/a``.
    """
    usage = extract_usage(response)
    cache_read = coerce_token_count(
        _details(usage, "input_tokens_details").get("cached_tokens")
    )
    return TokenUsage(
        input_tokens=coerce_token_count(usage.get("input_tokens")),
        output_tokens=coerce_token_count(usage.get("output_tokens")),
        cache_read_tokens=cache_read,
        cache_creation_tokens=0,  # no billed cache writes
        reasoning_tokens=coerce_token_count(
            _details(usage, "output_tokens_details").get("reasoning_tokens")
        ),
        cache_status="hit" if cache_read > 0 else "n/a",
    )


class OpenAIResponsesTranslator:
    """Stateless Responses-API translator. The same recipe covers
    Azure OpenAI deployments — their Responses surface routes
    through the identical request/response shapes."""

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
            raise ValueError(
                "OpenAIResponsesTranslator.translate: model is required"
            )

        usage = map_usage(response)
        system_text = _system_text(request)
        run_state.stable_system_prefix = update_stable_prefix(
            run_state.stable_system_prefix, system_text
        )
        static_text = run_state.stable_system_prefix
        dynamic_text = system_text[len(static_text):] if system_text else ""

        sys_static = tokenise_with_flags(static_text, model)
        sys_dynamic = tokenise_with_flags(dynamic_text, model)
        user_input = tokenise_with_flags(_current_user_text(request), model)
        tool_schema = tokenise_tools(
            _normalise_tools_for_tokenisation(request.get("tools")), model
        )
        tool_result = tokenise_with_flags(_tool_results_text(request), model)
        memory = tokenise_with_flags(_memory_text(request), model)

        # Run lifecycle: consume any pending tag_retrieval marker — see
        # AnthropicTranslator.translate for the full rationale.
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
            cache_creation_tokens=0,  # OpenAI: no billed cache writes
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
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
        flags.extend(
            f"responses_shape_unknown:{key}"
            for key in unknown_response_keys(response)
        )

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
            cache_status=usage.cache_status,
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
            metadata=metadata,
        )
