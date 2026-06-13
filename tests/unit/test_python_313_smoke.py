"""End-to-end smoke test, exercised by the Python 3.13 CI leg.

The unit suite covers each seam in isolation; this drives the whole
pipeline once — instrument → chat call → embedding call → render —
so the 3.13 matrix leg fails loudly if a transitive dependency or a
language-level change breaks the happy path on the newest interpreter.
It runs on every supported version too; 3.13 is simply where it
matters most.
"""

from __future__ import annotations

import json
import sys

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.cli.report import (
    _aggregate_embedding_totals,
    _aggregate_ledger_totals,
    render,
)
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


def test_supported_interpreter() -> None:
    # The packaging floor is 3.10; CI additionally pins a 3.13 leg.
    assert sys.version_info >= (3, 10)


def test_end_to_end_chat_and_embeddings(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage, sdks=["openai"], langchain=False, embeddings=True
    )

    with inkfoot.agent_run(task="smoke") as run:
        fakes["OpenAI"]().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hello"}],
        )
        fakes["OpenAI"]().embeddings.create(
            model="text-embedding-3-small",
            input=["chunk one", "chunk two"],
        )

    events = list(storage.iter_events(run.id))
    kinds = [ev["kind"] for ev in events]
    assert kinds.count("llm_call") == 1
    assert kinds.count("embedding_call") == 1

    # Render the report end to end and confirm both the causal chart
    # and the separate embeddings section show up.
    ledger_totals = _aggregate_ledger_totals(events)
    embeddings = _aggregate_embedding_totals(events)
    row = storage.get_run(run.id)
    out = render(
        run=row, ledger_totals=ledger_totals, smells=[], embeddings=embeddings
    )
    assert "Causal attribution:" in out
    assert "Embeddings (separate accounting" in out
    assert embeddings["count"] == 1
    assert embeddings["by_model"][("openai", "text-embedding-3-small")]["count"] == 1
