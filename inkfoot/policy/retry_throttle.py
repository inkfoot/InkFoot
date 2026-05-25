"""``RetryThrottle`` — emits when retries in a rolling window exceed
the threshold.

Phase 0 detects retries by inspecting ``ctx.metadata["retry"]`` — the
shim doesn't classify retries directly (no SDK call comes labelled
"this is a retry"), so the policy relies on the caller setting that
flag through ``inkfoot.tag`` (E5) or via an integration that knows
about retries. For Phase 0 internal use the test fixtures set the
flag manually.

The window is wall-clock seconds; we record event timestamps and
drop entries that fall out of the window before counting.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Deque
from collections import deque

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext, PolicyDecision

from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision  # noqa: E402


class RetryThrottle(Policy):
    """Emit ``retry_throttle`` when the count of observed retries in
    a rolling ``window_s`` window for a single run exceeds ``max``.

    Fires *on the breach call* — i.e. when the (max+1)th retry
    lands inside the window. After firing, the policy continues to
    track but won't fire again for the same run until the count
    drops back below the threshold and rises again.
    """

    NAME = "RetryThrottle"
    SUPPORTED_PATTERNS = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }

    def __init__(self, window_s: int, max: int) -> None:
        if not isinstance(window_s, int) or isinstance(window_s, bool):
            raise TypeError(
                f"window_s must be int, got {type(window_s).__name__}"
            )
        if not isinstance(max, int) or isinstance(max, bool):
            raise TypeError(f"max must be int, got {type(max).__name__}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        if max < 1:
            raise ValueError(f"max must be >= 1, got {max}")
        self.window_s = window_s
        self.max = max
        self._events: dict[str, Deque[float]] = defaultdict(deque)
        self._fired: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        is_retry = bool(ctx.metadata.get("retry"))
        if not is_retry:
            # Not a retry — reset the "fired" flag so a future
            # genuine burst can re-fire.
            with self._lock:
                self._fired.discard(ctx.run_id)
            return PolicyDecision(action="allow")

        now = time.monotonic()
        window_start = now - self.window_s
        with self._lock:
            events = self._events[ctx.run_id]
            events.append(now)
            while events and events[0] < window_start:
                events.popleft()
            count = len(events)
            if count > self.max and ctx.run_id not in self._fired:
                self._fired.add(ctx.run_id)
                return PolicyDecision(
                    action="warn",
                    reason=(
                        f"{count} retries in last {self.window_s}s; "
                        f"threshold is {self.max}"
                    ),
                    metadata={
                        "retry_count": count,
                        "window_s": self.window_s,
                        "max": self.max,
                    },
                    emit_event_kind="retry_throttle",
                )
        return PolicyDecision(action="allow")

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        # Bookkeeping only — the count is updated in before_call.
        # InMemoryRunState.retry_counts is also tracked here so
        # reporting can break retry overhead out by error class.
        from inkfoot._run_context import get_or_create_run_state

        if not bool(ctx.metadata.get("retry")):
            return
        cause = ctx.metadata.get("retry_cause") or "unknown"
        state = get_or_create_run_state(ctx.run_id)
        state.retry_counts[cause] = state.retry_counts.get(cause, 0) + 1

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def current_count(self, run_id: str) -> int:
        with self._lock:
            return len(self._events.get(run_id, ()))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._fired.clear()
