"""Hook isolation fuzz test (E3-S2 T6).

Inject random exceptions at every Inkfoot hook point on each call
and assert the user's call always completes with the original SDK
response. Repeat for 1000 random exception classes / hook
combinations.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    _set_current_run,
)
from inkfoot.policy import CallContext, IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_anthropic,
    uninstall_fake_sdks,
)


_EXCEPTION_CLASSES = (
    RuntimeError,
    ValueError,
    KeyError,
    AttributeError,
    TypeError,
    ZeroDivisionError,
    OverflowError,
    LookupError,
    IndexError,
)


class _Chaos(Policy):
    """A policy that raises a different exception on every call."""

    NAME = "Chaos"
    SUPPORTED_PATTERNS = {IntegrationPattern.A}

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def before_call(self, ctx: CallContext) -> PolicyDecision:
        if self._rng.random() < 0.5:
            exc_cls = self._rng.choice(_EXCEPTION_CLASSES)
            raise exc_cls(f"chaos before_call seed={self._rng.random()}")
        return PolicyDecision(action="allow")

    def after_call(self, ctx: CallContext, response: Any) -> None:
        if self._rng.random() < 0.5:
            exc_cls = self._rng.choice(_EXCEPTION_CLASSES)
            raise exc_cls(f"chaos after_call seed={self._rng.random()}")


@pytest.fixture(autouse=True)
def reset_state() -> None:
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


def test_thousand_random_exception_injections_never_reach_user_code(
    tmp_path,
) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    storage.start_run(
        run_id="fuzz-run",
        task="fuzz",
        agent_kind="unit",
        started_at=1_700_000_000_000,
    )
    instrument_mod.instrument(
        storage=storage, policies=[_Chaos(seed=4242)]
    )
    _set_current_run("fuzz-run")

    client = fakes["Messages"]()
    N = 1000
    crashes = 0
    bad_responses = 0
    for i in range(N):
        try:
            result = client.create(
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": f"call {i}"}],
            )
        except Exception:
            crashes += 1
            continue
        if not (isinstance(result, dict) and result.get("usage")):
            bad_responses += 1

    assert crashes == 0, f"{crashes}/{N} user calls saw a propagated exception"
    assert bad_responses == 0, (
        f"{bad_responses}/{N} calls returned a modified response"
    )
