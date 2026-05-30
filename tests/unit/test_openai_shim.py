"""OpenAIShim tests — same axes as the Anthropic shim tests."""

from __future__ import annotations

import asyncio
import inspect

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


def _events_count(storage: SQLiteStorage, run_id: str) -> int:
    return sum(1 for _ in storage.iter_events(run_id))


def test_install_replaces_create_and_uninstall_restores(tmp_path) -> None:
    fakes = install_fake_openai()
    original_sync = fakes["Completions"].create

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    assert getattr(fakes["Completions"].create, "__inkfoot_shim__", False) is True
    assert fakes["Completions"].create is not original_sync

    instrument_mod.shutdown()
    assert fakes["Completions"].create is original_sync


def test_async_wrapper_is_coroutine_function(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    assert inspect.iscoroutinefunction(fakes["AsyncCompletions"].create)


def test_one_event_per_sync_call(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["Completions"]()
    for _ in range(3):
        client.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert _events_count(storage, run_id) == 3


def test_one_event_per_async_call(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["AsyncCompletions"]()

    async def runner() -> None:
        for _ in range(2):
            await client.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )

    asyncio.run(runner())
    assert _events_count(storage, run_id) == 2


def test_response_is_returned_unmodified(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed(storage)

    client = fakes["Completions"]()
    result = client.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result["choices"][0]["message"]["content"] == "ack"
    assert result["usage"]["completion_tokens"] == 5
