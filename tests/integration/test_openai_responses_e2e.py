"""Live OpenAI Responses API smoke tests (opt-in).

These drive the real ``client.responses.create`` surface through the
raw-SDK shim and check the captured event against the provider's own
usage numbers: exactly one ``llm_call`` event, output tokens copied
verbatim, a causal input split in the same ballpark as the billed
input, and — critically — no ``responses_shape_unknown:*`` flags.
The golden fixtures in the unit suite mirror these calls' shapes;
this is where we learn when reality drifts.

The LangChain handler is deliberately disabled
(``instrument(langchain=False)``) so the shim is the only observer —
the handler's own Responses coverage lives in
``test_langchain_e2e.py``, and cross-layer dedup in
``test_handler_responses_dedup.py``.

Every test skips cleanly without credentials; the weekly live
workflows supply them in CI.
"""

from __future__ import annotations

import json
import os

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage


@pytest.fixture(autouse=True)
def reset_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()


# Request-side causal categories (the tokeniser-estimated split of
# the billed input). ``reasoning_tokens`` is excluded — it details
# the output side.
_REQUEST_SIDE_FIELDS = (
    "system_static_tokens",
    "system_dynamic_tokens",
    "user_input_tokens",
    "tool_schema_tokens",
    "tool_result_tokens",
    "retrieved_context_tokens",
    "memory_tokens",
    "retry_overhead_tokens",
    "summariser_tokens",
    "guardrail_tokens",
)


def _llm_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


def _assert_wire_shape_fully_mapped(payload: dict) -> None:
    unknown = [
        flag
        for flag in payload.get("estimation_flags") or ()
        if flag.startswith("responses_shape_unknown:")
    ]
    assert not unknown, f"wire shape drifted; unmapped keys: {unknown}"


_OPENAI_KEY = os.environ.get("OPENAI_API_KEY")


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_responses_call_with_tool_is_causally_split(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    with inkfoot.agent_run(task="live-responses") as run:
        response = openai.OpenAI().responses.create(
            model=os.environ.get("INKFOOT_LIVE_OPENAI_MODEL", "gpt-4o-mini"),
            instructions="You are a terse weather assistant.",
            input="What's the weather in Paris right now?",
            tools=tools,
        )
        run_id = run.id

    assert response.id.startswith("resp_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "openai"
    _assert_wire_shape_fully_mapped(payload)
    assert payload["tools_offered"] == ["get_weather"]

    ledger = payload["ledger"]
    assert ledger["output_tokens"] == response.usage.output_tokens
    assert ledger["system_static_tokens"] > 0
    assert ledger["user_input_tokens"] > 0
    assert ledger["tool_schema_tokens"] > 0

    causal_input = sum(ledger[f] for f in _REQUEST_SIDE_FIELDS)
    assert causal_input > 0
    # The split is tokeniser-estimated and can't see provider-side
    # harness overhead, so it may undershoot the billed input — but
    # it must never blow far past it. The +25 floor keeps tiny
    # prompts out of ratio noise.
    assert causal_input <= response.usage.input_tokens * 1.5 + 25
    assert payload["estimated_nanodollars"] > 0


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_reasoning_model_populates_reasoning_tokens(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-responses-reasoning") as run:
        response = openai.OpenAI().responses.create(
            model=os.environ.get(
                "INKFOOT_LIVE_OPENAI_REASONING_MODEL", "o4-mini"
            ),
            input=(
                "A bat and a ball cost $1.10 together; the bat costs "
                "$1.00 more than the ball. How much is the ball? "
                "Answer with just the number."
            ),
        )
        run_id = run.id

    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "openai"
    _assert_wire_shape_fully_mapped(payload)

    wire_reasoning = response.usage.output_tokens_details.reasoning_tokens
    assert wire_reasoning > 0, "model reported no reasoning tokens"
    assert payload["ledger"]["reasoning_tokens"] == wire_reasoning
    assert payload["ledger"]["output_tokens"] == response.usage.output_tokens


_AZURE_READY = all(
    os.environ.get(key)
    for key in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    )
)


@pytest.mark.live_azure
@pytest.mark.skipif(
    not _AZURE_READY,
    reason=(
        "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / "
        "AZURE_OPENAI_DEPLOYMENT not set"
    ),
)
def test_live_azure_responses_path_is_captured_by_the_shim(tmp_path) -> None:
    # ``AzureChatOpenAI(use_responses_api=True)`` routes through the
    # same ``Responses.create`` the shim patches — Azure coverage
    # comes free. With the handler off, the single captured event
    # proves the shim saw the call.
    pytest.importorskip("openai")
    langchain_openai = pytest.importorskip("langchain_openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    model = langchain_openai.AzureChatOpenAI(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.environ.get("OPENAI_API_VERSION", "preview"),
        use_responses_api=True,
    )
    with inkfoot.agent_run(task="live-azure-responses") as run:
        message = model.invoke("Reply with the single word OK.")
        run_id = run.id

    # Proof the Responses path (not Chat Completions) answered.
    assert message.response_metadata.get("id", "").startswith("resp_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "openai"
    _assert_wire_shape_fully_mapped(payload)
    assert payload["ledger"]["output_tokens"] > 0
    assert payload["ledger"]["user_input_tokens"] > 0
