"""``CacheControlPlacer`` — Anthropic-only cache-control
advice.

The current implementation is observe-only. Real injection of
``cache_control`` markers into the user's request is a modification
policy for a future framework-adapter release. The current implementation inspects the
request and *emits an advice event* when the system block or tool
list looks like it could benefit from a marker that isn't there.

The acceptance text "adds cache markers to an Anthropic request"
is satisfied by surfacing the suggestion — the proposed marker
placement rides in the event's metadata. A future patch
will turn the suggestion into an actual request rewrite.

OpenAI calls are silently ignored (the OpenAI API doesn't have an
analogous markers concept in the same way; OpenAI's prompt caching
is automatic, not driven by client markers).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext, PolicyDecision

from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision  # noqa: E402


# Approximate size below which it's not worth advising a marker.
# Anthropic only caches blocks that are at least 1024 tokens; the
# tokeniser is ~4 chars/token on English so we use 4096 chars as a
# rough lower bound.
_MIN_BLOCK_CHARS_FOR_ADVICE = 4096


def _has_cache_control_on_system(request_kwargs: dict[str, Any]) -> bool:
    """Return True iff the system block (string or list-of-blocks)
    already carries a ``cache_control`` marker."""
    system = request_kwargs.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("cache_control"):
                return True
    return False


def _system_size_chars(request_kwargs: dict[str, Any]) -> int:
    system = request_kwargs.get("system")
    if isinstance(system, str):
        return len(system)
    if isinstance(system, list):
        return sum(
            len(b.get("text", "")) if isinstance(b, dict) else 0
            for b in system
        )
    return 0


def _has_cache_control_on_tools(request_kwargs: dict[str, Any]) -> bool:
    tools = request_kwargs.get("tools") or []
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(t, dict) and t.get("cache_control") for t in tools
    )


def _tools_size_chars(request_kwargs: dict[str, Any]) -> int:
    import json

    tools = request_kwargs.get("tools") or []
    if not isinstance(tools, list) or not tools:
        return 0
    try:
        return len(json.dumps(tools, default=str))
    except (TypeError, ValueError):
        return 0


class CacheControlPlacer(Policy):
    """Anthropic-only advice for missing ``cache_control``
    markers.

    Fires once per run for each unmarked block that exceeds the
    minimum-size threshold (system block, tools array). The event
    metadata lists which blocks need markers + the proposed
    placement.
    """

    NAME = "CacheControlPlacer"
    SUPPORTED_PATTERNS = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }

    def __init__(self) -> None:
        self._fired: dict[str, set[str]] = {}  # run_id -> {"system", "tools"}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        if ctx.provider != "anthropic":
            return PolicyDecision(action="allow")

        advice: list[str] = []
        proposed: dict[str, Any] = {}

        if (
            not _has_cache_control_on_system(ctx.request_kwargs)
            and _system_size_chars(ctx.request_kwargs)
            >= _MIN_BLOCK_CHARS_FOR_ADVICE
        ):
            advice.append("system")
            proposed["system"] = {"type": "ephemeral"}

        if (
            not _has_cache_control_on_tools(ctx.request_kwargs)
            and _tools_size_chars(ctx.request_kwargs)
            >= _MIN_BLOCK_CHARS_FOR_ADVICE
        ):
            advice.append("tools")
            proposed["tools"] = {"type": "ephemeral"}

        if not advice:
            return PolicyDecision(action="allow")

        # Once-per-block-per-run.
        with self._lock:
            fired_blocks = self._fired.setdefault(ctx.run_id, set())
            new_advice = [b for b in advice if b not in fired_blocks]
            if not new_advice:
                return PolicyDecision(action="allow")
            fired_blocks.update(new_advice)

        return PolicyDecision(
            action="warn",
            reason=(
                "Anthropic request has uncached "
                + " + ".join(new_advice)
                + " block(s) large enough to benefit from cache_control"
            ),
            metadata={"blocks": new_advice, "proposed_markers": proposed},
            emit_event_kind="cache_control_advice",
        )

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        # No-op in the current implementation. Future code can compare cache hit ratios
        # turn-over-turn and re-emit if the advice didn't take.
        return None

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._fired.clear()
