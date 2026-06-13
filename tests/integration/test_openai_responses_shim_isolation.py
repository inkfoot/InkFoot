"""Hook isolation for the Responses-API shim.

Same invariant as every other shim: an exception inside Inkfoot's
own wrap — policy hooks, the translator, the emit path — must never
reach the caller, and the caller's response must come back
unmodified. Verified two ways:

* a fuzz run that injects random exception classes at the policy
  hook points across many calls, and
* deterministic faults forced inside the translator and the storage
  write (the two seams ``safely_run`` guards on the post-call side).

Provider errors are the one deliberate exception: those re-raise
to the caller untouched (after the failure event is recorded).
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
from inkfoot.policy import (
    CallContext,
    IntegrationPattern,
    Policy,
    PolicyDecision,
)
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_openai,
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


def _is_intact_response(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("object") == "response"
        and bool(result.get("usage"))
    )


def test_fuzzed_hook_exceptions_never_reach_the_caller(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    storage.start_run(
        run_id="fuzz-run",
        task="fuzz",
        agent_kind="integration",
        started_at=1_700_000_000_000,
    )
    instrument_mod.instrument(storage=storage, policies=[_Chaos(seed=4242)])
    _set_current_run("fuzz-run")

    client = fakes["Responses"]()
    N = 1000
    crashes = 0
    bad_responses = 0
    for i in range(N):
        try:
            result = client.create(model="gpt-4o", input=f"call {i}")
        except Exception:
            crashes += 1
            continue
        if not _is_intact_response(result):
            bad_responses += 1

    assert crashes == 0, (
        f"{crashes}/{N} user calls saw a propagated exception"
    )
    assert bad_responses == 0, (
        f"{bad_responses}/{N} calls returned a modified response"
    )


def test_translator_fault_is_absorbed_and_response_survives(
    tmp_path, monkeypatch
) -> None:
    from inkfoot.normalise.openai_responses import OpenAIResponsesTranslator

    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    storage.start_run(
        run_id="fault-run",
        task="fault",
        agent_kind="integration",
        started_at=1_700_000_000_000,
    )
    _set_current_run("fault-run")

    def _boom(self, **kwargs):
        raise RuntimeError("translator exploded")

    monkeypatch.setattr(OpenAIResponsesTranslator, "translate", _boom)

    result = fakes["Responses"]().create(model="gpt-4o", input="hi")
    assert _is_intact_response(result)
    # The event was dropped rather than half-written.
    assert not [
        ev
        for ev in storage.iter_events("fault-run")
        if ev["kind"] == "llm_call"
    ]


def test_storage_fault_is_absorbed_and_response_survives(
    tmp_path, monkeypatch
) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    storage.start_run(
        run_id="fault-run",
        task="fault",
        agent_kind="integration",
        started_at=1_700_000_000_000,
    )
    _set_current_run("fault-run")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(storage), "insert_event", _boom)

    result = fakes["Responses"]().create(model="gpt-4o", input="hi")
    assert _is_intact_response(result)


def test_async_fuzzed_hook_exceptions_never_reach_the_caller(
    tmp_path,
) -> None:
    import asyncio

    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    storage.start_run(
        run_id="fuzz-run-async",
        task="fuzz",
        agent_kind="integration",
        started_at=1_700_000_000_000,
    )
    instrument_mod.instrument(storage=storage, policies=[_Chaos(seed=1337)])
    _set_current_run("fuzz-run-async")

    client = fakes["AsyncResponses"]()
    N = 200

    async def runner() -> tuple[int, int]:
        crashes = 0
        bad_responses = 0
        for i in range(N):
            try:
                result = await client.create(model="gpt-4o", input=f"call {i}")
            except Exception:
                crashes += 1
                continue
            if not _is_intact_response(result):
                bad_responses += 1
        return crashes, bad_responses

    crashes, bad_responses = asyncio.run(runner())
    assert crashes == 0, (
        f"{crashes}/{N} async calls saw a propagated exception"
    )
    assert bad_responses == 0, (
        f"{bad_responses}/{N} async calls returned a modified response"
    )
