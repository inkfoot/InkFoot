"""End-to-end: the Gemini shim through ``inkfoot.agent_run``.

Offline tests run against the fake ``google.generativeai`` SDK and
exercise the full wiring — instrument → shim → translator → storage —
including the cache-resource flow (``CacheControlPlacer`` creates a
``CachedContent`` once; later calls rebind to it and bill reads).

The live smoke test is opt-in: marked ``live_gemini`` and skipped
unless ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` is set and the real
SDK is installed.
"""

from __future__ import annotations

import json
import os

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy import CacheControlPlacer
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.providers.gemini import GEMINI_CACHE_MANAGER
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_gemini, uninstall_fake_sdks


@pytest.fixture(autouse=True)
def reset_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    GEMINI_CACHE_MANAGER.reset()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    GEMINI_CACHE_MANAGER.reset()
    uninstall_fake_sdks()


def _llm_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


# ----------------------------------------------------------------------
# Offline (fake SDK)
# ----------------------------------------------------------------------


def test_multi_turn_tool_conversation_is_fully_attributed(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    tools = [
        {
            "function_declarations": [
                {"name": "get_weather", "description": "current weather"}
            ]
        }
    ]
    with inkfoot.agent_run(task="triage") as run:
        model = fakes["GenerativeModel"](
            "gemini-1.5-pro",
            system_instruction="You are a deploy-triage agent.",
            tools=tools,
        )
        model.generate_content("check SFO weather")
        model.generate_content(
            [
                {"role": "user", "parts": ["check SFO weather"]},
                {
                    "role": "model",
                    "parts": [
                        {"function_call": {"name": "get_weather", "args": {}}}
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": "get_weather",
                                "response": {"temp_c": 18, "sky": "fog"},
                            }
                        }
                    ],
                },
            ]
        )
        run_id = run.id

    first, second = _llm_payloads(storage, run_id)

    assert first["provider"] == "gemini"
    assert first["model"] == "gemini-1.5-pro"
    # Model-bound system + tools reached the ledger via the shim's
    # synthesised request.
    assert first["ledger"]["system_static_tokens"] > 0
    assert first["ledger"]["tool_schema_tokens"] > 0
    assert first["ledger"]["user_input_tokens"] > 0
    assert first["tools_offered"] == ["get_weather"]
    assert first["estimated_nanodollars"] > 0

    # Turn 2: the function_response bills as tool result, the prior
    # turns as memory.
    assert second["ledger"]["tool_result_tokens"] > 0
    assert second["ledger"]["memory_tokens"] > 0


def test_cache_resource_created_on_first_call_then_referenced(
    tmp_path,
) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, policies=[CacheControlPlacer()])

    big_system = "deploy-triage rules; " * 7_000
    with inkfoot.agent_run(task="triage") as run:
        model = fakes["GenerativeModel"](
            "gemini-1.5-pro", system_instruction=big_system
        )
        model.generate_content("first")
        model.generate_content("second")
        run_id = run.id

    assert len(fakes["cache_creations"]) == 1
    kinds = [ev["kind"] for ev in storage.iter_events(run_id)]
    assert kinds.count("cache_resource_created") == 1

    first, second = _llm_payloads(storage, run_id)
    assert first["cache_status"] == "miss"
    assert first["ledger"]["cache_creation_tokens"] > 0
    assert second["cache_status"] == "hit"
    assert second["ledger"]["cache_read_tokens"] > 0


def test_uninstrumented_behaviour_is_restored_after_shutdown(
    tmp_path,
) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    original = fakes["GenerativeModel"].generate_content
    inkfoot.instrument(storage=storage)
    instrument_mod.shutdown()

    assert fakes["GenerativeModel"].generate_content is original
    # Calls after shutdown still work and write nothing.
    model = fakes["GenerativeModel"]("gemini-1.5-pro")
    result = model.generate_content("hi")
    assert result["usage_metadata"]["candidates_token_count"] == 5


# ----------------------------------------------------------------------
# Live smoke (opt-in)
# ----------------------------------------------------------------------

_LIVE_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get(
    "GOOGLE_API_KEY"
)


@pytest.mark.live_gemini
@pytest.mark.skipif(
    not _LIVE_KEY, reason="GEMINI_API_KEY / GOOGLE_API_KEY not set"
)
def test_live_gemini_call_emits_one_event(tmp_path) -> None:
    genai = pytest.importorskip("google.generativeai")
    genai.configure(api_key=_LIVE_KEY)

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    with inkfoot.agent_run(task="live-smoke") as run:
        model = genai.GenerativeModel("gemini-1.5-flash")
        model.generate_content("Reply with the single word OK.")
        run_id = run.id

    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "gemini"
    assert payload["ledger"]["output_tokens"] > 0
    assert payload["estimated_nanodollars"] > 0
