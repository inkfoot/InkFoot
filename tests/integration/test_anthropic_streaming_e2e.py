"""Live Anthropic *streaming* smoke tests (opt-in).

Anthropic has two streaming entry points and the shim patches both:

* ``messages.stream(...)`` — the ergonomic context manager. Every
  consumption mode (text iterator, ``get_final_message``) pulls from
  the same underlying event stream the shim tees, so the captured
  output count must match the final message's usage.
* ``messages.create(stream=True)`` — the low-level raw-event iterator.

The terminal ``message_delta`` event carries the cumulative
``output_tokens``; the captured event copies it verbatim (no
``stream_no_usage`` flag). The LangChain handler is disabled so the
shim is the only observer. Skips cleanly without credentials.
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


_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
_MODEL = os.environ.get(
    "INKFOOT_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5"
)


def _llm_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


@pytest.mark.live_anthropic
@pytest.mark.skipif(not _ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not set")
def test_live_messages_stream_manager_is_captured(tmp_path) -> None:
    anthropic = pytest.importorskip("anthropic")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-anthropic-stream-manager") as run:
        with anthropic.Anthropic().messages.stream(
            model=_MODEL,
            max_tokens=64,
            messages=[
                {"role": "user", "content": "Name a primary color."}
            ],
        ) as stream:
            for _text in stream.text_stream:
                pass
            final = stream.get_final_message()
        run_id = run.id

    assert final.id.startswith("msg_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "anthropic"
    assert "stream_no_usage" not in payload["estimation_flags"]
    assert payload["ledger"]["output_tokens"] == final.usage.output_tokens
    assert payload["ledger"]["user_input_tokens"] > 0
    assert payload["estimated_nanodollars"] > 0


@pytest.mark.live_anthropic
@pytest.mark.skipif(not _ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not set")
def test_live_messages_create_stream_is_captured(tmp_path) -> None:
    anthropic = pytest.importorskip("anthropic")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-anthropic-create-stream") as run:
        output_tokens = None
        for event in anthropic.Anthropic().messages.create(
            model=_MODEL,
            max_tokens=64,
            messages=[{"role": "user", "content": "Reply with OK."}],
            stream=True,
        ):
            if getattr(event, "type", None) == "message_delta":
                output_tokens = event.usage.output_tokens
        run_id = run.id

    assert output_tokens is not None
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "anthropic"
    assert payload["ledger"]["output_tokens"] == output_tokens
