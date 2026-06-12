"""Golden-fixture tests for the LangChain attribution recipe.

The fixtures under ``tests/fixtures/langchain`` mirror the
LangChain-normalised result shapes the major chat integrations
produce — Anthropic, OpenAI (Chat Completions and Responses), Azure
OpenAI, Gemini, and Bedrock — including the cache-token detail keys
and the legacy ``token_usage`` fallback shape. Everything here is
duck-typed dicts so the recipe stays testable without
``langchain_core``; one test at the end proves real ``BaseMessage``
objects translate identically to their dict twins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.normalise.langchain import (
    USAGE_METADATA_MISSING_FLAG,
    LangChainTranslator,
    map_provider,
    summarise_response,
    usage_overlay,
)
from inkfoot.run import InMemoryRunState

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "langchain"
_FIXTURE_NAMES = [
    "anthropic",
    "openai_chat",
    "openai_responses",
    "azure",
    "gemini",
    "bedrock",
]


def _load(name: str) -> dict:
    return json.loads(
        (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )


def _translate(fixture: dict, run_state: InMemoryRunState | None = None):
    request_fix = fixture["request"]
    model = request_fix["model"]
    request = {
        "provider": map_provider(request_fix.get("raw_provider"), model),
        "model": model,
        "messages": request_fix["messages"],
        "tools": request_fix.get("tools") or [],
        "metadata": {"captured_by": "langchain_handler"},
    }
    return LangChainTranslator().translate(
        request=request,
        response=fixture["response"],
        run_state=run_state or InMemoryRunState(),
        started_at=1_000,
        ended_at=2_000,
        sequence=1,
    )


# ----------------------------------------------------------------------
# Golden fixtures: one per provider integration
# ----------------------------------------------------------------------


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_neutral_call_matches_the_golden_fixture(name):
    fixture = _load(name)
    expected = fixture["expected"]

    neutral_call = _translate(fixture)

    assert neutral_call.provider == expected["provider"]
    assert neutral_call.model == expected["model"]
    for field_name, value in expected["ledger"].items():
        assert getattr(neutral_call.ledger, field_name) == value, field_name
    assert neutral_call.cache_status == expected["cache_status"]
    assert list(neutral_call.tools_offered) == expected["tools_offered"]
    assert list(neutral_call.tools_called) == expected["tools_called"]
    assert USAGE_METADATA_MISSING_FLAG not in neutral_call.estimation_flags
    if expected["priced"]:
        assert neutral_call.estimated_nanodollars is not None
        assert neutral_call.estimated_nanodollars > 0
    for field_name in expected["expect_positive"]:
        assert getattr(neutral_call.ledger, field_name) > 0, field_name
    for field_name in expected["expect_zero"]:
        assert getattr(neutral_call.ledger, field_name) == 0, field_name
    assert neutral_call.metadata["captured_by"] == "langchain_handler"


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_response_id_extraction_matches_the_golden_fixture(name):
    fixture = _load(name)
    summary = summarise_response(fixture["response"])
    assert summary.response_id == fixture["expected"]["response_id"]


# ----------------------------------------------------------------------
# Degradation paths
# ----------------------------------------------------------------------


def test_missing_usage_metadata_flags_the_event_but_still_emits():
    response = {
        "generations": [
            [{"message": {"type": "ai", "content": "no usage here"}}]
        ],
        "llm_output": {},
    }
    neutral_call = LangChainTranslator().translate(
        request={
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "messages": [{"type": "human", "content": "hello there"}],
            "tools": [],
        },
        response=response,
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    assert USAGE_METADATA_MISSING_FLAG in neutral_call.estimation_flags
    assert neutral_call.ledger.output_tokens == 0
    assert neutral_call.ledger.cache_read_tokens == 0
    assert neutral_call.cache_status == "n/a"
    # The request side is still attributed from the message list.
    assert neutral_call.ledger.user_input_tokens > 0


def test_summarise_response_tolerates_empty_and_malformed_shapes():
    assert summarise_response(None).usage_metadata is None
    assert summarise_response({}).usage_metadata is None
    assert summarise_response({"generations": []}).usage_metadata is None

    summary = summarise_response({"generations": [[{}]]})
    assert summary.usage_metadata is None
    assert summary.response_id is None
    assert summary.model_name is None
    assert summary.tool_calls == ()


def test_token_usage_fallback_reads_llm_output_too():
    response = {
        "generations": [
            [{"message": {"type": "ai", "content": "legacy shape"}}]
        ],
        "llm_output": {
            "token_usage": {"prompt_tokens": 90, "completion_tokens": 9},
            "model_name": "gpt-4o",
        },
    }
    summary = summarise_response(response)
    assert summary.usage_metadata == {"input_tokens": 90, "output_tokens": 9}
    assert summary.model_name == "gpt-4o"
    overlay = usage_overlay(summary.usage_metadata)
    assert overlay.input_tokens == 90
    assert overlay.output_tokens == 9


# ----------------------------------------------------------------------
# Overlay + provider mapping units
# ----------------------------------------------------------------------


def test_usage_overlay_cache_status_classification():
    def overlay(details):
        return usage_overlay(
            {
                "input_tokens": 10,
                "output_tokens": 1,
                "input_token_details": details,
            }
        )

    assert overlay({"cache_read": 5}).cache_status == "hit"
    assert overlay({"cache_creation": 5}).cache_status == "miss"
    assert (
        overlay({"cache_read": 5, "cache_creation": 5}).cache_status
        == "partial"
    )
    assert overlay({}).cache_status == "n/a"
    assert usage_overlay(None).input_tokens == 0


def test_usage_overlay_reads_reasoning_tokens():
    overlay = usage_overlay(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "output_token_details": {"reasoning": 12},
        }
    )
    assert overlay.reasoning_tokens == 12


def test_map_provider_aliases_and_model_sniffing():
    assert map_provider("azure_openai") == "openai"
    assert map_provider("azure") == "openai"
    assert map_provider("google_genai") == "gemini"
    assert map_provider("google_vertexai") == "gemini"
    assert map_provider("amazon_bedrock") == "bedrock"
    assert map_provider("bedrock_converse") == "bedrock"
    assert map_provider("Anthropic") == "anthropic"
    # Unknown identifiers pass through (pricing simply misses).
    assert map_provider("somethingelse") == "somethingelse"
    # No identifier: conservative model-name sniff.
    assert map_provider(None, "claude-haiku-4-5") == "anthropic"
    assert map_provider("", "gpt-4o") == "openai"
    assert map_provider(None, "o3-mini") == "openai"
    assert map_provider(None, "gemini-1.5-pro") == "gemini"
    assert map_provider(None, "mystery-model") == "unknown"
    assert map_provider(None, "") == "unknown"


# ----------------------------------------------------------------------
# Request-side causal split details
# ----------------------------------------------------------------------


def test_stable_system_prefix_splits_static_from_dynamic():
    state = InMemoryRunState()
    base = "You are a weather assistant for the operations team. "

    def fixture(day: str) -> dict:
        return {
            "request": {
                "raw_provider": "anthropic",
                "model": "claude-haiku-4-5",
                "messages": [
                    {"type": "system", "content": base + f"Today is {day}."},
                    {"type": "human", "content": "Forecast please."},
                ],
                "tools": [],
            },
            "response": {"generations": [[]], "llm_output": {}},
        }

    first = _translate(fixture("Monday"), run_state=state)
    # First observation seeds the prefix with the whole block.
    assert first.ledger.system_dynamic_tokens == 0
    assert first.ledger.system_static_tokens > 0

    second = _translate(fixture("Friday"), run_state=state)
    assert second.ledger.system_dynamic_tokens > 0
    assert second.ledger.system_static_tokens > 0
    assert (
        second.ledger.system_static_tokens
        < first.ledger.system_static_tokens
    )


def test_tool_result_blocks_in_human_content_count_as_tool_result():
    payload = json.dumps({"rows": ["data"] * 40})
    fixture = {
        "request": {
            "raw_provider": "anthropic",
            "model": "claude-haiku-4-5",
            "messages": [
                {"type": "human", "content": "Summarise the table."},
                {
                    "type": "human",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_x",
                            "content": payload,
                        },
                        {"type": "text", "text": "Go on."},
                    ],
                },
            ],
            "tools": [],
        },
        "response": {"generations": [[]], "llm_output": {}},
    }
    neutral_call = _translate(fixture)
    assert neutral_call.ledger.tool_result_tokens > 0
    # The last human message's countable text is just "Go on." —
    # the embedded tool result must not inflate user input.
    assert (
        neutral_call.ledger.user_input_tokens
        < neutral_call.ledger.tool_result_tokens
    )
    # The earlier human turn is memory.
    assert neutral_call.ledger.memory_tokens > 0


def test_pending_retrieval_marker_is_consumed_once():
    state = InMemoryRunState()
    state.pending_retrieved_context_tokens = 77
    fixture = _load("anthropic")

    first = _translate(fixture, run_state=state)
    assert first.ledger.retrieved_context_tokens == 77
    assert state.pending_retrieved_context_tokens == 0

    second = _translate(fixture, run_state=state)
    assert second.ledger.retrieved_context_tokens == 0


# ----------------------------------------------------------------------
# Real langchain_core objects (when available)
# ----------------------------------------------------------------------


def test_real_langchain_message_objects_translate_like_dicts():
    pytest.importorskip("langchain_core")
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )
    from langchain_core.outputs import ChatGeneration, LLMResult

    fixture = _load("bedrock")
    request_fix = fixture["request"]

    def to_object(msg: dict):
        kind = msg["type"]
        if kind == "system":
            return SystemMessage(content=msg["content"])
        if kind == "human":
            return HumanMessage(content=msg["content"])
        if kind == "ai":
            return AIMessage(
                content=msg["content"],
                tool_calls=msg.get("tool_calls") or [],
            )
        return ToolMessage(
            content=msg["content"],
            tool_call_id=msg.get("tool_call_id", "tool-call"),
        )

    response_fix = fixture["response"]["generations"][0][0]["message"]
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content=response_fix["content"],
                        usage_metadata=response_fix["usage_metadata"],
                        response_metadata=response_fix["response_metadata"],
                    )
                )
            ]
        ]
    )

    from_objects = LangChainTranslator().translate(
        request={
            "provider": "bedrock",
            "model": request_fix["model"],
            "messages": [to_object(m) for m in request_fix["messages"]],
            "tools": request_fix["tools"],
        },
        response=result,
        run_state=InMemoryRunState(),
        started_at=1_000,
        ended_at=2_000,
        sequence=1,
    )
    from_dicts = _translate(fixture)

    assert from_objects.ledger == from_dicts.ledger
    assert from_objects.cache_status == from_dicts.cache_status
    assert from_objects.tools_offered == from_dicts.tools_offered
    assert (
        from_objects.estimated_nanodollars
        == from_dicts.estimated_nanodollars
    )
