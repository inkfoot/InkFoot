"""Cross-layer dedup for *streamed* calls.

A non-streamed call is safe by construction: the shim emits
synchronously inside the SDK call, always before the LangChain
handler's ``on_llm_end``. A streamed call breaks that timing — its
event is only complete at stream-close, which can land *after*
``on_llm_end`` for the same call. A naive first-emit-wins gate would
then let the handler's thinner event win.

The streaming recorder closes the gap by *claiming* the provider
response id at the first chunk that exposes it — which always precedes
``on_llm_end``, since the handler only finishes once the same stream is
fully consumed. These tests pin that: a handler emit racing a
mid-flight stream is suppressed, and the shim's richer streamed event
survives.
"""

from __future__ import annotations

import json
import logging

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    get_or_create_run_state,
)
from inkfoot.normalise.openai_responses import OpenAIResponsesTranslator
from inkfoot.policy import CallContext
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims._emit import emit_llm_call
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


def _llm_call_payloads(storage: SQLiteStorage) -> list[dict]:
    rows = storage._conn().execute(
        "SELECT payload_json FROM events"
        " WHERE kind = 'llm_call' ORDER BY sequence"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _emit_like_handler(
    storage: SQLiteStorage, run_id: str, response_id: str
) -> None:
    """Stand in for the LangChain handler's ``on_llm_end`` emit, with a
    deliberately distinct output count (999) so that if it ever won the
    race the surviving event would give it away."""
    ctx = CallContext(
        provider="openai",
        model="gpt-4o",
        run_id=run_id,
        request_kwargs={"model": "gpt-4o", "input": "hello world"},
    )
    response = {
        "id": response_id,
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
            "output_tokens": 999,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    emit_llm_call(
        ctx=ctx,
        response=response,
        started_at=1,
        ended_at=2,
        storage=storage,
        capture_mode="aggregate",
        translator=OpenAIResponsesTranslator(),
        before_decisions=[],
        response_id=response_id,
    )


def test_streamed_shim_claim_beats_a_late_handler_emit(tmp_path, caplog):
    # The handler emits *mid-stream* — after the shim has seen the first
    # event but before it closes. The claim made on that first event
    # must already own the id, so the handler stands down.
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with caplog.at_level(logging.DEBUG, logger="inkfoot.shims"):
        with inkfoot.agent_run(task="stream-dedup") as run:
            run_id = run.id
            client = fakes["OpenAI"]()
            stream = client.responses.create(
                model="gpt-4o", input="hello world", stream=True
            )
            iterator = iter(stream)
            first = next(iterator)  # shim claims resp_fake_1 here
            assert first["type"] == "response.created"

            # The handler's on_llm_end fires now, racing the open stream.
            _emit_like_handler(storage, run_id, "resp_fake_1")

            # Finish draining -> the shim finalises and emits.
            list(iterator)

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    # The survivor is the shim's streamed event (output 5), not the
    # handler's (output 999).
    assert payloads[0]["ledger"]["output_tokens"] == 5
    assert any(
        "already recorded" in record.getMessage()
        for record in caplog.records
    )


def test_streamed_call_double_observed_lands_one_event(tmp_path):
    # The realistic shape: a LangChain chat model whose ``_generate``
    # consumes a *streamed* SDK call and reports the response id. The
    # shim observes the stream; the handler observes ``on_llm_end``.
    # Exactly one event, and it is the shim's (no captured_by stamp).
    pytest.importorskip("langchain_core", reason="langchain-core not installed")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _StreamBackedChatModel(BaseChatModel):
        model: str = "gpt-4o"

        @property
        def _llm_type(self) -> str:
            return "stream-backed-chat-model"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            import openai  # the fake installed by the test

            stream = openai.OpenAI().responses.create(
                model=self.model, input="hello world", stream=True
            )
            text = ""
            resp_id = None
            usage = {}
            for event in stream:  # drives the shim's observer
                if event.get("type") == "response.completed":
                    response = event["response"]
                    resp_id = response["id"]
                    usage = response["usage"]
                    text = response["output"][0]["content"][0]["text"]
            message = AIMessage(
                content=text,
                usage_metadata={
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": usage["input_tokens"]
                    + usage["output_tokens"],
                },
                response_metadata={
                    "id": resp_id,
                    "model_name": self.model,
                    "model_provider": "openai",
                },
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _StreamBackedChatModel()
    with inkfoot.agent_run(task="stream-dedup"):
        result = model.invoke("hello")

    assert result.content == "ack"
    assert len(fakes["responses_calls"]) == 1
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    # Shim events carry no captured_by stamp; the handler's would.
    assert (payloads[0].get("metadata") or {}).get("captured_by") is None
    assert payloads[0]["ledger"]["output_tokens"] == 5


def test_get_or_create_run_state_is_available_for_streamed_emit(tmp_path):
    # Guards a regression where a streamed emit at close happened after
    # the run state had been torn down: the recorder must still resolve
    # the run state lazily.
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="stream-state") as run:
        run_id = run.id
        list(
            fakes["OpenAI"]().responses.create(
                model="gpt-4o", input="hi", stream=True
            )
        )
    assert get_or_create_run_state(run_id) is not None
    assert len(_llm_call_payloads(storage)) == 1
