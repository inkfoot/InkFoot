"""OpenAI Responses translator tests.

Two layers:

* Golden fixtures under ``tests/fixtures/openai_responses`` — real
  wire shapes (text, tool calls, reasoning, image input, multi-item
  input, the streamed terminal snapshot) round-trip through the
  translator and must match the expected ``NeutralCall`` fields.
* Unit tests for the Responses-specific mappings: ``instructions``
  as the system block, ``input`` string-vs-items, flat tools,
  ``function_call_output`` tool results, the renamed usage
  counters, and the flag-don't-crash handling of unknown response
  keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from inkfoot.normalise.openai_responses import (
    OpenAIResponsesTranslator,
    map_usage,
    unknown_response_keys,
)
from inkfoot.run import InMemoryRunState

_MODEL = "gpt-4o"

_FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "openai_responses"
)
_FIXTURE_NAMES = [
    "text_output",
    "tool_call_output",
    "reasoning_output",
    "image_input",
    "multi_item_input",
    "streamed_terminal",
]


def _load(name: str) -> dict:
    return json.loads(
        (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )


def _translate(
    request: dict,
    response,
    run_state: InMemoryRunState | None = None,
):
    return OpenAIResponsesTranslator().translate(
        request=request,
        response=response,
        run_state=run_state or InMemoryRunState(),
        started_at=1_000,
        ended_at=2_000,
        sequence=1,
    )


def _response(
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    output: list | None = None,
) -> dict:
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }
    return {
        "id": "resp_test",
        "object": "response",
        "status": "completed",
        "model": "gpt-4o-2024-08-06",
        "output": output
        or [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ack"}],
            }
        ],
        "usage": usage,
    }


# ----------------------------------------------------------------------
# Golden fixtures
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_neutral_call_matches_the_golden_fixture(name):
    fixture = _load(name)
    expected = fixture["expected"]

    neutral_call = _translate(fixture["request"], fixture["response"])

    assert neutral_call.provider == expected["provider"]
    assert neutral_call.model == expected["model"]
    for field_name, value in expected["ledger"].items():
        assert getattr(neutral_call.ledger, field_name) == value, field_name
    assert neutral_call.cache_status == expected["cache_status"]
    assert list(neutral_call.tools_offered) == expected["tools_offered"]
    assert list(neutral_call.tools_called) == expected["tools_called"]
    if expected["priced"]:
        assert neutral_call.estimated_nanodollars is not None
        assert neutral_call.estimated_nanodollars > 0
    for field_name in expected["expect_positive"]:
        assert getattr(neutral_call.ledger, field_name) > 0, field_name
    for field_name in expected["expect_zero"]:
        assert getattr(neutral_call.ledger, field_name) == 0, field_name


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_fixture_shapes_carry_no_unknown_key_flags(name):
    fixture = _load(name)
    neutral_call = _translate(fixture["request"], fixture["response"])
    assert not [
        flag
        for flag in neutral_call.estimation_flags
        if flag.startswith("responses_shape_unknown:")
    ]


# ----------------------------------------------------------------------
# Unknown-shape handling: flag, never crash
# ----------------------------------------------------------------------


def test_unknown_top_level_response_key_is_flagged_not_fatal():
    response = _response()
    response["brand_new_field"] = {"shape": "unmapped"}
    neutral_call = _translate(
        {"model": _MODEL, "input": "x"}, response
    )
    assert (
        "responses_shape_unknown:brand_new_field"
        in neutral_call.estimation_flags
    )
    # Translation proceeded: usage still mapped.
    assert neutral_call.ledger.output_tokens == 5


def test_multiple_unknown_keys_flag_sorted_and_translate_anyway():
    response = _response()
    response["zeta"] = 1
    response["alpha"] = 2
    neutral_call = _translate({"model": _MODEL, "input": "x"}, response)
    flags = [
        f
        for f in neutral_call.estimation_flags
        if f.startswith("responses_shape_unknown:")
    ]
    assert flags == [
        "responses_shape_unknown:alpha",
        "responses_shape_unknown:zeta",
    ]


def test_unknown_keys_helper_returns_empty_for_attr_only_objects():
    # Attribute-only duck types can't be enumerated — no flags, no
    # crash.
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        output=[],
    )
    assert unknown_response_keys(response) == ()


# ----------------------------------------------------------------------
# Usage mapping
# ----------------------------------------------------------------------


def test_input_and_output_tokens_map_to_token_usage():
    usage = map_usage(_response(input_tokens=120, output_tokens=34))
    assert usage.input_tokens == 120
    assert usage.output_tokens == 34


def test_cached_tokens_map_to_cache_read_tokens():
    call = _translate(
        {"model": _MODEL, "input": "x"},
        _response(input_tokens=100, output_tokens=10, cached_tokens=64),
    )
    assert call.ledger.cache_read_tokens == 64
    assert call.cache_status == "hit"


def test_cache_creation_tokens_always_zero():
    call = _translate(
        {"model": _MODEL, "input": "x"},
        _response(cached_tokens=64),
    )
    assert call.ledger.cache_creation_tokens == 0


def test_reasoning_tokens_land_in_reasoning_tokens():
    call = _translate(
        {"model": "o1", "input": "x"},
        _response(output_tokens=50, reasoning_tokens=33),
    )
    assert call.ledger.reasoning_tokens == 33


@pytest.mark.parametrize("cached,expected", [(0, "n/a"), (50, "hit")])
def test_cache_status_only_hit_or_na(cached, expected):
    call = _translate(
        {"model": _MODEL, "input": "x"},
        _response(cached_tokens=cached),
    )
    assert call.cache_status == expected


def test_map_usage_tolerates_attr_style_sdk_objects():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=15,
            output_tokens=4,
            total_tokens=19,
            input_tokens_details=SimpleNamespace(cached_tokens=8),
            output_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
    )
    usage = map_usage(response)
    assert usage.input_tokens == 15
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 8
    assert usage.reasoning_tokens == 2
    assert usage.cache_status == "hit"


def test_map_usage_clamps_missing_and_negative_counters():
    usage = map_usage({"usage": {"input_tokens": -3}})
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert map_usage(None).input_tokens == 0
    assert map_usage({}).cache_status == "n/a"


# ----------------------------------------------------------------------
# Request mapping
# ----------------------------------------------------------------------


def test_instructions_land_in_the_system_block():
    call = _translate(
        {
            "model": _MODEL,
            "instructions": "You are a careful planner.",
            "input": "x",
        },
        _response(),
    )
    assert call.ledger.system_static_tokens > 0
    assert call.ledger.system_dynamic_tokens == 0


def test_system_role_input_items_fold_into_the_system_block():
    call = _translate(
        {
            "model": _MODEL,
            "input": [
                {"role": "system", "content": "House rules apply."},
                {"role": "user", "content": "x"},
            ],
        },
        _response(),
    )
    assert call.ledger.system_static_tokens > 0


def test_string_input_is_the_current_user_turn():
    call = _translate(
        {"model": _MODEL, "input": "What is the capital of France?"},
        _response(),
    )
    assert call.ledger.user_input_tokens > 0
    assert call.ledger.memory_tokens == 0


def test_prior_turns_land_in_memory_not_user_input():
    state = InMemoryRunState()
    call = _translate(
        {
            "model": _MODEL,
            "input": [
                {"role": "user", "content": "First question, quite long."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "First answer."}
                    ],
                },
                {"role": "user", "content": "Follow-up?"},
            ],
        },
        _response(),
        state,
    )
    assert call.ledger.memory_tokens > 0
    assert call.ledger.user_input_tokens > 0


def test_function_call_output_items_land_in_tool_result_tokens():
    call = _translate(
        {
            "model": _MODEL,
            "input": [
                {"role": "user", "content": "x"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "wx",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "RESULT_BODY",
                },
            ],
        },
        _response(),
    )
    assert call.ledger.tool_result_tokens > 0


def test_empty_and_missing_input_translate_to_zero_request_side():
    call = _translate({"model": _MODEL}, _response())
    assert call.ledger.user_input_tokens == 0
    assert call.ledger.memory_tokens == 0
    assert call.ledger.tool_result_tokens == 0


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


def test_flat_function_tools_offered_by_name():
    call = _translate(
        {
            "model": _MODEL,
            "input": "x",
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            ],
        },
        _response(),
    )
    assert call.tools_offered == ("get_weather", "lookup")
    assert call.ledger.tool_schema_tokens > 0


def test_builtin_tools_offered_by_type():
    call = _translate(
        {
            "model": _MODEL,
            "input": "x",
            "tools": [{"type": "web_search_preview"}],
        },
        _response(),
    )
    assert call.tools_offered == ("web_search_preview",)


def test_multi_item_function_calls_all_collected():
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "name": "alpha", "call_id": "c1"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "between"}],
        },
        {"type": "function_call", "name": "beta", "call_id": "c2"},
    ]
    call = _translate(
        {"model": _MODEL, "input": "x"},
        _response(output=output),
    )
    assert call.tools_called == ("alpha", "beta")


# ----------------------------------------------------------------------
# Required fields
# ----------------------------------------------------------------------


def test_missing_model_raises():
    with pytest.raises(ValueError, match="model"):
        _translate({"input": "x"}, _response())
