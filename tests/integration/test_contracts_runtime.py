"""End-to-end: a real contract YAML enforced through the Anthropic shim.

Exercises the full wiring — ``instrument(contracts=[...])`` installs
the enforcer, ``agent_run`` registers the run, and the shim hot path
applies the degrade ladder: ``switch_to_cheap_model`` rewrites the
outgoing model and ``block`` raises ``PolicyBlocked`` before the SDK
call is made. Violation events land in storage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.errors import PolicyBlocked
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


def _write_contract(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "triage.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _violations(storage: SQLiteStorage, run_id: str) -> list[dict]:
    out = []
    for ev in storage.iter_events(run_id):
        if ev.get("kind") == "contract_violation":
            out.append(json.loads(ev["payload_json"]))
    return out


_SWITCH_CONTRACT = """
schema_version: 1
task: triage
cheap_model: claude-haiku-4-5
budget:
  max_llm_calls: 1
degrade:
  - at_percent: 90
    action: switch_to_cheap_model
"""

_BLOCK_CONTRACT = """
schema_version: 1
task: triage
budget:
  max_llm_calls: 1
degrade:
  - at_percent: 100
    action: block
"""


def test_switch_to_cheap_model_rewrites_outgoing_call(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage, contracts=[_write_contract(tmp_path, _SWITCH_CONTRACT)]
    )

    with inkfoot.agent_run(task="triage") as run:
        fakes["Messages"]().create(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hello"}],
        )
        run_id = run.id

    # The SDK actually received the cheap model, not the requested one.
    assert fakes["calls"][0]["kwargs"]["model"] == "claude-haiku-4-5"
    violations = _violations(storage, run_id)
    assert any(v["action"] == "switch_to_cheap_model" for v in violations)


def test_block_raises_policy_blocked_and_skips_sdk_call(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage, contracts=[_write_contract(tmp_path, _BLOCK_CONTRACT)]
    )

    run_id = None
    with pytest.raises(PolicyBlocked) as exc_info:
        with inkfoot.agent_run(task="triage") as run:
            run_id = run.id
            fakes["Messages"]().create(
                model="claude-opus-4-7",
                messages=[{"role": "user", "content": "hello"}],
            )

    # The SDK call never happened.
    assert fakes["calls"] == []
    assert exc_info.value.clause == "max_llm_calls"
    violations = _violations(storage, run_id)
    assert any(v["action"] == "block" for v in violations)


def test_recent_outcomes_excludes_current_run(tmp_path) -> None:
    # The advisory outcome window must not double-count the run whose
    # outcome was just set. _recent_outcomes excludes it by id so the
    # caller can prepend the live outcome deterministically, regardless
    # of whether the background aggregator has projected it yet.
    from inkfoot.contracts.runtime import _recent_outcomes

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    conn = storage._conn()
    for i, oc in enumerate(["success", "failure", "success"]):
        conn.execute(
            "INSERT INTO runs (id, task, started_at, status, outcome) "
            "VALUES (?, 'triage', ?, 'complete', ?)",
            [f"run-{i}", 1_700_000_000_000 + i, oc],
        )

    # Excluding the most-recent run (run-2, the "current" one) drops it
    # from the projection read entirely.
    recent = _recent_outcomes(storage, "triage", 10, exclude_run_id="run-2")
    assert recent == ["failure", "success"]

    # Without exclusion, all three are returned (newest first).
    assert _recent_outcomes(storage, "triage", 10) == [
        "success",
        "failure",
        "success",
    ]
    storage.close()


def test_untracked_task_is_unaffected(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(
        storage=storage, contracts=[_write_contract(tmp_path, _BLOCK_CONTRACT)]
    )

    # A task with no contract sails through untouched.
    with inkfoot.agent_run(task="some-other-task"):
        fakes["Messages"]().create(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hello"}],
        )
    assert fakes["calls"][0]["kwargs"]["model"] == "claude-opus-4-7"
