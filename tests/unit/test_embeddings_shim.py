"""OpenAI embeddings shim tests.

The shim is opt-in (``instrument(embeddings=True)``) and records each
``embeddings.create`` call as a standalone ``embedding_call`` event —
accounted separately from the causal token ledger. These tests drive
the fake OpenAI SDK (no network, no real ``openai`` package) and
assert on the captured event shape, the reported-vs-estimated token
split, the pricing, and the invariant that embeddings never fold into
a run's token/cost totals.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    _set_current_run,
)
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.aggregator import project_run_totals
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_openai, uninstall_fake_sdks


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


def _embedding_events(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "embedding_call"
    ]


def _instrument(storage: SQLiteStorage, *, embeddings: bool) -> None:
    instrument_mod.instrument(
        storage=storage, sdks=["openai"], langchain=False, embeddings=embeddings
    )


# ----------------------------------------------------------------------
# Install / opt-in
# ----------------------------------------------------------------------


def test_embeddings_true_patches_create_and_shutdown_restores(tmp_path) -> None:
    fakes = install_fake_openai()
    original_sync = fakes["Embeddings"].create
    original_async = fakes["AsyncEmbeddings"].create

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    assert getattr(fakes["Embeddings"].create, "__inkfoot_shim__", False) is True
    assert fakes["Embeddings"].create is not original_sync

    instrument_mod.shutdown()
    assert fakes["Embeddings"].create is original_sync
    assert fakes["AsyncEmbeddings"].create is original_async


def test_off_by_default_leaves_create_untouched(tmp_path) -> None:
    fakes = install_fake_openai()
    original_sync = fakes["Embeddings"].create

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=False)

    assert fakes["Embeddings"].create is original_sync
    assert (
        getattr(fakes["Embeddings"].create, "__inkfoot_shim__", False) is False
    )


# ----------------------------------------------------------------------
# Event emission
# ----------------------------------------------------------------------


def test_one_embedding_event_per_sync_call(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["Embeddings"]()
    for _ in range(3):
        client.create(model="text-embedding-3-small", input="hello world")

    events = _embedding_events(storage, run_id)
    assert len(events) == 3
    first = events[0]
    assert first["provider"] == "openai"
    assert first["model"] == "text-embedding-3-small"
    assert first["batch_size"] == 1
    assert set(first) >= {
        "provider",
        "model",
        "input_tokens",
        "batch_size",
        "estimated_nanodollars",
    }


def test_async_embedding_call_emits_event(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    client = fakes["AsyncEmbeddings"]()

    async def runner() -> None:
        await client.create(model="text-embedding-3-small", input="hi")

    asyncio.run(runner())
    assert len(_embedding_events(storage, run_id)) == 1


def test_batch_input_records_batch_size(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(
        model="text-embedding-3-small",
        input=["one", "two", "three"],
    )
    events = _embedding_events(storage, run_id)
    assert len(events) == 1
    assert events[0]["batch_size"] == 3


def test_response_returned_unmodified(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    _set_current_run(_seed(storage))

    result = fakes["Embeddings"]().create(
        model="text-embedding-3-small", input="hello"
    )
    # The fake returns a vector payload; the shim must hand it back as-is.
    assert result["object"] == "list"
    assert len(result["data"]) == 1


# ----------------------------------------------------------------------
# Token count: reported usage vs tokeniser fallback
# ----------------------------------------------------------------------


def test_prefers_provider_reported_usage(tmp_path) -> None:
    fakes = install_fake_openai()
    fakes["embedding_options"]["include_usage"] = True
    fakes["embedding_options"]["prompt_tokens"] = 7
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(
        model="text-embedding-3-small", input="hello world"
    )
    event = _embedding_events(storage, run_id)[0]
    assert event["input_tokens"] == 7
    assert event["token_count_estimated"] is False


def test_falls_back_to_tokeniser_when_no_usage(tmp_path) -> None:
    fakes = install_fake_openai()
    fakes["embedding_options"]["include_usage"] = False
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(
        model="text-embedding-3-small", input="hello world"
    )
    event = _embedding_events(storage, run_id)[0]
    assert event["input_tokens"] > 0
    assert event["token_count_estimated"] is True


# ----------------------------------------------------------------------
# Pricing + ledger isolation
# ----------------------------------------------------------------------


def test_estimated_cost_uses_embedding_pricing(tmp_path) -> None:
    fakes = install_fake_openai()
    fakes["embedding_options"]["prompt_tokens"] = 7
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(
        model="text-embedding-3-small", input="x"
    )
    event = _embedding_events(storage, run_id)[0]
    # text-embedding-3-small lists at $0.02/Mtok = 20 nd/token.
    assert event["estimated_nanodollars"] == 7 * 20


def test_unpriced_model_records_null_cost(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(model="some-unlisted-embedder", input="x")
    event = _embedding_events(storage, run_id)[0]
    assert event["estimated_nanodollars"] is None


def test_embedding_events_excluded_from_run_totals(tmp_path) -> None:
    fakes = install_fake_openai()
    fakes["embedding_options"]["prompt_tokens"] = 7
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _instrument(storage, embeddings=True)
    run_id = _seed(storage)
    _set_current_run(run_id)

    fakes["Embeddings"]().create(model="text-embedding-3-small", input="x")

    events = list(storage.iter_events(run_id))
    totals = project_run_totals(events)
    # Embeddings must never inflate the run's input-token or cost
    # totals — they are accounted separately.
    assert totals["total_input_tokens"] == 0
    assert totals["total_nanodollars"] == 0
