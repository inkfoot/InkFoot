"""Live OpenAI Chat Completions *streaming* smoke tests (opt-in).

Two things only the real API can prove:

* With ``stream_options={"include_usage": True}`` the final chunk
  carries authoritative usage, and the captured event copies its
  output count verbatim — no estimate, no ``stream_options_off`` flag.
* Without it the stream is usage-free, so the shim estimates the
  output from the streamed text and flags the event
  ``stream_options_off``. That estimate must land within a sane band
  of the authoritative number for the same prompt.

The LangChain handler is disabled so the shim is the only observer.
Skips cleanly without credentials; the weekly live workflow supplies
them.
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


_PROMPT = "List three primary colors, one per line."


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_chat_stream_with_usage_copies_output_verbatim(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-chat-stream-usage") as run:
        stream = openai.OpenAI().chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": _PROMPT}],
            temperature=0,
            stream=True,
            stream_options={"include_usage": True},
        )
        usage_seen = None
        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage_seen = chunk.usage
        run_id = run.id

    assert usage_seen is not None, "include_usage chunk never arrived"
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "openai"
    assert "stream_options_off" not in payload["estimation_flags"]
    assert payload["ledger"]["output_tokens"] == usage_seen.completion_tokens
    assert payload["estimated_nanodollars"] > 0


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_chat_stream_without_usage_estimates_within_band(
    tmp_path,
) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    messages = [{"role": "user", "content": _PROMPT}]

    # Ground truth: the same prompt with usage on.
    with inkfoot.agent_run(task="live-chat-stream-truth") as truth_run:
        truth_stream = openai.OpenAI().chat.completions.create(
            model=_MODEL,
            messages=messages,
            temperature=0,
            stream=True,
            stream_options={"include_usage": True},
        )
        for _ in truth_stream:
            pass
        truth_run_id = truth_run.id

    # Estimate: the same prompt with usage off -> tokeniser estimate.
    with inkfoot.agent_run(task="live-chat-stream-estimate") as est_run:
        est_stream = openai.OpenAI().chat.completions.create(
            model=_MODEL,
            messages=messages,
            temperature=0,
            stream=True,
        )
        for _ in est_stream:
            pass
        est_run_id = est_run.id

    (truth,) = _llm_payloads(storage, truth_run_id)
    (estimate,) = _llm_payloads(storage, est_run_id)

    assert "stream_options_off" in estimate["estimation_flags"]
    assert "output_tokens" in estimate["estimation_flags"]

    truth_out = truth["ledger"]["output_tokens"]
    est_out = estimate["ledger"]["output_tokens"]
    assert truth_out > 0 and est_out > 0
    # The tokeniser can't see provider-side formatting overhead, so allow
    # a generous band — the point is "same ballpark", not exactness.
    assert abs(est_out - truth_out) <= 0.2 * truth_out + 5
