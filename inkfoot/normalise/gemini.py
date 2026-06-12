"""Gemini attribution recipe — mirror of the Anthropic/OpenAI
translators for ``GenerativeModel.generate_content``.

Key differences from the other two:

* The request dict is *synthesised by the shim*: Gemini binds
  ``system_instruction`` and ``tools`` to the model object at
  construction time, so the shim copies them onto the per-call
  request dict (see :mod:`inkfoot.shims.gemini`) and this translator
  reads them back like ordinary kwargs.
* Usage counters come from
  :meth:`inkfoot.providers.gemini.GeminiProvider.map_usage` — see
  that module for the billing-shape notes (inclusive
  ``prompt_token_count``, thinking folded into output, no per-call
  cache writes).
* Cache attribution: ``map_usage`` maps any cached-content count to
  ``cache_read_tokens``. When the run state carries
  ``pending_cache_resource_creation`` (set by the cache-resource arm
  of ``CacheControlPlacer`` right after it created a
  ``CachedContent`` resource), this translator re-attributes that
  count to ``cache_creation_tokens`` and stamps the call ``miss`` —
  the one-time write. Subsequent calls read ``hit`` as usual.
* Messages live in ``contents``: each entry is
  ``{"role": "user"|"model", "parts": [...]}`` where a part is a
  bare string, ``{"text": ...}``, ``{"function_call": ...}``, or
  ``{"function_response": ...}``. Tool results are
  ``function_response`` parts; tools are
  ``{"function_declarations": [...]}`` wrappers.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import (
    NeutralCall,
    NeutralError,
    _collect_runtime_metadata,
    update_stable_prefix,
)
from inkfoot.pricing import estimate_nanodollars
from inkfoot.providers.gemini import GeminiProvider, response_candidates
from inkfoot.run import InMemoryRunState
from inkfoot.tokenisers import tokenise_tools, tokenise_with_flags

_PROVIDER = GeminiProvider.PROVIDER_TYPE
_PROVIDER_IMPL = GeminiProvider()


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        txt = part.get("text", "")
        return txt if isinstance(txt, str) else ""
    return ""


def _is_function_response(part: Any) -> bool:
    return isinstance(part, dict) and "function_response" in part


def _normalised_contents(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse the shapes ``contents`` may take — a bare string, a
    single content dict, or a list mixing content dicts with loose
    parts — into a uniform list of ``{"role", "parts"}`` dicts.
    Loose parts (bare strings / part dicts without a role wrapper)
    are the SDK's "single user message" convenience form."""
    contents = request.get("contents")
    if contents is None:
        return []
    if isinstance(contents, str):
        return [{"role": "user", "parts": [contents]}]
    if isinstance(contents, dict):
        return [contents]
    if not isinstance(contents, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    loose_parts: list[Any] = []
    for item in contents:
        if isinstance(item, dict) and ("role" in item or "parts" in item):
            out.append(item)
        else:
            loose_parts.append(item)
    if loose_parts:
        out.append({"role": "user", "parts": loose_parts})
    return out


def _content_parts(content: dict[str, Any]) -> list[Any]:
    parts = content.get("parts")
    if isinstance(parts, (list, tuple)):
        return list(parts)
    if parts is None:
        return []
    return [parts]


def _extract_system_text(request: dict[str, Any]) -> str:
    """The shim flattens the model-bound ``system_instruction`` to a
    plain string, but Pattern-B callers may hand the structured
    shapes (a content dict or a parts list) — collapse them all."""
    raw = request.get("system_instruction")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return "".join(_part_text(p) for p in _content_parts(raw))
    if isinstance(raw, (list, tuple)):
        return "".join(_part_text(p) for p in raw)
    return ""


def _last_user_index(contents: list[dict[str, Any]]) -> Optional[int]:
    last: Optional[int] = None
    for i, content in enumerate(contents):
        # Role defaults to "user" in the API when omitted.
        if content.get("role", "user") == "user":
            last = i
    return last


def _current_user_text(request: dict[str, Any]) -> str:
    """Text parts of the *last* user-role content — "the current
    turn's user input". ``function_response`` parts (which ride on
    user-role contents in Gemini's API) are excluded — those count
    toward ``tool_result_tokens`` instead."""
    contents = _normalised_contents(request)
    idx = _last_user_index(contents)
    if idx is None:
        return ""
    return "".join(
        _part_text(p)
        for p in _content_parts(contents[idx])
        if not _is_function_response(p)
    )


def _function_response_text(part: dict[str, Any]) -> str:
    """Serialise one ``function_response`` part's payload. Gemini
    wraps tool output as ``{"function_response": {"name": ...,
    "response": {...}}}``; the response dict is what the model
    actually reads, so that's what we tokenise."""
    fr = part.get("function_response")
    if not isinstance(fr, dict):
        return ""
    response = fr.get("response")
    if isinstance(response, str):
        return response
    if response is None:
        return ""
    try:
        return json.dumps(response, default=str)
    except (TypeError, ValueError):
        return str(response)


def _tool_results_text(request: dict[str, Any]) -> str:
    parts: list[str] = []
    for content in _normalised_contents(request):
        for part in _content_parts(content):
            if _is_function_response(part):
                parts.append(_function_response_text(part))
    return "".join(parts)


def _memory_text(request: dict[str, Any]) -> str:
    """Every prior turn that isn't the current-turn user content and
    isn't a ``function_response`` part: prior model turns + prior
    user turns."""
    contents = _normalised_contents(request)
    idx = _last_user_index(contents)
    parts: list[str] = []
    for i, content in enumerate(contents):
        if i == idx:
            continue
        for part in _content_parts(content):
            if _is_function_response(part):
                continue
            parts.append(_part_text(part))
    return "".join(parts)


def _normalise_tools_for_tokenisation(
    tools: Any,
) -> list[dict[str, Any]]:
    """Gemini's tools array nests schemas under
    ``"function_declarations"``. For tokenisation we flatten to the
    inner declaration dicts so the bytes look closer to what the
    provider's tokeniser sees. Non-dict entries (the SDK accepts
    bare Python callables) are skipped — their schema isn't
    recoverable here, and the resulting undercount is already
    surfaced by the ``tool_schema_tokens`` estimation flag."""
    if not isinstance(tools, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        declarations = tool.get("function_declarations")
        if isinstance(declarations, (list, tuple)):
            out.extend(d for d in declarations if isinstance(d, dict))
            continue
        out.append(tool)
    return out


def _tool_names(tools: Any) -> tuple[str, ...]:
    names: list[str] = []
    for tool in _normalise_tools_for_tokenisation(tools):
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _tool_calls_in_response(response: Any) -> tuple[str, ...]:
    """Gemini surfaces tool calls as ``function_call`` parts on
    ``candidates[0].content.parts``."""
    candidates = response_candidates(response)
    if not candidates:
        return ()
    first = candidates[0]
    content = (
        first.get("content")
        if isinstance(first, dict)
        else getattr(first, "content", None)
    )
    if content is None:
        return ()
    parts = (
        content.get("parts")
        if isinstance(content, dict)
        else getattr(content, "parts", None)
    )
    if not parts:
        return ()
    out: list[str] = []
    for part in parts:
        fc = (
            part.get("function_call")
            if isinstance(part, dict)
            else getattr(part, "function_call", None)
        )
        if fc is None:
            continue
        name = fc.get("name") if isinstance(fc, dict) else getattr(fc, "name", None)
        if isinstance(name, str) and name:
            out.append(name)
    return tuple(out)


class GeminiTranslator:
    """Stateless Gemini translator; usage shape per
    ``generate_content``. Reads the shim-synthesised request dict
    (``model`` / ``contents`` / ``system_instruction`` / ``tools``)."""

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
            raise ValueError("GeminiTranslator.translate: model is required")

        usage = _PROVIDER_IMPL.map_usage(response)
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

        output_tokens = usage.output_tokens
        reasoning = usage.reasoning_tokens
        cache_read = usage.cache_read_tokens
        cache_creation = usage.cache_creation_tokens
        cache_status = usage.cache_status

        # Run lifecycle: consume the cache-resource creation marker.
        # The placer created a CachedContent resource immediately
        # before this call, so the cached portion the response
        # reports is the one-time write, not a read. (A concurrent
        # same-run call could consume a sibling's marker — same
        # tolerated race family as the stable-prefix update.)
        created = bool(
            getattr(run_state, "pending_cache_resource_creation", False)
        )
        run_state.pending_cache_resource_creation = False
        if created and cache_read:
            cache_creation, cache_read = cache_read, 0
            cache_status = "miss"

        # Run lifecycle: consume any pending tag_retrieval marker — see
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
            cache_creation_tokens=cache_creation,
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

        # framework metadata contract — Pattern-C metadata pass-through
        # (mirror of the Anthropic translator's handling).
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
            cache_status=cache_status,
            parent_run_id=parent_run_id,
            sequence=sequence,
            estimation_flags=tuple(flags),
            metadata=metadata,
        )
