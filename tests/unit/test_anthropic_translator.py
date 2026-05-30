"""Anthropic translator tests.

Covers:
- Direct fields (output, cache_read, cache_creation, reasoning)
  populate from ``response.usage`` exactly.
- Tokenised fields (system_static / system_dynamic, user_input,
  tool_schema, tool_result, memory) populate from the request.
- Stable-prefix detector tracks across a 4-turn run and the
  static count is monotonically non-increasing.
- Estimation flags appear when the fallback tokeniser is used.
- ``NeutralCall.tools_offered`` lists the tools in the request;
  ``tools_called`` lists tools the response invoked.
- ``cache_status`` is correctly classified.
- Validation invariant holds on a 4-turn fixture run.
"""

from __future__ import annotations

import sys

import pytest

from inkfoot.ledger import validate_against_usage
from inkfoot.normalise.anthropic import AnthropicTranslator
from inkfoot.run import InMemoryRunState


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


_MODEL = "claude-sonnet-4-6"


def _response(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    thinking_tokens: int | None = None,
    content: list | None = None,
) -> dict:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if thinking_tokens is not None:
        usage["thinking_tokens"] = thinking_tokens
    return {
        "usage": usage,
        "content": content or [{"type": "text", "text": "ack"}],
    }


# ----------------------------------------------------------------------
# Direct-from-response fields
# ----------------------------------------------------------------------


def test_output_tokens_read_directly_from_usage() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]},
        response=_response(input_tokens=10, output_tokens=42),
        run_state=InMemoryRunState(),
        started_at=1,
        ended_at=2,
    )
    assert call.ledger.output_tokens == 42


def test_cache_fields_populate_from_usage() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]},
        response=_response(
            input_tokens=10,
            output_tokens=5,
            cache_read=200,
            cache_creation=50,
        ),
        run_state=InMemoryRunState(),
        started_at=1,
        ended_at=2,
    )
    assert call.ledger.cache_read_tokens == 200
    assert call.ledger.cache_creation_tokens == 50


def test_reasoning_tokens_from_usage_thinking_tokens_field() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]},
        response=_response(
            input_tokens=10, output_tokens=5, thinking_tokens=77
        ),
        run_state=InMemoryRunState(),
        started_at=1,
        ended_at=2,
    )
    assert call.ledger.reasoning_tokens == 77


def test_reasoning_tokens_from_thinking_blocks() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]},
        response=_response(
            input_tokens=10,
            output_tokens=5,
            content=[
                {"type": "thinking", "tokens": 30},
                {"type": "thinking", "tokens": 20},
                {"type": "text", "text": "answer"},
            ],
        ),
        run_state=InMemoryRunState(),
        started_at=1,
        ended_at=2,
    )
    assert call.ledger.reasoning_tokens == 50


# ----------------------------------------------------------------------
# Stable-prefix detection across a 4-turn run
# ----------------------------------------------------------------------


def test_system_static_is_monotonically_non_increasing_across_turns() -> None:
    t = AnthropicTranslator()
    state = InMemoryRunState()
    systems = [
        "You are an agent. Now: 2026-05-25T10:00",
        "You are an agent. Now: 2026-05-25T10:01",
        "You are an agent. Now: 2026-05-25T11",  # shorter common
        "You are an agent. Now: 2026-05-25T11:00",  # same as prev
    ]
    static_counts: list[int] = []
    for i, system in enumerate(systems):
        call = t.translate(
            request={
                "model": _MODEL,
                "system": system,
                "messages": [{"role": "user", "content": "x"}],
            },
            response=_response(input_tokens=1, output_tokens=1),
            run_state=state,
            started_at=i,
            ended_at=i + 1,
        )
        static_counts.append(call.ledger.system_static_tokens)
    for i in range(1, len(static_counts)):
        assert static_counts[i] <= static_counts[i - 1], (
            f"static count grew between turn {i-1} and {i}: {static_counts}"
        )


def test_system_dynamic_populates_with_the_drift() -> None:
    t = AnthropicTranslator()
    state = InMemoryRunState()
    # First call seeds; second call has drift in the tail.
    t.translate(
        request={
            "model": _MODEL,
            "system": "You are an agent. Now: A",
            "messages": [{"role": "user", "content": "x"}],
        },
        response=_response(input_tokens=1, output_tokens=1),
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    call2 = t.translate(
        request={
            "model": _MODEL,
            "system": "You are an agent. Now: B",
            "messages": [{"role": "user", "content": "x"}],
        },
        response=_response(input_tokens=1, output_tokens=1),
        run_state=state,
        started_at=2,
        ended_at=3,
    )
    # The stable prefix shortened; the diverging tail goes into
    # system_dynamic_tokens.
    assert call2.ledger.system_dynamic_tokens > 0


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


def test_tools_offered_lifted_from_request() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {"name": "get_weather", "description": "..."},
                {"name": "lookup", "description": "..."},
            ],
        },
        response=_response(input_tokens=1, output_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_offered == ("get_weather", "lookup")


def test_tools_called_lifted_from_response_content() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "get_weather"}],
        },
        response={
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "name": "get_weather"}],
        },
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.tools_called == ("get_weather",)


def test_tool_schema_tokens_populated_when_tools_present() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Look up weather for a city.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        },
        response=_response(input_tokens=10, output_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.tool_schema_tokens > 0


def test_tool_result_tokens_sum_across_messages() -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "messages": [
                {"role": "user", "content": "do work"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "x"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "RESULT_BODY"},
                    ],
                },
            ],
        },
        response=_response(input_tokens=10, output_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.ledger.tool_result_tokens > 0


# ----------------------------------------------------------------------
# cache_status classification
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cache_read,cache_create,expected",
    [
        (0, 0, "n/a"),
        (50, 0, "hit"),
        (0, 30, "miss"),
        (50, 30, "partial"),
    ],
)
def test_cache_status_classification(
    cache_read: int, cache_create: int, expected: str
) -> None:
    t = AnthropicTranslator()
    call = t.translate(
        request={"model": _MODEL, "messages": [{"role": "user", "content": "x"}]},
        response=_response(
            input_tokens=1,
            output_tokens=1,
            cache_read=cache_read,
            cache_creation=cache_create,
        ),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert call.cache_status == expected


# ----------------------------------------------------------------------
# Estimation flags
# ----------------------------------------------------------------------


def test_estimation_flags_surface_when_anthropic_fallback_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the fallback path. Every tokenised field should pick up
    the estimated flag."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    t = AnthropicTranslator()
    call = t.translate(
        request={
            "model": _MODEL,
            "system": "You are an agent.",
            "messages": [{"role": "user", "content": "hello"}],
        },
        response=_response(input_tokens=10, output_tokens=1),
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    # system_static_tokens is the first tokenised field that has
    # non-empty content on the first call.
    assert "system_static_tokens" in call.estimation_flags
    assert "user_input_tokens" in call.estimation_flags


# ----------------------------------------------------------------------
# Validation invariant — non-circular, independent expected total
# ----------------------------------------------------------------------


def _independent_token_count(text: str) -> int:
    """Compute the tokeniser count for ``text`` directly, without
    going through the translator. The non-circular test below uses
    this to compute the expected ``input_total`` from request
    content alone, then feeds the number into ``raw_input``."""
    import tiktoken  # local import — keeps the module-level imports tidy

    enc = tiktoken.get_encoding("o200k_base")
    return len(enc.encode(text))


def test_input_total_matches_independently_tokenised_request_no_cache() -> None:
    """Build a request whose tokenised content is computable
    independently. Assert input_total matches that count (within
    the 2% bar). The fixture has zero cache fields so the
    "total billed input" simplifies to ``usage.input_tokens``.
    """
    t = AnthropicTranslator()
    state = InMemoryRunState()

    system = "You are a helpful agent."
    user = "Find weather for NYC."
    request = {
        "model": _MODEL,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    expected_structural = _independent_token_count(
        system
    ) + _independent_token_count(user)

    call = t.translate(
        request=request,
        response=_response(
            input_tokens=expected_structural, output_tokens=1
        ),
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    # input_total should match the independent count exactly when
    # the same tokeniser is used end-to-end.
    assert call.ledger.input_total == expected_structural
    # validate against the independent number — within 2% bar.
    validate_against_usage(
        call.ledger, raw_input=expected_structural, raw_output=1
    )


def test_input_total_matches_request_under_cache_hit_regression() -> None:
    """Finding #1 regression: under the old buggy semantics,
    input_total would have been inflated by cache_read_tokens +
    cache_creation_tokens, because the structural categories *and*
    the cache fields both got summed. Under the new semantics
    cache_* are billing overlays — not summed into input_total —
    so the cache-hit case has the same structural total as the
    no-cache case.

    Concretely: 5000-token system block, 4800 served from cache.
    The structural sum (system_static + dynamic) should equal the
    tokenised system content, *not* 5000 + 4800.
    """
    t = AnthropicTranslator()
    state = InMemoryRunState()

    # A system block we can tokenise independently.
    system = "Inkfoot agent header. " * 200  # ~1500 tokens-ish
    request = {
        "model": _MODEL,
        "system": system,
        "messages": [{"role": "user", "content": "go"}],
    }

    independent_system = _independent_token_count(system)
    independent_user = _independent_token_count("go")
    expected_structural = independent_system + independent_user

    # Provider reports most of the system block served from cache.
    cache_read = max(0, independent_system - 50)
    fresh = expected_structural - cache_read  # the fresh portion
    call = t.translate(
        request=request,
        response=_response(
            input_tokens=fresh,
            output_tokens=1,
            cache_read=cache_read,
        ),
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    # The structural sum equals the request-body tokenisation —
    # cache_read is NOT added on top.
    assert call.ledger.input_total == expected_structural
    # And the cache field is populated separately.
    assert call.ledger.cache_read_tokens == cache_read

    # The total billed input the provider reports equals
    # fresh + cache_read = expected_structural by construction.
    total_billed_input = fresh + cache_read
    validate_against_usage(
        call.ledger, raw_input=total_billed_input, raw_output=1
    )


def test_four_turn_run_invariant_holds_with_independent_totals() -> None:
    """End-to-end across a 4-turn run. Each turn's expected
    ``input_total`` is computed independently from the request
    content; we feed that into the synthetic response and validate.
    No circular fixture."""
    t = AnthropicTranslator()
    state = InMemoryRunState()

    system = "You are a helpful weather agent."
    user_turn_1 = "Look up NYC weather."
    tool_results = [
        "NYC: 60F sunny",
        "Forecast: 65/55 high/low",
        "History: 14d trailing avg",
        "Tail data",
    ]

    messages: list[dict] = [
        {"role": "user", "content": user_turn_1},
    ]
    # Tool array shape — content matters for token counting.
    tools_array = [{"name": "wx", "description": "weather"}]
    import json as _json

    tools_serialised = _json.dumps(
        tools_array, sort_keys=True, separators=(",", ":")
    )

    for turn, result in enumerate(tool_results):
        messages.append(
            {"role": "assistant", "content": [{"type": "tool_use", "name": "wx"}]}
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": result},
                ],
            }
        )

        request = {
            "model": _MODEL,
            "system": system,
            "messages": list(messages),
            "tools": tools_array,
        }

        # Recompute the expected structural sum independently from
        # the request content.
        expected = _independent_token_count(system)  # system_static (stable)
        # system_dynamic = 0 because system text never changes here
        expected += _independent_token_count(user_turn_1) if turn == 0 else 0
        # When turn > 0, the last user message is the most recent
        # tool_result (anthropic tool_result rides on user role).
        # The translator excludes tool_result blocks from
        # user_input_tokens; on those turns user_input_tokens is 0.
        expected += _independent_token_count(tools_serialised)
        # tool_results accumulate.
        for previous_result in tool_results[: turn + 1]:
            expected += _independent_token_count(previous_result)
        # memory: every prior assistant + user turn that isn't the
        # current user message and isn't a tool result. The
        # assistant tool_use blocks have an empty text body and
        # contribute 0 from this translator. The earlier user
        # text ("Look up NYC weather.") goes to memory on turn > 0.
        if turn > 0:
            expected += _independent_token_count(user_turn_1)

        call = t.translate(
            request=request,
            response=_response(input_tokens=expected, output_tokens=1),
            run_state=state,
            started_at=turn,
            ended_at=turn + 1,
        )
        # The actual ledger total should match what we independently
        # computed within the 2% bar.
        validate_against_usage(
            call.ledger,
            raw_input=expected,
            raw_output=1,
            tolerance=0.02,
        )
