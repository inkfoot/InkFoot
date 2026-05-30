"""``inkfoot report --run <id> --group-by node``

Tests the per-node slice of the single-run report. Builds a fixture
DB with two LLM calls tagged with different ``node_name`` values
and asserts the render lays them out one row per node with sensible
aggregates.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from inkfoot.cli.report import run as report_run
from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.storage.sqlite import SQLiteStorage


def _ledger(input_tokens: int, output_tokens: int) -> CausalTokenLedger:
    """Minimal ledger — only the fields the per-node summary reads."""
    return CausalTokenLedger(
        system_static_tokens=0,
        system_dynamic_tokens=0,
        user_input_tokens=input_tokens,
        tool_schema_tokens=0,
        tool_result_tokens=0,
        retrieved_context_tokens=0,
        memory_tokens=0,
        retry_overhead_tokens=0,
        summariser_tokens=0,
        reasoning_tokens=0,
        guardrail_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=output_tokens,
    )


def _seed(db_path: Path) -> str:
    """Two LLM-call events on one run, different node_names."""
    s = SQLiteStorage(path=db_path)
    s.connect()
    run_id = "r1"
    s.start_run(
        run_id=run_id,
        task="lg-task",
        agent_kind="langgraph",
        started_at=1,
    )
    for seq, (node, in_tok, out_tok) in enumerate(
        [("retrieve", 100, 20), ("synthesise", 200, 50)], start=1
    ):
        call = NeutralCall(
            provider="openai",
            model="gpt-4o-mini",
            started_at=1,
            ended_at=2,
            ledger=_ledger(in_tok, out_tok),
            metadata={"node_name": node},
        )
        s.insert_event(
            event_id=f"e{seq}",
            run_id=run_id,
            kind="llm_call",
            occurred_at=2,
            sequence=seq,
            payload_json=json.dumps(asdict(call), default=str),
        )
    s.end_run(run_id=run_id, ended_at=3, status="complete")
    s.close()
    return run_id


def test_group_by_node_prints_one_row_per_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "runs.db"
    run_id = _seed(db_path)

    args = SimpleNamespace(
        db=str(db_path),
        run=run_id,
        last=None,
        task=None,
        group_by="node",
        show_zero=False,
    )
    rc = report_run(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "retrieve" in out
    assert "synthesise" in out
    # The header column is present.
    assert "input_tok" in out
    # Synthesise should sit first (higher cost — heavier ledger).
    retrieve_idx = out.index("retrieve")
    synthesise_idx = out.index("synthesise")
    assert synthesise_idx < retrieve_idx, (
        "expected the more-expensive node to sort first"
    )


def test_group_by_node_with_untagged_calls_shows_no_node_bucket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "runs.db"
    s = SQLiteStorage(path=db_path)
    s.connect()
    s.start_run(run_id="r2", task="t", agent_kind="raw", started_at=1)
    call = NeutralCall(
        provider="openai",
        model="gpt-4o-mini",
        started_at=1,
        ended_at=2,
        ledger=_ledger(10, 5),
        metadata={},  # no node_name
    )
    s.insert_event(
        event_id="e1",
        run_id="r2",
        kind="llm_call",
        occurred_at=2,
        sequence=1,
        payload_json=json.dumps(asdict(call), default=str),
    )
    s.end_run(run_id="r2", ended_at=3, status="complete")
    s.close()

    args = SimpleNamespace(
        db=str(db_path),
        run="r2",
        last=None,
        task=None,
        group_by="node",
        show_zero=False,
    )
    report_run(args)
    out = capsys.readouterr().out
    assert "(no node)" in out


def test_group_by_node_on_run_without_llm_calls_shows_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "runs.db"
    s = SQLiteStorage(path=db_path)
    s.connect()
    s.start_run(run_id="r3", task="t", agent_kind="raw", started_at=1)
    s.end_run(run_id="r3", ended_at=2, status="complete")
    s.close()

    args = SimpleNamespace(
        db=str(db_path),
        run="r3",
        last=None,
        task=None,
        group_by="node",
        show_zero=False,
    )
    report_run(args)
    out = capsys.readouterr().out
    assert "No node-tagged LLM calls" in out
    assert "inkfoot.langgraph.instrument" in out


def test_group_by_node_rejected_on_aggregate_view(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "runs.db"
    s = SQLiteStorage(path=db_path)
    s.connect()
    s.close()

    args = SimpleNamespace(
        db=str(db_path),
        run=None,
        last="7d",
        task=None,
        group_by="node",
        show_zero=False,
    )
    report_run(args)
    out = capsys.readouterr().out
    assert "only applies to a single run" in out
