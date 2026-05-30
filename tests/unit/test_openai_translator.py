"""OpenAI translator tests.

Mirrors the Anthropic translator tests with OpenAI's response shape:
- ``usage.prompt_tokens_details.cached_tokens`` → ``cache_read_tokens``
- ``cache_creation_tokens`` always 0 (OpenAI doesn't bill writes)
- Reasoning from ``usage.completion_tokens_details.reasoning_tokens``
- Tools as ``[{"type": "function", "function": {...}}]``
- Tool calls under ``choices[0].message.tool_calls``
"""

from __future__ import annotations

import pytest

from inkfoot.normalise.openai import OpenAITranslator
from inkfoot.run import InMemoryRunState


_MODEL = "gpt-4o"


def _response(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    tool_call_names: list[str] | None = None,
) -> dict:
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if reasoning_tokens:
        usage["completion_tokens_details"] = {
            "reasoning_tokens": reasoning_tokens
        }
    message: dict = {"role": "assistant", "content": "ack"}
    if tool_call_names:
        message["tool_calls"] = [
            {"id": f"call_{i}", "function": {"name": name}}
            for i, name in enumerate(tool_call_names)
        ]
    return {
        "usage": usage,
        "choices": [{"message": message}],
    }


# ----------------------------------------------------------------------
# OpenAI-specific mappings
# ----------------------------------------------------------------------


def test_cached_tokens_map_to_cache_read_tokens() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "x"}]},
        response=_response(prompt_tokens=100, completion_tokens=10, cached_tokens=40),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.cache_read_tokens == 40


def test_openai_cache_creation_tokens_always_zero() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "x"}]},
        response=_response(prompt_tokens=100, completion_tokens=10, cached_tokens=40),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.cache_creation_tokens == 0


def test_completion_tokens_map_to_output_tokens() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "x"}]},
        response=_response(prompt_tokens=10, completion_tokens=77),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.output_tokens == 77


def test_reasoning_tokens_lift_from_o_series_details() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={"model": "o1", "messages": [{"role": "user", "content": "x"}]},
        response=_response(
            prompt_tokens=10, completion_tokens=50, reasoning_tokens=33
        ),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.reasoning_tokens == 33


def test_system_block_lifted_from_system_role_message() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [
                {"role": "system", "content": "You are a helpful agent."},
                {"role": "user", "content": "hello"},
            ],
        },
        response=_response(prompt_tokens=20, completion_tokens=5),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    # First call: all of system goes to static.
    assert call.ledger.system_static_tokens > 0
    assert call.ledger.system_dynamic_tokens == 0


def test_developer_role_treated_as_system() -> None:
    """Newer OpenAI models use ``role="developer"`` in place of
    ``"system"``; the translator should treat them equivalently."""
    t = OpenAITranslator()
    call = t.translate(
        request={
            "model": "o1",
            "messages": [
                {"role": "developer", "content": "You are an agent."},
                {"role": "user", "content": "x"},
            ],
        },
        response=_response(prompt_tokens=20, completion_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.system_static_tokens > 0


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


def test_tools_offered_lifted_from_request() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_weather", "description": "..."},
                },
                {
                    "type": "function",
                    "function": {"name": "lookup", "description": "..."},
                },
            ],
        },
        response=_response(prompt_tokens=10, completion_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_offered == ("get_weather", "lookup")


def test_tool_calls_lifted_from_response() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "x"}],
        },
        response=_response(
            prompt_tokens=10,
            completion_tokens=5,
            tool_call_names=["get_weather", "lookup"],
        ),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_called == ("get_weather", "lookup")


def test_tool_result_role_contributes_to_tool_result_tokens() -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [
                {"role": "user", "content": "x"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "1", "function": {"name": "wx"}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "1",
                    "content": "RESULT_BODY",
                },
            ],
        },
        response=_response(prompt_tokens=10, completion_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.tool_result_tokens > 0


# ----------------------------------------------------------------------
# cache_status
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cached,expected", [(0, "n/a"), (50, "hit")]
)
def test_cache_status_only_hit_or_na_for_openai(
    cached: int, expected: str
) -> None:
    t = OpenAITranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "x"}]},
        response=_response(
            prompt_tokens=10, completion_tokens=1, cached_tokens=cached
        ),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.cache_status == expected


# ----------------------------------------------------------------------
# Required fields
# ----------------------------------------------------------------------


def test_missing_model_raises() -> None:
    t = OpenAITranslator()
    with pytest.raises(ValueError, match="model"):
        t.translate(
            request={"messages": [{"role": "user", "content": "x"}]},
            response=_response(prompt_tokens=10, completion_tokens=1),
            run_state=InMemoryRunState(),
            started_at=0,
            ended_at=1,
        )
