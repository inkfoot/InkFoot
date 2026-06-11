"""End-to-end: ``LazyToolExposure`` through the Anthropic shim.

A stale tool is dropped from the outgoing request the SDK actually
receives; referencing it in assistant text brings it back on the next
turn; both transitions land as events in storage.
"""

from __future__ import annotations

import json

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy import (
    IntegrationPattern,
    LazyToolExposure,
    register_policies,
)
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


SEARCH = {"name": "search", "description": "web search", "input_schema": {}}
CALC = {"name": "calc", "description": "calculator", "input_schema": {}}


def _tools() -> list[dict]:
    # Fresh dicts per turn, like a framework re-supplying its registry.
    return [dict(SEARCH), dict(CALC)]


def _sent_tool_names(call: dict) -> list[str]:
    return [t["name"] for t in call["kwargs"]["tools"]]


def _policy_events(storage: SQLiteStorage, run_id: str, kind: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == kind
    ]


def test_stale_tool_dropped_then_restored_on_reference(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    register_policies(
        [LazyToolExposure(stale_after_turns=1)],
        active_pattern=IntegrationPattern.C,
    )

    question = {"role": "user", "content": "use search to find the report"}

    with inkfoot.agent_run(task="research") as run:
        run_id = run.id
        # Turns 1-2: calc inside its relevance window.
        for _ in range(2):
            fakes["Messages"]().create(
                model="claude-sonnet-4-6",
                messages=[dict(question)],
                tools=_tools(),
            )
        # Turn 3: calc has been idle past the window.
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=[dict(question)],
            tools=_tools(),
        )
        # Turn 4: the assistant says it needs calc -> restore.
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=[
                dict(question),
                {
                    "role": "assistant",
                    "content": "I'd need the calc tool to total these.",
                },
            ],
            tools=_tools(),
        )

    sent = [_sent_tool_names(c) for c in fakes["calls"]]
    assert sent[0] == ["search", "calc"]
    assert sent[1] == ["search", "calc"]
    assert sent[2] == ["search"]  # calc dropped from the real request
    assert sent[3] == ["search", "calc"]  # restored after the reference

    dropped = _policy_events(storage, run_id, "lazy_tool_dropped")
    assert dropped == [{"dropped": ["calc"], "turn": 3}]
    restored = _policy_events(storage, run_id, "lazy_tool_restored")
    assert restored == [{"restored": ["calc"], "turn": 4}]


def test_called_tool_stays_exposed_across_turns(tmp_path) -> None:
    """A tool the model keeps invoking is never dropped, with the
    relevance refresh coming from the response's tool_use blocks."""
    fakes = install_fake_anthropic()

    # Make the fake SDK answer with a tool_use block for calc. Must
    # happen before instrument() so the shim wraps *this* method.
    original_create = fakes["Messages"].create

    def create_with_tool_use(self, *args, **kwargs):
        original_create(self, *args, **kwargs)
        return {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "content": [{"type": "tool_use", "name": "calc", "input": {}}],
        }

    fakes["Messages"].create = create_with_tool_use

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    register_policies(
        [LazyToolExposure(stale_after_turns=1)],
        active_pattern=IntegrationPattern.C,
    )

    with inkfoot.agent_run(task="research"):
        for _ in range(4):
            fakes["Messages"]().create(
                model="claude-sonnet-4-6",
                messages=[
                    {"role": "user", "content": "use search to find it"}
                ],
                tools=_tools(),
            )

    for call in fakes["calls"]:
        assert "calc" in _sent_tool_names(call)
