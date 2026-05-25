"""AnthropicShim tests (E3-S2 acceptance).

Covers:
- After install, ``Messages.create`` is the shim; the original
  pointer is recoverable via uninstall.
- One event per call: events grows by exactly N after N sync calls;
  same for async calls.
- The async wrapper is itself a coroutine function.
- Hook isolation: a raising policy doesn't propagate; the user's
  call returns the original response unchanged.
- Replay mode: with ``capture_mode="replay"`` set, an
  ``event_contents`` row is written per ``llm_call`` event; with
  the default ``"metadata"`` mode, zero content rows are written
  for the same workload.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _reset_ambient_run,
    _set_current_run,
    _clear_current_run,
)
from inkfoot.policy import CallContext, IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_anthropic,
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


def _seed_run(storage: SQLiteStorage, run_id: str = "test-run") -> str:
    # Ensure migrations are applied — instrument() does this too,
    # but tests that seed before calling instrument() need the
    # schema present.
    storage.connect()
    storage.start_run(
        run_id=run_id,
        task="test",
        agent_kind="unit",
        started_at=1_700_000_000_000,
    )
    return run_id


def _events_count(storage: SQLiteStorage, run_id: str) -> int:
    return sum(1 for _ in storage.iter_events(run_id))


def _content_rows_count(storage: SQLiteStorage) -> int:
    conn = storage._conn()
    return conn.execute("SELECT COUNT(*) FROM event_contents").fetchone()[0]


# ----------------------------------------------------------------------
# Install / uninstall mechanics
# ----------------------------------------------------------------------


def test_install_replaces_create_and_uninstall_restores(tmp_path) -> None:
    fakes = install_fake_anthropic()
    original_sync = fakes["Messages"].create
    original_async = fakes["AsyncMessages"].create

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot_inst = instrument_mod
    inkfoot_inst.instrument(storage=storage)

    # After install: Messages.create is the shim wrapper.
    assert getattr(fakes["Messages"].create, "__inkfoot_shim__", False) is True
    assert fakes["Messages"].create is not original_sync

    inkfoot_inst.shutdown()

    # Original is restored.
    assert fakes["Messages"].create is original_sync
    assert fakes["AsyncMessages"].create is original_async


def test_sync_wrapper_is_a_plain_function_async_wrapper_is_coroutine(
    tmp_path,
) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)

    assert not inspect.iscoroutinefunction(fakes["Messages"].create)
    assert inspect.iscoroutinefunction(fakes["AsyncMessages"].create)


# ----------------------------------------------------------------------
# Event emit
# ----------------------------------------------------------------------


def test_one_event_per_sync_call(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed_run(storage)
    _set_current_run(run_id)

    client_self = object()
    n_calls = 5
    for _ in range(n_calls):
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert _events_count(storage, run_id) == n_calls


def test_one_event_per_async_call(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed_run(storage)
    _set_current_run(run_id)

    client = fakes["AsyncMessages"]()
    n_calls = 4

    async def runner() -> None:
        for _ in range(n_calls):
            await client.create(
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": "hi"}],
            )

    asyncio.run(runner())
    assert _events_count(storage, run_id) == n_calls


def test_response_is_returned_unmodified(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)

    client = fakes["Messages"]()
    result = client.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
    )
    # Fake returns a dict with usage + content; the shim must hand
    # it back untouched.
    assert result["content"] == [{"type": "text", "text": "ack"}]
    assert result["usage"]["output_tokens"] == 5


# ----------------------------------------------------------------------
# Hook isolation
# ----------------------------------------------------------------------


def test_raising_policy_does_not_propagate(tmp_path) -> None:
    class BadPolicy(Policy):
        NAME = "BadPolicy"
        SUPPORTED_PATTERNS = {IntegrationPattern.A}

        def before_call(self, ctx: CallContext) -> PolicyDecision:
            raise RuntimeError("policy is busted")

        def after_call(self, ctx: CallContext, response: Any) -> None:
            raise RuntimeError("after also busted")

    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _seed_run(storage)
    instrument_mod.instrument(storage=storage, policies=[BadPolicy()])
    _set_current_run("test-run")

    # User call must succeed; provider response comes back.
    client = fakes["Messages"]()
    result = client.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result["usage"]["output_tokens"] == 5
    # An event was still written (the after_call raise didn't block emit).
    assert _events_count(storage, "test-run") == 1


# ----------------------------------------------------------------------
# Replay mode
# ----------------------------------------------------------------------


def test_metadata_mode_writes_zero_content_rows(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _seed_run(storage)
    instrument_mod.instrument(storage=storage)  # default capture_mode
    _set_current_run("test-run")

    client = fakes["Messages"]()
    for _ in range(3):
        client.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "x"}],
        )

    assert _events_count(storage, "test-run") == 3
    assert _content_rows_count(storage) == 0


def test_replay_mode_writes_one_content_row_per_event(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _seed_run(storage)
    instrument_mod.instrument(storage=storage, capture_mode="replay")
    _set_current_run("test-run")

    client = fakes["Messages"]()
    n_calls = 4
    for _ in range(n_calls):
        client.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "x"}],
        )

    assert _events_count(storage, "test-run") == n_calls
    assert _content_rows_count(storage) == n_calls

    # Every content row joins 1:1 to its event by id and has non-null
    # request_json + response_json.
    conn = storage._conn()
    rows = conn.execute(
        """
        SELECT ec.event_id, ec.request_json, ec.response_json
        FROM event_contents ec
        JOIN events e ON e.id = ec.event_id
        WHERE e.run_id = 'test-run' AND e.kind = 'llm_call'
        """
    ).fetchall()
    assert len(rows) == n_calls
    for row in rows:
        assert row["request_json"] is not None
        assert row["response_json"] is not None
