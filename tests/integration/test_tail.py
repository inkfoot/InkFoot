"""Integration test for ``inkfoot tail``.

Boots a real :class:`SQLiteStorage`, starts the tail loop in a
background thread, then mutates the storage from the main test
thread and asserts the lines land within the acceptance window.
"""

from __future__ import annotations

import io
import json
import threading
import time
from typing import Any

import pytest
from ulid import ULID

from inkfoot.cli.tail import tail_loop
from inkfoot.storage.sqlite import SQLiteStorage


def _seed_event(
    storage: SQLiteStorage,
    *,
    run_id: str,
    kind: str,
    sequence: int,
    payload: dict[str, Any] | None = None,
):
    storage.insert_event(
        event_id=str(ULID()),
        run_id=run_id,
        kind=kind,
        occurred_at=int(time.time() * 1000),
        sequence=sequence,
        payload_json=json.dumps(payload or {"sequence": sequence}),
        capture_mode="metadata",
    )


def test_tail_emits_inserted_event_within_one_second(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-live.db")
    storage.connect()
    storage.start_run(
        run_id="run-live", task="live", agent_kind="x", started_at=1
    )

    captured: list[str] = []
    captured_lock = threading.Lock()

    class _CollectingWriter:
        def write(self, s: str) -> int:
            with captured_lock:
                captured.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    stop_signal = threading.Event()

    def _runner():
        # Sentinel-driven so the loop body exits as soon as we set the event.
        def _sleep(_):
            if stop_signal.wait(timeout=0.01):
                # Signalled — burn through remaining iterations fast.
                return
            time.sleep(0.05)

        tail_loop(
            storage=storage,
            task_filter=None,
            since_ms=None,
            poll_interval_s=0.05,
            max_iterations=40,  # 40 × 0.05s = 2s wall-clock ceiling
            writer=_CollectingWriter(),
            sleep=_sleep,
        )

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    try:
        # Give the tail a head start to read the empty table.
        time.sleep(0.1)
        _seed_event(storage, run_id="run-live", kind="checkpoint", sequence=1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with captured_lock:
                if any("checkpoint" in s for s in captured):
                    break
            time.sleep(0.02)
        else:
            pytest.fail("tail did not surface the checkpoint within 1 s")
    finally:
        stop_signal.set()
        thread.join(timeout=3)
        storage.close()


def test_tail_task_filter_excludes_unmatched_runs(tmp_path):
    storage = SQLiteStorage(path=tmp_path / "tail-filter.db")
    storage.connect()
    storage.start_run(
        run_id="run-A", task="wanted", agent_kind="x", started_at=1
    )
    storage.start_run(
        run_id="run-B", task="other", agent_kind="x", started_at=2
    )

    captured: list[str] = []
    captured_lock = threading.Lock()

    class _CollectingWriter:
        def write(self, s: str) -> int:
            with captured_lock:
                captured.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    stop_signal = threading.Event()

    def _runner():
        def _sleep(_):
            stop_signal.wait(timeout=0.05)

        tail_loop(
            storage=storage,
            task_filter="wanted",
            since_ms=None,
            poll_interval_s=0.05,
            max_iterations=20,
            writer=_CollectingWriter(),
            sleep=_sleep,
        )

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    try:
        time.sleep(0.1)
        _seed_event(storage, run_id="run-B", kind="llm_call", sequence=1)
        _seed_event(storage, run_id="run-A", kind="outcome", sequence=2)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with captured_lock:
                if any("outcome" in s for s in captured):
                    break
            time.sleep(0.02)
    finally:
        stop_signal.set()
        thread.join(timeout=3)
        storage.close()

    joined = "".join(captured)
    assert "outcome" in joined
    # The run-B llm_call has task=other; the task filter should
    # have suppressed it.
    assert "llm_call" not in joined
