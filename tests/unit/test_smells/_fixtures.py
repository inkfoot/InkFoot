"""Shared fixture builders for the smell tests.

Each smell needs a *positive* fixture (the smell fires) and a
*negative* fixture (the smell stays silent). To keep the test files
focused on assertions rather than JSON wrangling, we build events
here from typed inputs and serialise to the same JSON shape the
shim's emit pipeline writes.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Iterable, Optional, Sequence

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall


def make_neutral_call(
    *,
    sequence: int,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    ledger_fields: Optional[dict[str, int]] = None,
    tools_called: Sequence[str] = (),
    tools_offered: Sequence[str] = (),
    cache_status: str = "n/a",
    started_at: int = 0,
    ended_at: int = 1,
    estimated_nanodollars: Optional[int] = 0,
) -> NeutralCall:
    """Build a :class:`NeutralCall` with explicit ledger overrides.

    Defaults make every ledger field zero; pass a dict of overrides
    to populate just the fields the smell cares about.
    """
    ledger = CausalTokenLedger(**(ledger_fields or {}))
    return NeutralCall(
        provider=provider,
        model=model,
        started_at=started_at,
        ended_at=ended_at,
        ledger=ledger,
        estimated_nanodollars=estimated_nanodollars,
        tools_offered=tuple(tools_offered),
        tools_called=tuple(tools_called),
        cache_status=cache_status,
        sequence=sequence,
    )


def event_from_neutral_call(
    call: NeutralCall,
    *,
    event_id: Optional[str] = None,
    kind: str = "llm_call",
) -> dict[str, Any]:
    """Serialise a :class:`NeutralCall` into the dict shape that
    Storage.iter_events yields (after the shim's
    json.dumps(asdict(call)) write)."""
    payload_json = json.dumps(dataclasses.asdict(call), default=str)
    return {
        "id": event_id or f"e-{call.sequence}",
        "run_id": "fixture-run",
        "kind": kind,
        "occurred_at": call.ended_at,
        "payload_json": payload_json,
        "sequence": call.sequence,
        "capture_mode": "metadata",
    }


def fixture_run() -> dict[str, Any]:
    """Lightweight stand-in for :class:`inkfoot.run.Run` — the
    detectors only read ``run.id`` defensively, and even that is
    optional, so a plain dict works fine in tests."""
    return {"id": "fixture-run", "task": "test", "agent_kind": "smell-test"}
