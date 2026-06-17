"""Behavioral contract tests for PostgresStorage against a real server.

Opt-in: set ``INKFOOT_TEST_PG_DSN`` (e.g. a local Docker Postgres).
These tests mirror the SQLite backend's behavioral suite — the two
backends must be interchangeable from the caller's point of view:
two-tier writes, the claim-and-project lost-update guarantee, the
replay-mode content contract, and identical row-dict values.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("INKFOOT_TEST_PG_DSN"),
        reason="INKFOOT_TEST_PG_DSN not set",
    ),
]

psycopg = pytest.importorskip("psycopg")


def _seed_run(storage, run_id: str = "run-1", started_at: int = 1000) -> None:
    storage.start_run(
        run_id=run_id,
        task="demo-task",
        agent_kind="test-agent",
        started_at=started_at,
    )


# ----------------------------------------------------------------------
# Lifecycle + migrations
# ----------------------------------------------------------------------


def test_connect_is_idempotent(pg_storage) -> None:
    pg_storage.connect()  # second call must be a no-op
    assert pg_storage.schema_version() >= 1


def test_migrations_apply_once(pg_dsn) -> None:
    from inkfoot.storage.postgres_migrations import apply_migrations

    with psycopg.connect(pg_dsn) as conn:
        first = apply_migrations(conn)
    with psycopg.connect(pg_dsn) as conn:
        second = apply_migrations(conn)
    assert first == [1]
    assert second == []


def test_concurrent_connect_does_not_race_ddl(pg_dsn) -> None:
    """Two processes calling connect() at the same instant serialise
    on the migrations advisory lock instead of racing CREATE TABLE."""
    from inkfoot.storage.postgres import PostgresStorage

    errors: list[Exception] = []

    def open_one() -> None:
        storage = PostgresStorage(dsn=pg_dsn, pool_min=1, pool_max=1)
        try:
            storage.connect()
        except Exception as exc:  # pragma: no cover — failure path
            errors.append(exc)
        finally:
            storage.close()

    threads = [threading.Thread(target=open_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# ----------------------------------------------------------------------
# Run lifecycle
# ----------------------------------------------------------------------


def test_start_run_roundtrip_with_defaults(pg_storage) -> None:
    pg_storage.start_run(
        run_id="run-1",
        task="demo",
        agent_kind="kind",
        started_at=1234,
        metadata_json='{"k": "v"}',
    )
    row = pg_storage.get_run("run-1")
    assert row is not None
    assert row["id"] == "run-1"
    assert row["task"] == "demo"
    assert row["agent_kind"] == "kind"
    assert row["started_at"] == 1234
    assert row["status"] == "running"
    assert row["run_kind"] == "root"
    assert row["parent_run_id"] is None
    assert row["ended_at"] is None
    assert row["aggregates_dirty"] == 0
    assert row["metadata_json"] == '{"k": "v"}'
    for column in (
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_cache_creation_tokens",
        "total_nanodollars",
    ):
        assert row[column] == 0


def test_get_run_returns_none_for_unknown_id(pg_storage) -> None:
    assert pg_storage.get_run("nope") is None


def test_end_run_updates_status_and_timestamp(pg_storage) -> None:
    _seed_run(pg_storage)
    pg_storage.end_run(run_id="run-1", ended_at=2000, status="complete")
    row = pg_storage.get_run("run-1")
    assert row["status"] == "complete"
    assert row["ended_at"] == 2000


def test_child_run_references_parent(pg_storage) -> None:
    _seed_run(pg_storage, run_id="parent-1")
    pg_storage.start_run(
        run_id="child-1",
        task="sub",
        agent_kind=None,
        started_at=1001,
        parent_run_id="parent-1",
        run_kind="subagent",
    )
    row = pg_storage.get_run("child-1")
    assert row["parent_run_id"] == "parent-1"
    assert row["run_kind"] == "subagent"


def test_child_with_missing_parent_is_rejected_at_commit(pg_storage) -> None:
    """The parent FK is deferred, so the violation surfaces when the
    transaction commits — still inside the start_run call."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_storage.start_run(
            run_id="orphan-1",
            task=None,
            agent_kind=None,
            started_at=1,
            parent_run_id="ghost",
        )


def test_find_runs_with_status(pg_storage) -> None:
    _seed_run(pg_storage, run_id="run-a", started_at=1)
    _seed_run(pg_storage, run_id="run-b", started_at=2)
    pg_storage.end_run(run_id="run-b", ended_at=3, status="complete")
    assert pg_storage.find_runs_with_status("running") == ["run-a"]
    assert pg_storage.find_runs_with_status("complete") == ["run-b"]


# ----------------------------------------------------------------------
# Events: two-tier writes + replay-mode content contract
# ----------------------------------------------------------------------


def test_insert_event_appends_and_marks_dirty(pg_storage) -> None:
    _seed_run(pg_storage)
    pg_storage.insert_event(
        event_id="evt-1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1500,
        sequence=1,
        payload_json='{"input_tokens": 11}',
    )
    row = pg_storage.get_run("run-1")
    assert row["aggregates_dirty"] == 1
    events = list(pg_storage.iter_events("run-1"))
    assert [e["id"] for e in events] == ["evt-1"]
    assert events[0]["kind"] == "llm_call"
    assert events[0]["sequence"] == 1
    assert json.loads(events[0]["payload_json"]) == {"input_tokens": 11}


def test_iter_events_orders_by_sequence(pg_storage) -> None:
    _seed_run(pg_storage)
    for sequence in (3, 1, 2):
        pg_storage.insert_event(
            event_id=f"evt-{sequence}",
            run_id="run-1",
            kind="llm_call",
            occurred_at=1500,
            sequence=sequence,
        )
    assert [e["sequence"] for e in pg_storage.iter_events("run-1")] == [
        1,
        2,
        3,
    ]


def _content_row_count(pg_dsn: str, event_id: str) -> int:
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM event_contents WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        return int(row[0])


def test_metadata_mode_suppresses_content_row(pg_storage, pg_dsn) -> None:
    _seed_run(pg_storage)
    pg_storage.insert_event(
        event_id="evt-1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        capture_mode="metadata",
        request_json='{"messages": []}',
    )
    assert _content_row_count(pg_dsn, "evt-1") == 0


def test_replay_mode_with_content_writes_sibling_row(
    pg_storage, pg_dsn
) -> None:
    _seed_run(pg_storage)
    pg_storage.insert_event(
        event_id="evt-1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        capture_mode="replay",
        request_json='{"messages": ["hi"]}',
        response_json='{"content": "hello"}',
        content_redacted=True,
    )
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT request_json, response_json, tool_result_json,"
            " content_redacted FROM event_contents WHERE event_id = %s",
            ("evt-1",),
        ).fetchone()
    assert row == ('{"messages": ["hi"]}', '{"content": "hello"}', None, 1)


def test_replay_mode_without_content_skips_sibling_row(
    pg_storage, pg_dsn
) -> None:
    _seed_run(pg_storage)
    pg_storage.insert_event(
        event_id="evt-1",
        run_id="run-1",
        kind="budget_warning",
        occurred_at=1,
        sequence=1,
        capture_mode="replay",
        payload_json='{"reason": "over budget"}',
    )
    assert _content_row_count(pg_dsn, "evt-1") == 0


def test_redaction_hook_masks_content_before_write(
    pg_storage, pg_dsn
) -> None:
    """Cross-backend parity: the floor masks replay content on Postgres
    exactly as it does on SQLite — no secret reaches the content row,
    and ``content_redacted`` is set."""
    from inkfoot.storage.redaction import default_redactor

    pg_storage.set_redaction_hook(default_redactor())
    _seed_run(pg_storage)
    pg_storage.insert_event(
        event_id="evt-1",
        run_id="run-1",
        kind="llm_call",
        occurred_at=1,
        sequence=1,
        capture_mode="replay",
        request_json='{"system": "mail alice@example.com"}',
        response_json='{"text": "ok"}',
    )
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT request_json, content_redacted "
            "FROM event_contents WHERE event_id = %s",
            ("evt-1",),
        ).fetchone()
    assert row is not None
    assert "alice@example.com" not in row[0]
    assert "[REDACTED:email]" in row[0]
    assert row[1] == 1


# ----------------------------------------------------------------------
# Dirty queue + claim-and-project
# ----------------------------------------------------------------------


def test_read_dirty_orders_by_started_at_and_respects_limit(
    pg_storage,
) -> None:
    for index, started in enumerate((300, 100, 200)):
        run_id = f"run-{index}"
        _seed_run(pg_storage, run_id=run_id, started_at=started)
        pg_storage.mark_dirty(run_id)
    assert pg_storage.read_dirty(limit=2) == ["run-1", "run-2"]
    assert pg_storage.read_dirty() == ["run-1", "run-2", "run-0"]


def test_claim_clean_is_an_atomic_cas(pg_storage) -> None:
    _seed_run(pg_storage)
    pg_storage.mark_dirty("run-1")
    assert pg_storage.claim_clean("run-1") is True
    assert pg_storage.claim_clean("run-1") is False


def test_insert_event_re_dirties_after_claim(pg_storage) -> None:
    """The never-lost-update guarantee: an event landing after the
    claim re-dirties the run, so the next sweep re-projects it."""
    _seed_run(pg_storage)
    pg_storage.mark_dirty("run-1")
    assert pg_storage.claim_clean("run-1")
    pg_storage.insert_event(
        event_id="late-evt",
        run_id="run-1",
        kind="llm_call",
        occurred_at=2,
        sequence=2,
    )
    assert pg_storage.read_dirty() == ["run-1"]


def test_concurrent_claims_yield_exactly_one_winner(pg_storage) -> None:
    _seed_run(pg_storage)
    pg_storage.mark_dirty("run-1")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        barrier.wait()
        results.append(pg_storage.claim_clean("run-1"))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]


def test_write_totals_roundtrips_all_projection_columns(pg_storage) -> None:
    _seed_run(pg_storage)
    totals = {
        "total_input_tokens": 123,
        "total_output_tokens": 456,
        "total_cache_read_tokens": 7,
        "total_cache_creation_tokens": 8,
        "total_nanodollars": 5_000_000_000,  # exceeds int32 on purpose
        "outcome": "success",
        "quality_score": 0.875,
    }
    pg_storage.write_totals(run_id="run-1", totals=totals)
    row = pg_storage.get_run("run-1")
    for key, expected in totals.items():
        assert row[key] == expected


def test_update_aggregates_only_writes_when_dirty(pg_storage) -> None:
    _seed_run(pg_storage)
    assert (
        pg_storage.update_aggregates(
            run_id="run-1", totals={"total_input_tokens": 5}
        )
        is False
    )
    pg_storage.mark_dirty("run-1")
    assert (
        pg_storage.update_aggregates(
            run_id="run-1", totals={"total_input_tokens": 5}
        )
        is True
    )
    assert pg_storage.get_run("run-1")["total_input_tokens"] == 5


def test_mark_all_dirty_counts_rows(pg_storage) -> None:
    for index in range(3):
        _seed_run(pg_storage, run_id=f"run-{index}", started_at=index)
    assert pg_storage.mark_all_dirty() == 3
    assert len(pg_storage.read_dirty()) == 3


# ----------------------------------------------------------------------
# End-to-end: the in-process drain engine against Postgres
# ----------------------------------------------------------------------


def test_aggregator_drain_projects_totals(pg_storage) -> None:
    from inkfoot.storage.aggregator import AggregatorWorker

    _seed_run(pg_storage)
    for sequence, tokens in enumerate((10, 20, 30), start=1):
        pg_storage.insert_event(
            event_id=f"evt-{sequence}",
            run_id="run-1",
            kind="llm_call",
            occurred_at=sequence,
            sequence=sequence,
            # Real emitted shape: tokens nested under ``ledger``, cost in
            # ``estimated_nanodollars`` (what emit_llm_call writes).
            payload_json=json.dumps(
                {
                    "ledger": {
                        "user_input_tokens": tokens,
                        "output_tokens": tokens * 2,
                    },
                    "estimated_nanodollars": tokens * 100,
                }
            ),
        )
    pg_storage.insert_event(
        event_id="evt-outcome",
        run_id="run-1",
        kind="outcome",
        occurred_at=9,
        sequence=9,
        payload_json='{"outcome": "success"}',
    )

    drained = AggregatorWorker(pg_storage).drain_once()

    assert drained == 1
    row = pg_storage.get_run("run-1")
    assert row["total_input_tokens"] == 60
    assert row["total_output_tokens"] == 120
    assert row["total_nanodollars"] == 6000
    assert row["outcome"] == "success"
    assert row["aggregates_dirty"] == 0


# ----------------------------------------------------------------------
# Heartbeat surface
# ----------------------------------------------------------------------


def test_heartbeat_roundtrip_and_upsert(pg_storage) -> None:
    assert pg_storage.read_heartbeat() is None
    pg_storage.write_heartbeat(swept_at=1000, runs_swept=3)
    assert pg_storage.read_heartbeat() == {
        "last_sweep_at": 1000,
        "runs_swept": 3,
    }
    pg_storage.write_heartbeat(swept_at=2000, runs_swept=0)
    assert pg_storage.read_heartbeat() == {
        "last_sweep_at": 2000,
        "runs_swept": 0,
    }
