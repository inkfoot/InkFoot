"""End-to-end: ``CheapSummariser`` through the Anthropic shim.

Exercises the full wiring — the policy rewrites the oversized tool
result on the outgoing request, its helper call rides through the
same shim (re-entrancy guard + ledger re-attribution), the raw result
is preserved in the ``summariser_replaced`` event, and the content
cache absorbs the resend on the next turn.
"""

from __future__ import annotations

import json

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy import CheapSummariser, IntegrationPattern, register_policies
from inkfoot.policy.cheap_summariser import _clear_disabled_tasks
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks


THRESHOLD = 50
BIG_TEXT = " ".join(f"row-{i} status=ok latency={i % 97}ms" for i in range(200))


@pytest.fixture(autouse=True)
def reset_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    _clear_disabled_tasks()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    _clear_disabled_tasks()
    uninstall_fake_sdks()


def _request_messages(result_text: str) -> list[dict]:
    return [
        {"role": "user", "content": "what failed in the deploy?"},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": result_text,
                }
            ],
        },
    ]


def _events(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return list(storage.iter_events(run_id))


def _setup(tmp_path) -> tuple[dict, SQLiteStorage, CheapSummariser]:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    register_policies([policy], active_pattern=IntegrationPattern.C)
    return fakes, storage, policy


def test_oversized_result_is_summarised_through_the_shim(tmp_path) -> None:
    fakes, storage, _ = _setup(tmp_path)

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        run_id = run.id

    # Two SDK calls: the helper call first (it fires inside
    # before_call of the outer one), then the rewritten outer call.
    assert len(fakes["calls"]) == 2
    helper, outer = fakes["calls"]

    assert helper["kwargs"]["model"] == "claude-haiku-4-5"
    assert BIG_TEXT in helper["kwargs"]["messages"][0]["content"]

    # The fake SDK answers "ack" — that became the tool result.
    block = outer["kwargs"]["messages"][1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"] == "ack"

    kinds = [
        ev["kind"]
        for ev in _events(storage, run_id)
        if ev["kind"] in ("llm_call", "summariser_replaced")
    ]
    assert kinds == ["llm_call", "summariser_replaced", "llm_call"]


def test_helper_call_tokens_land_in_summariser_category(tmp_path) -> None:
    fakes, storage, _ = _setup(tmp_path)

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        run_id = run.id

    payloads = [
        json.loads(ev["payload_json"])
        for ev in _events(storage, run_id)
        if ev["kind"] == "llm_call"
    ]
    helper = next(p for p in payloads if p["model"] == "claude-haiku-4-5")
    outer = next(p for p in payloads if p["model"] == "claude-sonnet-4-6")

    # Helper call: full structural input re-attributed to the
    # summariser category; flagged in metadata for report roll-ups.
    assert helper["ledger"]["summariser_tokens"] > 0
    assert helper["ledger"]["user_input_tokens"] == 0
    assert helper["metadata"]["summariser_call"] is True

    # The user's own call is untouched by the re-attribution.
    assert outer["ledger"]["summariser_tokens"] == 0
    assert "summariser_call" not in outer["metadata"]


def test_raw_result_is_preserved_for_replay(tmp_path) -> None:
    fakes, storage, _ = _setup(tmp_path)

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        run_id = run.id

    replaced = [
        json.loads(ev["payload_json"])
        for ev in _events(storage, run_id)
        if ev["kind"] == "summariser_replaced"
    ]
    assert len(replaced) == 1
    payload = replaced[0]
    assert payload["raw"] == BIG_TEXT
    assert payload["summariser_model"] == "claude-haiku-4-5"
    assert payload["tool_id"] == "toolu_1"
    assert payload["original_tokens"] > THRESHOLD


def test_resent_result_is_served_from_cache(tmp_path) -> None:
    """Turn 2 resends the same raw result in history: the cached
    summary is swapped in without a second helper call or event."""
    fakes, storage, _ = _setup(tmp_path)

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        run_id = run.id

    # 3 SDK calls total: helper + outer (turn 1), outer (turn 2).
    assert len(fakes["calls"]) == 3
    turn2 = fakes["calls"][2]
    assert turn2["kwargs"]["messages"][1]["content"][0]["content"] == "ack"

    kinds = [ev["kind"] for ev in _events(storage, run_id)]
    assert kinds.count("summariser_replaced") == 1
    assert kinds.count("llm_call") == 3


def test_tag_kill_switch_disables_summarisation_in_run(tmp_path) -> None:
    fakes, storage, _ = _setup(tmp_path)

    with inkfoot.agent_run(task="triage"):
        inkfoot.tag("disable_summariser", True)
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )

    # No helper call; the raw result reached the SDK unchanged.
    assert len(fakes["calls"]) == 1
    block = fakes["calls"][0]["kwargs"]["messages"][1]["content"][0]
    assert block["content"] == BIG_TEXT
