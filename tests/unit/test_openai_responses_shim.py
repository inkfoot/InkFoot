"""OpenAIResponsesShim tests — same axes as the OpenAI
chat-completions shim tests, plus the downlevel-SDK skip (an
``openai`` build without a Responses surface) and the
install-together contract with the chat shim."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    _set_current_run,
)
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_openai,
    uninstall_fake_sdks,
)


@pytest.fixture(autouse=True)
def reset_state() -> None:
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


def _seed(storage: SQLiteStorage, run_id: str = "test-run") -> str:
    storage.start_run(
        run_id=run_id,
        task="t",
        agent_kind="u",
        started_at=1_700_000_000_000,
    )
    return run_id


def _llm_call_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


def test_install_replaces_create_and_uninstall_restores(tmp_path) -> None:
    fakes = install_fake_openai()
    original_sync = fakes["Responses"].create
    original_async = fakes["AsyncResponses"].create

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    assert getattr(fakes["Responses"].create, "__inkfoot_shim__", False) is True
    assert fakes["Responses"].create is not original_sync
    assert fakes["AsyncResponses"].create is not original_async

    instrument_mod.shutdown()
    assert fakes["Responses"].create is original_sync
    assert fakes["AsyncResponses"].create is original_async


def test_chat_and_responses_shims_install_together(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    assert getattr(fakes["Completions"].create, "__inkfoot_shim__", False)
    assert getattr(fakes["Responses"].create, "__inkfoot_shim__", False)


def test_downlevel_sdk_without_responses_surface_is_skipped(tmp_path) -> None:
    fakes = install_fake_openai(with_responses=False)
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    # Must not raise; the chat shim still installs.
    instrument_mod.instrument(storage=storage)
    assert getattr(fakes["Completions"].create, "__inkfoot_shim__", False)
    assert not getattr(fakes["Responses"].create, "__inkfoot_shim__", False)


def test_async_wrapper_is_coroutine_function(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    assert inspect.iscoroutinefunction(fakes["AsyncResponses"].create)


def test_one_event_per_sync_call(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["Responses"]()
    for _ in range(3):
        client.create(model="gpt-4o", input="hi")

    assert len(_llm_call_payloads(storage, run_id)) == 3


def test_one_event_per_async_call(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["AsyncResponses"]()

    async def runner() -> None:
        for _ in range(2):
            await client.create(model="gpt-4o", input="hi")

    asyncio.run(runner())
    assert len(_llm_call_payloads(storage, run_id)) == 2


def test_event_carries_responses_usage_mapping(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Responses"]().create(
        model="gpt-4o",
        instructions="terse",
        input="hello there",
    )

    payloads = _llm_call_payloads(storage, run_id)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-4o"
    # The fake reports usage.output_tokens=5 in Responses shape.
    assert payload["ledger"]["output_tokens"] == 5
    assert payload["ledger"]["user_input_tokens"] > 0


def test_response_is_returned_unmodified(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed(storage)

    client = fakes["Responses"]()
    result = client.create(model="gpt-4o", input="hi")
    assert result["output"][0]["content"][0]["text"] == "ack"
    assert result["usage"]["output_tokens"] == 5
    assert result["id"].startswith("resp_fake_")


def test_provider_error_propagates_and_lands_one_error_event(
    tmp_path,
) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    class _FakeAPIError(Exception):
        pass

    fakes["responses_errors"].append(_FakeAPIError("rate limited"))
    with pytest.raises(_FakeAPIError, match="rate limited"):
        fakes["Responses"]().create(model="gpt-4o", input="hi")

    payloads = _llm_call_payloads(storage, run_id)
    assert len(payloads) == 1
    assert payloads[0]["error"]["type"] == "_FakeAPIError"
    assert "rate limited" in payloads[0]["error"]["message"]


def test_double_install_is_idempotent(tmp_path) -> None:
    from inkfoot.shims.openai_responses import OpenAIResponsesShim

    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()

    first = OpenAIResponsesShim(storage, lambda: "metadata")
    assert first.install() is True
    patched = fakes["Responses"].create
    second = OpenAIResponsesShim(storage, lambda: "metadata")
    assert second.install() is True
    # The second install saw the existing patch and left it alone.
    assert fakes["Responses"].create is patched
    first.uninstall()
    storage.close()
