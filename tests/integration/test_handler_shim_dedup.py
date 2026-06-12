"""Cross-layer dedup: when the LangChain callback handler and a
raw-SDK shim both observe the same provider call, exactly one
``llm_call`` event may land.

The shim fires mid-``_generate`` (it wraps the SDK method itself), so
its event is always written first; the handler's ``on_llm_end`` runs
afterwards and must recognise the provider response id and stand
down. The reverse guarantees matter too: distinct calls keep their
own events, handler-only captures are never suppressed, and the
dedup ledger is scoped to a single run.

Error paths can't use response ids (failures don't have one), so the
emit gate keys on the exception object itself — matched through its
``__cause__``/``__context__`` chain, so a partner-package wrapper
around the SDK exception still pairs with the shim's sighting, while
a failure raised above the SDK (which the shim never saw) still
lands.
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
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks


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


class _ShimBackedChatModel(BaseChatModel):
    """Chat model whose ``_generate`` calls the (fake, shimmed)
    Anthropic SDK and copies the provider response id into
    ``response_metadata`` — exactly what the real partner package
    does. Inkfoot therefore sees each call twice: once through the
    raw-SDK shim, once through the callback handler."""

    model: str = "claude-haiku-4-5"

    @property
    def _llm_type(self) -> str:
        return "shim-backed-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import anthropic  # the fake installed by the test

        response = anthropic.Anthropic().messages.create(
            model=self.model,
            messages=[{"role": "user", "content": "hello"}],
        )
        usage = response["usage"]
        message = AIMessage(
            content=response["content"][0]["text"],
            usage_metadata={
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": (
                    usage["input_tokens"] + usage["output_tokens"]
                ),
            },
            response_metadata={
                "id": response["id"],
                "model_name": self.model,
                "model_provider": "anthropic",
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class ChatAnthropic(BaseChatModel):
    """Raising stand-in. The class name matters: LangChain derives
    ``ls_provider`` from it (``Chat`` prefix stripped, lowercased),
    so this model reports ``ls_provider="anthropic"`` just like the
    real partner class — which is what the handler's error-path
    provider resolution keys on."""

    model: str = "claude-haiku-4-5"

    @property
    def _llm_type(self) -> str:
        return "raising-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("provider exploded")


class _StaticChatModel(BaseChatModel):
    """Returns one canned message; used for handler-only flows."""

    message: AIMessage

    @property
    def _llm_type(self) -> str:
        return "static-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=self.message)]
        )


def _canned_message(response_id: str) -> AIMessage:
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        },
        response_metadata={
            "model_name": "claude-haiku-4-5",
            "model_provider": "anthropic",
            "id": response_id,
        },
    )


def _llm_call_payloads(storage: SQLiteStorage) -> list[dict]:
    rows = storage._conn().execute(
        "SELECT payload_json FROM events"
        " WHERE kind = 'llm_call' ORDER BY sequence"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


# ----------------------------------------------------------------------
# Success path: response-id dedup
# ----------------------------------------------------------------------


def test_double_observed_call_lands_exactly_one_event(tmp_path, caplog):
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _ShimBackedChatModel()
    with caplog.at_level(logging.DEBUG, logger="inkfoot.shims"):
        with inkfoot.agent_run(task="dedup"):
            result = model.invoke("hello")

    assert result.content == "ack"
    assert len(fakes["calls"]) == 1

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert payloads[0]["provider"] == "anthropic"
    # The shim's event (written mid-_generate, before on_llm_end)
    # is the survivor: shim events carry no captured_by stamp.
    assert (payloads[0].get("metadata") or {}).get("captured_by") is None
    assert any(
        "already recorded" in record.getMessage()
        for record in caplog.records
    )


def test_each_distinct_response_id_keeps_its_own_event(tmp_path):
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _ShimBackedChatModel()
    with inkfoot.agent_run(task="dedup"):
        model.invoke("first")
        model.invoke("second")

    assert len(fakes["calls"]) == 2
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 2
    # Handler-side skips happen before sequence allocation, so the
    # surviving events stay contiguous (no gaps where the handler's
    # duplicate emits were dropped).
    assert payloads[1]["sequence"] == payloads[0]["sequence"] + 1


def test_handler_capture_is_not_suppressed_without_a_shim(tmp_path):
    # No fake SDK installed -> no raw-SDK shim -> the handler is the
    # only observer; its first sighting of each id must pass through.
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    with inkfoot.agent_run(task="dedup"):
        _StaticChatModel(message=_canned_message("msg_dedup_a")).invoke("hi")
        _StaticChatModel(message=_canned_message("msg_dedup_b")).invoke("hi")

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 2
    assert all(
        p["metadata"]["captured_by"] == "langchain_handler"
        for p in payloads
    )


def test_repeated_response_id_within_a_run_collapses(tmp_path):
    # Real providers mint unique ids; the same id twice in one run
    # means two layers (or a naive retry wrapper) reported the same
    # underlying call -> one ledger entry.
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _StaticChatModel(message=_canned_message("msg_dedup_same"))
    with inkfoot.agent_run(task="dedup"):
        model.invoke("hi")
        model.invoke("hi")

    assert len(_llm_call_payloads(storage)) == 1


def test_dedup_is_scoped_per_run(tmp_path):
    # The same response id in two different runs is two events: the
    # seen-ids ledger is per run and released with the run state.
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = _StaticChatModel(message=_canned_message("msg_dedup_same"))
    with inkfoot.agent_run(task="one"):
        model.invoke("hi")
    with inkfoot.agent_run(task="two"):
        model.invoke("hi")

    assert len(_llm_call_payloads(storage)) == 2


# ----------------------------------------------------------------------
# Error path: exception-identity dedup
# ----------------------------------------------------------------------


class _FakeAPIError(Exception):
    """Stand-in for an SDK-level failure (rate limit, auth, ...)."""


class _WrappingChatModel(_ShimBackedChatModel):
    """Partner packages sometimes catch the SDK exception and raise
    their own; the ``from`` chain is how the dedup still pairs the
    handler's sighting with the shim's."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
        except Exception as exc:
            raise RuntimeError("partner wrapped the failure") from exc


def test_sdk_error_lands_exactly_one_event_when_both_layers_observe(
    tmp_path, caplog
):
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    fakes["errors"].append(_FakeAPIError("rate limited"))
    model = _ShimBackedChatModel()
    with caplog.at_level(logging.DEBUG, logger="inkfoot.shims"):
        with inkfoot.agent_run(task="dedup"):
            with pytest.raises(_FakeAPIError, match="rate limited"):
                model.invoke("hi")

    # The SDK was reached and failed: the shim saw the exception
    # first-hand and its event survives; the handler's later
    # sighting of the same exception is skipped.
    assert len(fakes["calls"]) == 1
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert payloads[0]["provider"] == "anthropic"
    assert payloads[0]["error"]["type"] == "_FakeAPIError"
    assert "rate limited" in payloads[0]["error"]["message"]
    assert any(
        "skipping duplicate error emit" in record.getMessage()
        for record in caplog.records
    )


def test_wrapped_sdk_error_still_collapses_to_one_event(tmp_path):
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    fakes["errors"].append(_FakeAPIError("boom at the wire"))
    model = _WrappingChatModel()
    with inkfoot.agent_run(task="dedup"):
        with pytest.raises(RuntimeError, match="partner wrapped"):
            model.invoke("hi")

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    # The surviving event is the shim's: it names the SDK-level
    # exception, not the wrapper the handler saw.
    assert payloads[0]["error"]["type"] == "_FakeAPIError"
    assert "boom at the wire" in payloads[0]["error"]["message"]


def test_pre_sdk_error_is_recorded_even_when_the_shim_is_installed(tmp_path):
    # A failure raised in the LangChain layer never reaches the SDK,
    # so the shim records nothing — the handler must not stand down
    # just because the provider's shim happens to be installed.
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = ChatAnthropic()
    with inkfoot.agent_run(task="dedup"):
        with pytest.raises(RuntimeError, match="provider exploded"):
            model.invoke("hi")

    assert fakes["calls"] == []  # the SDK was never reached
    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert payloads[0]["provider"] == "anthropic"
    assert payloads[0]["error"]["type"] == "RuntimeError"
    assert "provider exploded" in payloads[0]["error"]["message"]


def test_error_event_emitted_when_no_shim_observes_the_provider(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)

    model = ChatAnthropic()
    with inkfoot.agent_run(task="dedup"):
        with pytest.raises(RuntimeError, match="provider exploded"):
            model.invoke("hi")

    payloads = _llm_call_payloads(storage)
    assert len(payloads) == 1
    assert payloads[0]["provider"] == "anthropic"
    assert payloads[0]["error"]["type"] == "RuntimeError"
    assert "provider exploded" in payloads[0]["error"]["message"]
