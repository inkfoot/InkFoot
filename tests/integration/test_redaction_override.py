"""A deployment's own redaction hook composes with the floor.

An operator can pass a custom hook to mask organisation-specific
shapes the floor doesn't know about. The contract is that the custom
hook runs *and* the floor still runs alongside it — a custom hook
extends the redaction, it never replaces the guaranteed minimum. This
test masks a made-up internal token shape with a custom hook and
checks that both the custom shape and the floor's standard shapes are
gone from the on-disk payload.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.redaction import RedactionContext, RedactionPayload
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


ORG_TOKEN = "ACME-482913"
EMAIL = "alice.smith@example.com"

_ORG_PATTERN = re.compile(r"ACME-[0-9]{6}")


class OrgTokenRedactor:
    """A minimal custom hook: masks one organisation-internal token
    shape the floor doesn't cover. Recurses into the content the same
    way the floor does and returns a new payload (never mutates)."""

    def __call__(
        self, payload: RedactionPayload, ctx: RedactionContext
    ) -> RedactionPayload:
        return {key: self._scrub(value) for key, value in payload.items()}

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return _ORG_PATTERN.sub("[ORG_TOKEN]", value)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value


def _request_json(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT request_json, content_redacted FROM event_contents"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "expected a replay content row"
    assert row[1] == 1
    return row[0]


def test_custom_hook_and_floor_both_fire(tmp_path) -> None:
    db = tmp_path / "runs.db"
    fakes = install_fake_anthropic()
    inkfoot.instrument(
        storage=SQLiteStorage(path=db),
        capture_mode="replay",
        redaction_hook=OrgTokenRedactor(),
        langchain=False,
    )
    client = fakes["Anthropic"]()
    with inkfoot.agent_run(task="redaction-override"):
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            system=f"Internal ref {ORG_TOKEN}; reach me at {EMAIL}.",
            messages=[{"role": "user", "content": "summarise please"}],
        )
    instrument_mod.shutdown()

    # Neither the org token (custom hook) nor the email (floor) is on disk.
    blob = read_db_and_siblings(db)
    assert ORG_TOKEN.encode("utf-8") not in blob
    assert EMAIL.encode("utf-8") not in blob

    # Both maskings are visible in the stored content row: the custom
    # placeholder and the floor's placeholder side by side.
    request = _request_json(db)
    assert "[ORG_TOKEN]" in request
    assert "[REDACTED:email]" in request
