"""Skeleton-level checks for the LangChain callback handler.

Covers the wiring contract rather than attribution detail (that
lives in ``test_langchain_normalise.py``): the handler is a real
``BaseCallbackHandler``, its hooks never break a chain, the global
registration is idempotent and survives deactivate/reactivate
cycles, and ``inkfoot.instrument()`` auto-detects ``langchain_core``.
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import inkfoot
import inkfoot._instrument as instrument_mod
import inkfoot.langchain as inkfoot_langchain
from inkfoot.langchain import InkfootCallbackHandler
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import uninstall_fake_sdks


@pytest.fixture(autouse=True)
def clean_instrumentation_state():
    from inkfoot._run_context import _clear_current_run

    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()
    yield
    _clear_current_run()
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()


class _StaticChatModel(BaseChatModel):
    """Minimal chat model that returns one canned message — enough
    to drive the real LangChain callback plumbing offline."""

    message: AIMessage

    @property
    def _llm_type(self) -> str:
        return "static-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=self.message)]
        )


def _canned_message(response_id: str = "msg_skeleton_1") -> AIMessage:
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


# ----------------------------------------------------------------------
# Handler shape + isolation
# ----------------------------------------------------------------------


def test_handler_imports_and_is_a_base_callback_handler():
    handler = InkfootCallbackHandler()
    assert isinstance(handler, BaseCallbackHandler)


def test_raising_hook_is_swallowed_and_the_chain_continues(caplog):
    handler = InkfootCallbackHandler()

    def boom(*args, **kwargs):
        raise RuntimeError("intentional hook failure")

    handler._record_start = boom
    handler._record_end = boom
    handler._record_error = boom

    model = _StaticChatModel(message=AIMessage(content="still works"))
    with caplog.at_level(logging.WARNING, logger="inkfoot.errors"):
        result = model.invoke("hello", config={"callbacks": [handler]})

    assert result.content == "still works"
    assert any("isolated" in record.message for record in caplog.records)


def test_handler_is_a_noop_before_instrument():
    # No instrument() call — no storage to write to. The callbacks
    # must degrade to silence, not errors.
    handler = InkfootCallbackHandler()
    model = _StaticChatModel(message=_canned_message())
    result = model.invoke("hi", config={"callbacks": [handler]})
    assert result.content == "ok"


def test_embeddings_stubs_are_noops():
    handler = InkfootCallbackHandler()
    assert handler.on_embeddings_start("anything", ["text"]) is None
    assert handler.on_embeddings_end(object()) is None


# ----------------------------------------------------------------------
# Global registration lifecycle
# ----------------------------------------------------------------------


def test_global_install_is_idempotent_and_returns_the_singleton():
    from langchain_core.tracers.context import _configure_hooks

    first = inkfoot_langchain.instrument()
    hooks_after_first = len(_configure_hooks)
    second = inkfoot_langchain.instrument()

    assert second is first
    assert len(_configure_hooks) == hooks_after_first
    assert inkfoot_langchain.get_handler() is first


def test_first_registration_logs_once_at_info(caplog, monkeypatch):
    # The singleton may already be registered by an earlier test in
    # this process (langchain has no unregister API). Re-arming
    # ``_REGISTERED`` while keeping the same handler instance is
    # safe: the re-registered hook contributes the *same* instance,
    # which LangChain's ``add_handler`` deduplicates.
    handler = inkfoot_langchain.instrument()
    monkeypatch.setattr(inkfoot_langchain, "_REGISTERED", False)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="inkfoot.langchain"):
        assert inkfoot_langchain.instrument() is handler
    infos = [
        r
        for r in caplog.records
        if r.name == "inkfoot.langchain"
        and r.levelno == logging.INFO
        and "registered" in r.getMessage()
    ]
    assert len(infos) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="inkfoot.langchain"):
        assert inkfoot_langchain.instrument() is handler
    assert not [
        r
        for r in caplog.records
        if r.name == "inkfoot.langchain" and r.levelno == logging.INFO
    ]


def test_uninstrument_deactivates_and_reinstrument_reactivates():
    handler = inkfoot_langchain.instrument()
    assert inkfoot_langchain.is_instrumented() is True

    inkfoot_langchain.uninstrument()
    assert handler.is_active is False
    assert inkfoot_langchain.is_instrumented() is False

    assert inkfoot_langchain.instrument() is handler
    assert handler.is_active is True


def test_deactivated_handler_records_nothing(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    inkfoot_langchain.uninstrument()

    model = _StaticChatModel(message=_canned_message())
    with inkfoot.agent_run(task="inactive"):
        model.invoke("hi")

    rows = storage._conn().execute(
        "SELECT 1 FROM events WHERE kind = 'llm_call'"
    ).fetchall()
    assert rows == []


# ----------------------------------------------------------------------
# inkfoot.instrument() wiring
# ----------------------------------------------------------------------


def test_instrument_auto_detects_and_captures_without_explicit_callbacks(
    tmp_path,
):
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)  # langchain="auto" default
    assert inkfoot_langchain.is_instrumented() is True

    model = _StaticChatModel(message=_canned_message())
    with inkfoot.agent_run(task="skeleton"):
        model.invoke("hi")  # no callbacks passed anywhere

    rows = storage._conn().execute(
        "SELECT payload_json FROM events WHERE kind = 'llm_call'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-haiku-4-5"
    assert payload["metadata"]["captured_by"] == "langchain_handler"


def test_instrument_langchain_false_skips_registration(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)
    assert inkfoot_langchain.is_instrumented() is False


def test_instrument_rejects_invalid_langchain_value():
    with pytest.raises(ValueError, match="langchain"):
        inkfoot.instrument(langchain="yes")
    assert instrument_mod.is_instrumented() is False


def test_instrument_langchain_true_without_the_dependency_raises(
    monkeypatch,
):
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "langchain_core":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)
    with pytest.raises(ImportError, match=r"inkfoot\[langchain\]"):
        inkfoot.instrument(langchain=True)
    assert instrument_mod.is_instrumented() is False


def test_shutdown_deactivates_the_global_handler(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    handler = inkfoot_langchain.get_handler()
    assert handler is not None and handler.is_active is True

    instrument_mod.shutdown()
    assert handler.is_active is False
