"""Cross-layer dedup on the Responses-API path.

A LangChain chat model configured for the Responses API ultimately
calls ``client.responses.create`` — which the Responses shim also
patches. Both layers therefore observe the same provider call, keyed
by the same wire response id (``resp_...``): the shim reads it off
the raw response, the handler reads it off ``response_metadata``.
The emit gate must collapse the pair to exactly one event — the
shim's, since it fires inside the SDK call, before ``on_llm_end``.

Mirrors ``test_handler_shim_dedup.py`` (the chat-completions /
Anthropic suite); this file pins the same guarantees for the
Responses surface.
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_openai, uninstall_fake_sdks


@pytest.fixture(autouse=True)
def reset_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    uninstall_fake_sdks()


class _ResponsesBackedChatModel(BaseChatModel):
    """Chat model whose ``_generate`` calls the (fake, shimmed)
    Responses API and copies the wire response id into
    ``response_metadata`` — exactly what the real partner package
    does when the Responses API is enabled. Inkfoot therefore sees
    each call twice: once through the Responses shim, once through
    the callback handler."""

    model: str = "gpt-4o"

    @property
    def _llm_type(self) -> str:
        return "responses-backed-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import openai  # the fake installed by the test

        response = openai.OpenAI().responses.create(
            model=self.model,
            input="hello",
        )
        usage = response["usage"]
        message = AIMessage(
            content=response["output"][0]["content"][0]["text"],
            usage_metadata={
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
            },
            response_metadata={
                "id": response["id"],
                "model_name": self.model,
                "model_provider": "openai",
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _llm_call_payloads(storage: SQLiteStorage) -> list[dict]:
    rows = storage._conn().execute(
        "SELECT payload_json FROM events"
        " WHERE kind = 'llm_call' ORDER BY sequence"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_double_observed_responses_call_lands_exactly_one_event(
    tmp_path, caplog
):
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _ResponsesBackedChatModel()
    with caplog.at_level(logging.DEBUG, logger="inkfoot.shims"):
        with inkfoot.agent_run(task="dedup"):
            result = model.invoke("hello")

    assert result.content == "ack"
    assert len(fakes["responses_calls"]) == 1

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert payloads[0]["provider"] == "openai"
    # The shim's event (written mid-_generate, before on_llm_end)
    # is the survivor: shim events carry no captured_by stamp and
    # keep the richer wire-shape payload.
    assert (payloads[0].get("metadata") or {}).get("captured_by") is None
    assert any(
        "already recorded" in record.getMessage()
        for record in caplog.records
    )


def test_each_distinct_response_id_keeps_its_own_event(tmp_path):
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _ResponsesBackedChatModel()
    with inkfoot.agent_run(task="dedup"):
        model.invoke("first")
        model.invoke("second")

    assert len(fakes["responses_calls"]) == 2
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 2
    assert payloads[1]["sequence"] == payloads[0]["sequence"] + 1


def test_shim_only_responses_capture_is_never_suppressed(tmp_path):
    # No LangChain involvement at all: a raw-SDK Responses call must
    # land exactly one event even with the handler registered.
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    import openai

    with inkfoot.agent_run(task="dedup"):
        openai.OpenAI().responses.create(model="gpt-4o", input="hi")

    assert len(fakes["responses_calls"]) == 1
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert (payloads[0].get("metadata") or {}).get("captured_by") is None


def test_dedup_is_scoped_per_run(tmp_path, monkeypatch):
    # The same resp_ id in two different runs is two events: the
    # seen-ids ledger is per run and released with the run state.
    fakes = install_fake_openai()

    def _fixed_id_payload(self, *args, **kwargs):
        fakes["responses_calls"].append({"variant": "sync", "kwargs": kwargs})
        return {
            "id": "resp_fixed",
            "object": "response",
            "status": "completed",
            "model": "gpt-4o",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ack"}],
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
        }

    monkeypatch.setattr(fakes["Responses"], "create", _fixed_id_payload)
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    import openai

    with inkfoot.agent_run(task="one"):
        openai.OpenAI().responses.create(model="gpt-4o", input="hi")
    with inkfoot.agent_run(task="two"):
        openai.OpenAI().responses.create(model="gpt-4o", input="hi")

    assert len(_llm_call_payloads(storage)) == 2
