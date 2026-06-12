"""Live LangChain integration smoke tests (opt-in).

Each test drives a real partner chat model through the globally
registered Inkfoot callback handler and checks the captured event
against the provider's own usage numbers: exactly one ``llm_call``
event, the right provider tag, output tokens copied verbatim, and a
causal input split in the same ballpark as the billed input. The
golden fixtures in the unit suite mirror these integrations'
response shapes; this is where we learn when reality drifts.

The raw-SDK shims are deliberately disabled (``instrument(sdks=[])``)
so the handler is the only observer — these tests pin the LangChain
capture path itself, and cross-layer dedup has its own suite
(``test_handler_shim_dedup.py``).

Every test skips cleanly without credentials; the weekly
``live-langchain.yml`` workflow supplies them in CI.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

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


def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny in {city}"


_SYSTEM = "You are a terse weather assistant."
_PROMPT = "What's the weather in Paris right now?"

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


def _drive(model, tmp_path, *, provider: str):
    """Invoke ``model`` once (with one bound tool) under
    instrumentation; return ``(payload, message)`` after the shared
    assertions."""
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, sdks=[])

    bound = model.bind_tools([get_weather])
    with inkfoot.agent_run(task="live-langchain") as run:
        message = bound.invoke([("system", _SYSTEM), ("human", _PROMPT)])
        run_id = run.id

    payloads = [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["provider"] == provider
    assert payload["metadata"]["captured_by"] == "langchain_handler"

    usage = message.usage_metadata
    assert usage, "partner package reported no usage_metadata"
    ledger = payload["ledger"]
    assert ledger["output_tokens"] == usage["output_tokens"]

    causal_input = sum(ledger[f] for f in _REQUEST_SIDE_FIELDS)
    assert ledger["user_input_tokens"] > 0
    assert causal_input > 0
    # The split is tokeniser-estimated and can't see provider-side
    # harness overhead (tool-use system prompts and the like), so it
    # may undershoot the billed input — but it must never blow far
    # past it. The +25 floor keeps tiny prompts out of ratio noise.
    assert causal_input <= usage["input_tokens"] * 1.5 + 25
    return payload, message


# ----------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")


@pytest.mark.live_anthropic
@pytest.mark.skipif(not _ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not set")
def test_live_chat_anthropic_capture(tmp_path) -> None:
    langchain_anthropic = pytest.importorskip("langchain_anthropic")
    model = langchain_anthropic.ChatAnthropic(
        model=os.environ.get(
            "INKFOOT_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5"
        ),
        max_tokens=256,
    )
    payload, _ = _drive(model, tmp_path, provider="anthropic")
    assert payload["ledger"]["tool_schema_tokens"] > 0


# ----------------------------------------------------------------------
# OpenAI — Chat Completions and Responses
# ----------------------------------------------------------------------

_OPENAI_KEY = os.environ.get("OPENAI_API_KEY")


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_chat_openai_chat_completions_capture(tmp_path) -> None:
    langchain_openai = pytest.importorskip("langchain_openai")
    model = langchain_openai.ChatOpenAI(
        model=os.environ.get("INKFOOT_LIVE_OPENAI_MODEL", "gpt-4o-mini")
    )
    payload, _ = _drive(model, tmp_path, provider="openai")
    assert payload["ledger"]["tool_schema_tokens"] > 0


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_chat_openai_responses_api_capture(tmp_path) -> None:
    langchain_openai = pytest.importorskip("langchain_openai")
    model = langchain_openai.ChatOpenAI(
        model=os.environ.get("INKFOOT_LIVE_OPENAI_MODEL", "gpt-4o-mini"),
        use_responses_api=True,
    )
    payload, message = _drive(model, tmp_path, provider="openai")
    assert payload["ledger"]["tool_schema_tokens"] > 0
    # Proof the Responses path (not Chat Completions) answered.
    response_id = message.response_metadata.get("id", "")
    assert response_id.startswith("resp_")


# ----------------------------------------------------------------------
# Azure OpenAI
# ----------------------------------------------------------------------

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
def test_live_azure_chat_openai_capture(tmp_path) -> None:
    langchain_openai = pytest.importorskip("langchain_openai")
    model = langchain_openai.AzureChatOpenAI(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"),
    )
    # The azure identifier must land on openai pricing/reporting.
    _drive(model, tmp_path, provider="openai")


# ----------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------

_GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get(
    "GOOGLE_API_KEY"
)


@pytest.mark.live_gemini
@pytest.mark.skipif(
    not _GEMINI_KEY, reason="GEMINI_API_KEY / GOOGLE_API_KEY not set"
)
def test_live_chat_google_genai_capture(tmp_path) -> None:
    langchain_google_genai = pytest.importorskip("langchain_google_genai")
    model = langchain_google_genai.ChatGoogleGenerativeAI(
        model=os.environ.get(
            "INKFOOT_LIVE_GEMINI_MODEL", "gemini-1.5-flash"
        ),
        google_api_key=_GEMINI_KEY,
    )
    _drive(model, tmp_path, provider="gemini")


# ----------------------------------------------------------------------
# Bedrock
# ----------------------------------------------------------------------

_HAS_AWS_CREDS = bool(
    os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
)
_BEDROCK_MODEL = (
    os.environ.get("INKFOOT_LIVE_BEDROCK_MODEL")
    or "anthropic.claude-3-5-haiku-20241022-v1:0"
)


@pytest.mark.live_bedrock
@pytest.mark.skipif(
    not _HAS_AWS_CREDS,
    reason="AWS_ACCESS_KEY_ID / AWS_PROFILE not set",
)
def test_live_chat_bedrock_converse_capture(tmp_path) -> None:
    langchain_aws = pytest.importorskip("langchain_aws")
    model = langchain_aws.ChatBedrockConverse(model=_BEDROCK_MODEL)
    _drive(model, tmp_path, provider="bedrock")
