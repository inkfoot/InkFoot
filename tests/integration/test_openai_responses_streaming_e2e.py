"""Live OpenAI Responses *streaming* smoke tests (opt-in).

The ergonomic ``client.responses.stream(...)`` helper routes through
the same ``Responses.create`` the shim patches, so teeing the event
iterator captures it. The terminal ``response.completed`` event carries
the finished object — full ``output`` and ``usage`` — which the shim
hands to the translator verbatim. This checks the captured event
against that object's own numbers and asserts no
``responses_shape_unknown:*`` / ``stream_no_usage`` flags.

The LangChain handler is disabled so the shim is the only observer.
Skips cleanly without credentials.
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


_OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
_MODEL = os.environ.get("INKFOOT_LIVE_OPENAI_MODEL", "gpt-4o-mini")


def _llm_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_responses_stream_uses_completed_usage(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-responses-stream") as run:
        final = None
        with openai.OpenAI().responses.stream(
            model=_MODEL,
            instructions="You are a terse assistant.",
            input="Name the largest planet in one word.",
        ) as stream:
            for _event in stream:
                pass
            final = stream.get_final_response()
        run_id = run.id

    assert final is not None
    assert final.id.startswith("resp_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "openai"
    unknown = [
        flag
        for flag in payload["estimation_flags"]
        if flag.startswith("responses_shape_unknown:") or flag == "stream_no_usage"
    ]
    assert not unknown, f"unexpected flags: {unknown}"
    assert payload["ledger"]["output_tokens"] == final.usage.output_tokens
    assert payload["ledger"]["user_input_tokens"] > 0
    assert payload["estimated_nanodollars"] > 0


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_responses_raw_create_stream_is_captured(tmp_path) -> None:
    # The low-level ``create(stream=True)`` form, iterated event by
    # event, must be captured too.
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-responses-raw-stream") as run:
        output_tokens = None
        for event in openai.OpenAI().responses.create(
            model=_MODEL,
            input="Reply with the single word OK.",
            stream=True,
        ):
            if getattr(event, "type", None) == "response.completed":
                output_tokens = event.response.usage.output_tokens
        run_id = run.id

    assert output_tokens is not None
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["ledger"]["output_tokens"] == output_tokens
