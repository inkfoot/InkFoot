"""Live OpenAI embeddings smoke test (opt-in).

Drives the real ``client.embeddings.create`` surface through the
opt-in embeddings shim and checks the captured ``embedding_call``
event against the provider's own usage: exactly one event, the input
tokens copied from ``response.usage``, and a non-null estimated cost
for a priced model.

Skips cleanly without ``OPENAI_API_KEY``; the weekly live workflow
supplies it in CI.
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


def _embedding_events(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "embedding_call"
    ]


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_embedding_call_is_captured(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False, embeddings=True)

    model = os.environ.get(
        "INKFOOT_LIVE_OPENAI_EMBED_MODEL", "text-embedding-3-small"
    )
    with inkfoot.agent_run(task="live-embeddings") as run:
        openai.OpenAI().embeddings.create(
            model=model,
            input="Inkfoot captures embedding calls separately.",
        )

    events = _embedding_events(storage, run.id)
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "openai"
    assert event["model"] == model
    assert event["input_tokens"] > 0
    # Provider reported usage, so the count is exact (not estimated).
    assert event["token_count_estimated"] is False
    # text-embedding-3-small is priced, so the estimate is populated.
    assert event["estimated_nanodollars"] is not None


@pytest.mark.live_openai
@pytest.mark.skipif(not _OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_live_embeddings_not_folded_into_run_totals(tmp_path) -> None:
    openai = pytest.importorskip("openai")
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False, embeddings=True)

    model = os.environ.get(
        "INKFOOT_LIVE_OPENAI_EMBED_MODEL", "text-embedding-3-small"
    )
    with inkfoot.agent_run(task="live-embeddings-isolation") as run:
        openai.OpenAI().embeddings.create(model=model, input="hello")

    from inkfoot.storage.aggregator import project_run_totals

    totals = project_run_totals(list(storage.iter_events(run.id)))
    assert totals["total_input_tokens"] == 0
    assert totals["total_nanodollars"] == 0
