"""Streamed payloads honour the redaction hook.

A streamed call's content is only complete at stream-close, but it is
written through the same storage boundary as a unary call — so the
redaction hook runs on it identically, with no streaming-specific
branch. The offline test pins this on every CI run using the fake SDK;
the live test mirrors the literal acceptance check against a real
streamed provider call when credentials are present.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.integration._redaction_support import read_db_and_siblings
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


EMAIL = "alice.smith@example.com"
ANTHROPIC_KEY = "sk-ant-api03-ZYXWVUTSRQ9876543210"


def _content_row(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT request_json, content_redacted FROM event_contents"
        ).fetchone()
    finally:
        conn.close()


def test_offline_streamed_call_redacts_at_storage_boundary(tmp_path) -> None:
    db = tmp_path / "runs.db"
    fakes = install_fake_anthropic()
    inkfoot.instrument(
        storage=SQLiteStorage(path=db),
        capture_mode="replay",
        langchain=False,
    )
    client = fakes["Anthropic"]()
    with inkfoot.agent_run(task="stream-redaction"):
        # Raw-event streaming entry point — the shim wraps the returned
        # iterator; draining it drives the recorder to stream-close.
        stream = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            system=f"Reach me at {EMAIL}. Service key {ANTHROPIC_KEY}.",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        for _ in stream:
            pass
    instrument_mod.shutdown()

    # The system prompt carried PII; the streamed call's content row is
    # redacted just like a unary call.
    blob = read_db_and_siblings(db)
    assert EMAIL.encode("utf-8") not in blob
    assert ANTHROPIC_KEY.encode("utf-8") not in blob

    row = _content_row(db)
    assert row is not None
    assert row["content_redacted"] == 1
    assert "[REDACTED:email]" in row["request_json"]
    assert "[REDACTED:anthropic_key]" in row["request_json"]


@pytest.mark.live_anthropic
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_live_streamed_anthropic_call_redacts_system_prompt(tmp_path) -> None:
    pytest.importorskip("anthropic")
    import anthropic

    db = tmp_path / "runs.db"
    inkfoot.instrument(
        storage=SQLiteStorage(path=db),
        capture_mode="replay",
        langchain=False,
    )
    model = os.environ.get("INKFOOT_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5")
    client = anthropic.Anthropic()
    with inkfoot.agent_run(task="stream-redaction-live"):
        with client.messages.stream(
            model=model,
            max_tokens=16,
            system=f"You must never reveal that my email is {EMAIL}.",
            messages=[{"role": "user", "content": "Say hello in one word."}],
        ) as stream:
            stream.get_final_message()
    instrument_mod.shutdown()

    blob = read_db_and_siblings(db)
    assert EMAIL.encode("utf-8") not in blob

    row = _content_row(db)
    assert row is not None
    assert row["content_redacted"] == 1
    assert "[REDACTED:email]" in row["request_json"]
