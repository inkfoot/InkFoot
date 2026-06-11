"""End-to-end: a Token Contract and ``CheapSummariser`` on the same task.

The summariser's helper call is policy infrastructure that exists to
*reduce* spend, so the contract enforcer exempts it: the helper is
never gated or blocked, and it doesn't advance ``max_llm_calls`` —
only the user's own agent calls consume the call budget. The helper's
real spend still folds into the run's running spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.errors import PolicyBlocked
from inkfoot.policy import CheapSummariser, IntegrationPattern, register_policies
from inkfoot.policy.cheap_summariser import _clear_disabled_tasks
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks

THRESHOLD = 50
BIG_TEXT = " ".join(f"row-{i} status=ok latency={i % 97}ms" for i in range(200))

_TWO_CALL_BLOCK_CONTRACT = """
schema_version: 1
task: triage
budget:
  max_llm_calls: 2
degrade:
  - at_percent: 100
    action: block
"""

_ONE_CALL_BLOCK_CONTRACT = """
schema_version: 1
task: triage
budget:
  max_llm_calls: 1
degrade:
  - at_percent: 100
    action: block
"""


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


def _write_contract(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "triage.yaml"
    p.write_text(body, encoding="utf-8")
    return p


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


def _violations(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev.get("kind") == "contract_violation"
    ]


def _setup(tmp_path, contract_body: str) -> tuple[dict, SQLiteStorage]:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage,
        contracts=[_write_contract(tmp_path, contract_body)],
    )
    register_policies(
        [CheapSummariser(threshold_tokens=THRESHOLD)],
        active_pattern=IntegrationPattern.C,
    )
    return fakes, storage


def test_helper_call_does_not_consume_the_call_budget(tmp_path) -> None:
    """With ``max_llm_calls: 2``, one oversized request produces two
    SDK calls (helper + the rewritten agent call). Only the agent call
    counts — it projects 1/2 = 50% and goes through unblocked."""
    fakes, storage = _setup(tmp_path, _TWO_CALL_BLOCK_CONTRACT)

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-sonnet-4-6",
            messages=_request_messages(BIG_TEXT),
        )
        run_id = run.id

    assert [c["kwargs"]["model"] for c in fakes["calls"]] == [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
    ]
    # The agent call carried the summary ("ack" from the fake SDK) —
    # summarised, not blocked and not degraded to truncation.
    block = fakes["calls"][1]["kwargs"]["messages"][1]["content"][0]
    assert block["content"] == "ack"
    assert not any(v["action"] == "block" for v in _violations(storage, run_id))


def test_second_agent_call_still_hits_the_ceiling(tmp_path) -> None:
    """The exemption covers the helper only: the user's second agent
    call projects 2/2 = 100% and blocks as the contract demands."""
    fakes, _ = _setup(tmp_path, _TWO_CALL_BLOCK_CONTRACT)

    with pytest.raises(PolicyBlocked) as exc_info:
        with inkfoot.agent_run(task="triage"):
            fakes["Messages"]().create(
                model="claude-sonnet-4-6",
                messages=_request_messages(BIG_TEXT),
            )
            fakes["Messages"]().create(
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": "and the fix?"}],
            )

    assert exc_info.value.clause == "max_llm_calls"
    # Helper + first agent call only; the blocked call never went out.
    assert len(fakes["calls"]) == 2


def test_helper_is_not_blocked_at_the_ceiling(tmp_path) -> None:
    """With ``max_llm_calls: 1`` every agent call blocks at the
    ceiling — but the helper (fired during ``before_call``, ahead of
    enforcement) is exempt and reaches the SDK instead of silently
    degrading the summary to truncation."""
    fakes, _ = _setup(tmp_path, _ONE_CALL_BLOCK_CONTRACT)

    with pytest.raises(PolicyBlocked) as exc_info:
        with inkfoot.agent_run(task="triage"):
            fakes["Messages"]().create(
                model="claude-sonnet-4-6",
                messages=_request_messages(BIG_TEXT),
            )

    assert exc_info.value.clause == "max_llm_calls"
    # The only SDK call that happened is the helper's cheap-model call.
    assert [c["kwargs"]["model"] for c in fakes["calls"]] == ["claude-haiku-4-5"]
