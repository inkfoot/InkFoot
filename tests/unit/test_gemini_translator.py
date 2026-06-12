"""Gemini translator tests.

Mirrors the Anthropic/OpenAI translator tests with Gemini's shapes:
- ``usage_metadata.cached_content_token_count`` → ``cache_read_tokens``
  (re-attributed to ``cache_creation_tokens`` + ``miss`` when the run
  state carries the resource-creation marker)
- Thinking from ``thoughts_token_count`` folds into output and lands
  in ``reasoning_tokens``
- Messages in ``contents`` with ``parts``; tool results as
  ``function_response`` parts; tools under ``function_declarations``
- Tool calls as ``function_call`` parts on ``candidates[0]``
"""

from __future__ import annotations

import pytest

from inkfoot.normalise.gemini import GeminiTranslator
from inkfoot.run import InMemoryRunState


_MODEL = "gemini-1.5-pro"


def _response(
    *,
    prompt: int,
    candidates: int,
    cached: int = 0,
    thoughts: int = 0,
    tool_call_names: list[str] | None = None,
) -> dict:
    usage = {
        "prompt_token_count": prompt,
        "candidates_token_count": candidates,
    }
    if cached:
        usage["cached_content_token_count"] = cached
    if thoughts:
        usage["thoughts_token_count"] = thoughts
    parts: list[dict] = [{"text": "ack"}]
    if tool_call_names:
        parts = [
            {"function_call": {"name": name, "args": {}}}
            for name in tool_call_names
        ]
    return {
        "usage_metadata": usage,
        "candidates": [{"content": {"role": "model", "parts": parts}}],
    }


def _request(contents="x", **extra) -> dict:
    return {"model": _MODEL, "contents": contents, **extra}


# ----------------------------------------------------------------------
# Gemini-specific usage mappings
# ----------------------------------------------------------------------


def test_cached_tokens_map_to_cache_read_tokens() -> None:
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(prompt=100, candidates=10, cached=40),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.cache_read_tokens == 40
    assert call.ledger.cache_creation_tokens == 0
    assert call.cache_status == "hit"


def test_candidates_plus_thoughts_map_to_output_tokens() -> None:
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(prompt=10, candidates=50, thoughts=27),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.output_tokens == 77
    assert call.ledger.reasoning_tokens == 27


def test_priced_model_gets_a_nanodollar_estimate() -> None:
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(prompt=10, candidates=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.estimated_nanodollars is not None


# ----------------------------------------------------------------------
# Cache-resource creation re-attribution
# ----------------------------------------------------------------------


def test_creation_marker_reattributes_cached_count_as_write() -> None:
    run_state = InMemoryRunState()
    run_state.pending_cache_resource_creation = True
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(prompt=300, candidates=5, cached=256),
        run_state=run_state,
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.cache_creation_tokens == 256
    assert call.ledger.cache_read_tokens == 0
    assert call.cache_status == "miss"
    # One-time write — the marker is consumed.
    assert run_state.pending_cache_resource_creation is False


def test_creation_marker_is_consumed_even_without_cached_tokens() -> None:
    run_state = InMemoryRunState()
    run_state.pending_cache_resource_creation = True
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(prompt=10, candidates=5),
        run_state=run_state,
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.cache_creation_tokens == 0
    assert call.cache_status == "n/a"
    assert run_state.pending_cache_resource_creation is False


def test_call_after_creation_reads_as_hit() -> None:
    run_state = InMemoryRunState()
    run_state.pending_cache_resource_creation = True
    t = GeminiTranslator()
    first = t.translate(
        request=_request(),
        response=_response(prompt=300, candidates=5, cached=256),
        run_state=run_state,
        started_at=0,
        ended_at=1,
    )
    second = t.translate(
        request=_request(),
        response=_response(prompt=300, candidates=5, cached=256),
        run_state=run_state,
        started_at=2,
        ended_at=3,
    )
    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    assert second.ledger.cache_read_tokens == 256
    assert second.ledger.cache_creation_tokens == 0


# ----------------------------------------------------------------------
# Request-side attribution
# ----------------------------------------------------------------------


def test_bare_string_contents_count_as_user_input() -> None:
    call = GeminiTranslator().translate(
        request=_request("what failed in the deploy?"),
        response=_response(prompt=10, candidates=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.user_input_tokens > 0


def test_system_instruction_lands_in_system_static_on_first_call() -> None:
    call = GeminiTranslator().translate(
        request=_request(
            system_instruction="You are a helpful agent."
        ),
        response=_response(prompt=20, candidates=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.system_static_tokens > 0
    assert call.ledger.system_dynamic_tokens == 0


def test_structured_system_instruction_is_flattened() -> None:
    call = GeminiTranslator().translate(
        request=_request(
            system_instruction={"parts": [{"text": "You are an agent."}]}
        ),
        response=_response(prompt=20, candidates=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.system_static_tokens > 0


def test_prior_turns_count_as_memory_not_user_input() -> None:
    contents = [
        {"role": "user", "parts": ["first question"]},
        {"role": "model", "parts": [{"text": "first answer"}]},
        {"role": "user", "parts": ["second question"]},
    ]
    call = GeminiTranslator().translate(
        request=_request(contents),
        response=_response(prompt=30, candidates=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.memory_tokens > 0
    assert call.ledger.user_input_tokens > 0


def test_function_response_parts_count_as_tool_results() -> None:
    contents = [
        {"role": "user", "parts": ["check the weather"]},
        {
            "role": "model",
            "parts": [{"function_call": {"name": "wx", "args": {}}}],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "wx",
                        "response": {"temp_c": 21, "sky": "RESULT_BODY"},
                    }
                }
            ],
        },
    ]
    call = GeminiTranslator().translate(
        request=_request(contents),
        response=_response(prompt=30, candidates=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.tool_result_tokens > 0
    # The function_response rode on the last user content — it must
    # not double-count as current-turn user input.
    assert call.ledger.user_input_tokens == 0


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


def test_tools_offered_lifted_from_function_declarations() -> None:
    call = GeminiTranslator().translate(
        request=_request(
            tools=[
                {
                    "function_declarations": [
                        {"name": "get_weather", "description": "..."},
                        {"name": "lookup", "description": "..."},
                    ]
                }
            ]
        ),
        response=_response(prompt=10, candidates=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_offered == ("get_weather", "lookup")
    assert call.ledger.tool_schema_tokens > 0


def test_tool_calls_lifted_from_function_call_parts() -> None:
    call = GeminiTranslator().translate(
        request=_request(),
        response=_response(
            prompt=10,
            candidates=5,
            tool_call_names=["get_weather", "lookup"],
        ),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_called == ("get_weather", "lookup")


# ----------------------------------------------------------------------
# Required fields
# ----------------------------------------------------------------------


def test_missing_model_raises() -> None:
    with pytest.raises(ValueError, match="model"):
        GeminiTranslator().translate(
            request={"contents": "x"},
            response=_response(prompt=10, candidates=1),
            run_state=InMemoryRunState(),
            started_at=0,
            ended_at=1,
        )
