"""Direct event writes for policies that emit more than one event
kind per call.

The shim's decision pipeline (``_emit_policy_events``) writes at most
one event per :class:`PolicyDecision`. Modification policies can
produce several distinct facts on a single call — e.g. one tool
dropped *and* another restored — so they write their own rows through
this helper, the same way the contract runtime writes violation
events.

Events written here land *before* the call's ``llm_call`` event in
sequence order, which is truthful: the modification happened before
the SDK call was made.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOG = logging.getLogger("inkfoot.policy")


def emit_policy_event(run_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Write one event row onto ``run_id``. Best-effort: no-ops when
    instrumentation hasn't initialised storage, and never raises into
    the caller — this sits on the policy hot path, where a storage
    hiccup must not break the user's LLM call.
    """
    try:
        from inkfoot._instrument import _STORAGE  # noqa: PLC0415

        if _STORAGE is None:
            return

        from ulid import ULID  # noqa: PLC0415

        from inkfoot.shims._emit import _next_sequence, _now_ms  # noqa: PLC0415

        _STORAGE.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind=kind,
            occurred_at=_now_ms(),
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(payload, default=str),
            # Policy events never carry replay content.
            capture_mode="metadata",
        )
    except Exception:  # pylint: disable=broad-except
        _LOG.warning(
            "emit_policy_event(%s) failed for run %s", kind, run_id, exc_info=True
        )
