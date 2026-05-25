"""Process-global :class:`PolicyRegistry` — the dispatch surface the
shims call into.

A class-level state is appropriate here: only one Inkfoot
instrumentation can be active in a process at a time (the shim
monkey-patches a module-level function pointer), so the registry is
inherently a singleton.

The registry stores policies in insertion order so reports and
debug logs see them in the order the user registered them. Identity
dedup (``id(policy) in self._ids``) prevents accidental double-add
of the same instance; passing two *different* instances of the same
class is fine (use case: two ``BudgetCap`` watchers with different
thresholds).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext, Policy, PolicyDecision


class _Registry:
    def __init__(self) -> None:
        self._policies: list["Policy"] = []
        self._ids: set[int] = set()
        self._lock = threading.Lock()

    def add(self, policy: "Policy") -> None:
        with self._lock:
            if id(policy) in self._ids:
                return
            self._ids.add(id(policy))
            self._policies.append(policy)

    def clear(self) -> None:
        with self._lock:
            self._policies.clear()
            self._ids.clear()

    def __iter__(self) -> Iterator["Policy"]:
        # Take a snapshot under the lock so concurrent registration
        # mid-iteration doesn't blow up.
        with self._lock:
            snapshot = list(self._policies)
        return iter(snapshot)

    def __len__(self) -> int:
        with self._lock:
            return len(self._policies)

    def before_call(self, ctx: "CallContext") -> list["PolicyDecision"]:
        """Dispatch ``before_call`` to every registered policy.
        Returns the list of decisions in registration order; the
        shim inspects them to decide whether to emit events."""
        from inkfoot.shims._isolation import safely_run

        decisions: list["PolicyDecision"] = []
        for policy in self:
            decision = safely_run(
                policy.before_call,
                ctx,
                hook_label=f"{type(policy).__name__}.before_call",
            )
            if decision is None:
                # The hook raised; isolation absorbed it. Fall back
                # to a no-op decision so downstream code keeps going.
                from inkfoot.policy import PolicyDecision  # noqa: PLC0415

                decision = PolicyDecision(action="allow")
            decisions.append(decision)
        return decisions

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        from inkfoot.shims._isolation import safely_run

        for policy in self:
            safely_run(
                policy.after_call,
                ctx,
                response,
                hook_label=f"{type(policy).__name__}.after_call",
            )


# Singleton.
PolicyRegistry = _Registry()
