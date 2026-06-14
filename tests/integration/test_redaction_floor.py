"""The redaction floor keeps replay capture safe for services.

With replay capture enabled, request and response bodies are persisted
so a run can be replayed. The default floor masks the secret shapes
that must never land on disk. These tests drive a real shimmed call
carrying a synthetic secrets corpus and prove two things:

* not one un-redacted byte of the corpus appears in the on-disk SQLite
  file (we read the file back and scan it), and
* the counts-only audit trail records how many of each shape were
  masked, never the secret itself.

The call runs against the offline fake SDK so the guarantee is checked
on every CI run, with no network or credentials.
"""

from __future__ import annotations

import logging
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


# Synthetic secrets corpus. Each is fake but shaped like the real thing.
EMAIL_A = "alice.smith@example.com"
EMAIL_B = "ops-team@corp.example.org"
OPENAI_KEY = "sk-proj-ABCDEFGHIJKLMNOPqrstuvwx0123"
ANTHROPIC_KEY = "sk-ant-api03-ZYXWVUTSRQ9876543210"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.s1gNature_tok-EN"

# Every distinct secret string, for the on-disk scan.
CORPUS = [EMAIL_A, EMAIL_B, OPENAI_KEY, ANTHROPIC_KEY, JWT]

# Expected per-pattern hit counts for one redaction pass over the
# request built below (two emails, one of each key shape, one JWT).
EXPECTED_COUNTS = {
    "email": 2,
    "anthropic_key": 1,
    "openai_key": 1,
    "jwt": 1,
}


def _instrument_replay(db_path: Path, *, capture_mode: str = "replay"):
    """Install the fake Anthropic SDK *then* instrument, so the shim is
    actually patched onto it; returns the fake client facade."""
    fakes = install_fake_anthropic()
    inkfoot.instrument(
        storage=SQLiteStorage(path=db_path),
        capture_mode=capture_mode,
        langchain=False,
    )
    return fakes["Anthropic"]()


def _make_pii_call(client) -> None:
    """Drive one shimmed Anthropic call whose request carries the
    whole secrets corpus across the system prompt and a user message."""
    with inkfoot.agent_run(task="redaction-floor"):
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            system=(
                f"Reach me at {EMAIL_A} or {EMAIL_B}. "
                f"Service key {ANTHROPIC_KEY}."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Here is my key {OPENAI_KEY} and bearer {JWT}."
                    ),
                }
            ],
        )


def _event_contents_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT request_json, response_json, content_redacted "
            "FROM event_contents"
        ).fetchall()
    finally:
        conn.close()


def test_no_corpus_byte_reaches_the_sqlite_file(tmp_path) -> None:
    db = tmp_path / "runs.db"
    client = _instrument_replay(db)
    _make_pii_call(client)
    # Flush + close so everything the run produced is durable on disk.
    instrument_mod.shutdown()

    blob = read_db_and_siblings(db)
    for secret in CORPUS:
        assert secret.encode("utf-8") not in blob, (
            f"un-redacted secret {secret!r} found in the on-disk payload"
        )


def test_content_row_is_written_and_flagged_redacted(tmp_path) -> None:
    db = tmp_path / "runs.db"
    client = _instrument_replay(db)
    _make_pii_call(client)
    instrument_mod.shutdown()

    rows = _event_contents_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["content_redacted"] == 1
    # The masked placeholders are present in the stored request.
    assert "[REDACTED:email]" in row["request_json"]
    assert "[REDACTED:anthropic_key]" in row["request_json"]
    assert "[REDACTED:openai_key]" in row["request_json"]
    assert "[REDACTED:jwt]" in row["request_json"]


def test_audit_log_counts_match_corpus_and_omit_the_text(
    tmp_path, caplog
) -> None:
    db = tmp_path / "runs.db"
    client = _instrument_replay(db)
    with caplog.at_level(logging.INFO, logger="inkfoot.redaction"):
        _make_pii_call(client)

    audit = [
        r for r in caplog.records if "redaction_audit" in r.getMessage()
    ]
    assert len(audit) == 1
    record = audit[0]
    assert record.redaction_counts == EXPECTED_COUNTS
    # The audit trail must never carry the secret itself.
    message = record.getMessage()
    for secret in CORPUS:
        assert secret not in message

    instrument_mod.shutdown()


def test_metadata_mode_keeps_floor_off_and_writes_no_content(
    tmp_path,
) -> None:
    # Sanity counter-test: in metadata mode no content is persisted at
    # all, so there is nothing to redact and no content row.
    db = tmp_path / "runs.db"
    client = _instrument_replay(db, capture_mode="metadata")
    _make_pii_call(client)
    instrument_mod.shutdown()

    assert _event_contents_rows(db) == []
    # The metadata payload never carries raw prompt text, so the corpus
    # is absent here too.
    blob = read_db_and_siblings(db)
    for secret in CORPUS:
        assert secret.encode("utf-8") not in blob
