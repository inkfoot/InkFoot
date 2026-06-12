"""GeminiShim tests.

Covers:
- After install, ``GenerativeModel.generate_content`` (sync + async)
  is the shim; the originals are restored on uninstall.
- One event per call (sync and async); the user's response comes
  back unmodified; provider errors still emit an ``llm_call`` event.
- Request synthesis: the model-bound ``model_name`` (stripped of the
  ``models/`` prefix) and ``system_instruction`` reach the ledger.
- The cache-resource flow end to end: ``CacheControlPlacer`` creates
  one ``CachedContent`` for the oversized stable prefix, the first
  call bills it as a write (``miss``), later calls rebind to the
  resource and bill reads (``hit``); per-call ``tools`` overrides
  skip the rebind; creation failure degrades to one advice event.
- ``install_shims`` allow-list keys select providers by name.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    _set_current_run,
)
from inkfoot.policy import CacheControlPlacer
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.providers.gemini import GEMINI_CACHE_MANAGER
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_anthropic,
    install_fake_gemini,
    uninstall_fake_sdks,
)


@pytest.fixture(autouse=True)
def reset_state() -> None:
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    GEMINI_CACHE_MANAGER.reset()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    GEMINI_CACHE_MANAGER.reset()
    uninstall_fake_sdks()


def _seed_run(storage: SQLiteStorage, run_id: str = "test-run") -> str:
    storage.connect()
    storage.start_run(
        run_id=run_id,
        task="test",
        agent_kind="unit",
        started_at=1_700_000_000_000,
    )
    return run_id


def _events(storage: SQLiteStorage, run_id: str = "test-run") -> list:
    return list(storage.iter_events(run_id))


def _llm_payloads(storage: SQLiteStorage, run_id: str = "test-run") -> list:
    return [
        json.loads(ev["payload_json"])
        for ev in _events(storage, run_id)
        if ev["kind"] == "llm_call"
    ]


# ----------------------------------------------------------------------
# Install / uninstall mechanics
# ----------------------------------------------------------------------


def test_install_replaces_generate_content_and_uninstall_restores(
    tmp_path,
) -> None:
    fakes = install_fake_gemini()
    model_cls = fakes["GenerativeModel"]
    original_sync = model_cls.generate_content
    original_async = model_cls.generate_content_async

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)

    assert getattr(
        model_cls.generate_content, "__inkfoot_shim__", False
    ) is True
    assert getattr(
        model_cls.generate_content_async, "__inkfoot_shim__", False
    ) is True
    assert model_cls.generate_content is not original_sync

    instrument_mod.shutdown()

    assert model_cls.generate_content is original_sync
    assert model_cls.generate_content_async is original_async


def test_sync_wrapper_is_plain_async_wrapper_is_coroutine(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)

    model_cls = fakes["GenerativeModel"]
    assert not inspect.iscoroutinefunction(model_cls.generate_content)
    assert inspect.iscoroutinefunction(model_cls.generate_content_async)


def test_install_shims_allow_list_selects_by_provider_name(
    tmp_path,
) -> None:
    from inkfoot._shim_install import (
        install_shims,
        installed_providers,
        uninstall_shims,
    )

    install_fake_anthropic()
    install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    installed = install_shims(
        storage=storage,
        capture_mode_getter=lambda: "metadata",
        sdks=["gemini"],
    )
    try:
        # Anthropic is importable but excluded by the allow-list.
        assert installed == ["gemini"]
        assert installed_providers() == ["gemini"]
    finally:
        uninstall_shims()


# ----------------------------------------------------------------------
# Event emit
# ----------------------------------------------------------------------


def test_one_event_per_sync_call(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)
    _set_current_run("test-run")

    model = fakes["GenerativeModel"]("gemini-1.5-pro")
    n_calls = 5
    for _ in range(n_calls):
        model.generate_content("hi")

    assert len(_events(storage)) == n_calls


def test_one_event_per_async_call(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)
    _set_current_run("test-run")

    model = fakes["GenerativeModel"]("gemini-1.5-pro")
    n_calls = 4

    async def runner() -> None:
        for _ in range(n_calls):
            await model.generate_content_async("hi")

    asyncio.run(runner())
    assert len(_events(storage)) == n_calls


def test_response_is_returned_unmodified(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)
    _set_current_run("test-run")

    model = fakes["GenerativeModel"]("gemini-1.5-pro")
    result = model.generate_content("hello")
    assert result["candidates"][0]["content"]["parts"] == [{"text": "ack"}]
    assert result["usage_metadata"]["candidates_token_count"] == 5


def test_model_name_is_stripped_of_models_prefix(tmp_path) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)
    _set_current_run("test-run")

    # The fake stores "models/gemini-1.5-flash" like the real SDK.
    model = fakes["GenerativeModel"]("gemini-1.5-flash")
    model.generate_content("hi")

    (payload,) = _llm_payloads(storage)
    assert payload["model"] == "gemini-1.5-flash"
    assert payload["provider"] == "gemini"


def test_model_bound_system_instruction_reaches_the_ledger(
    tmp_path,
) -> None:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed_run(storage)
    _set_current_run("test-run")

    model = fakes["GenerativeModel"](
        "gemini-1.5-pro",
        system_instruction="You are a careful deploy-triage agent.",
    )
    model.generate_content("what failed?")

    (payload,) = _llm_payloads(storage)
    assert payload["ledger"]["system_static_tokens"] > 0
    assert payload["ledger"]["user_input_tokens"] > 0


def test_provider_error_still_emits_an_llm_call_event(tmp_path) -> None:
    fakes = install_fake_gemini()

    class ProviderRateLimit(Exception):
        pass

    def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise ProviderRateLimit("rate limited")

    fakes["GenerativeModel"].generate_content = boom
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _seed_run(storage)
    instrument_mod.instrument(storage=storage)
    _set_current_run("test-run")

    model = fakes["GenerativeModel"]("gemini-1.5-pro")
    with pytest.raises(ProviderRateLimit, match="rate limited"):
        model.generate_content("hi")

    events = _events(storage)
    assert len(events) == 1
    assert events[0]["kind"] == "llm_call"
    payload = json.loads(events[0]["payload_json"])
    assert payload["error"]["type"] == "ProviderRateLimit"
    assert payload["ledger"]["output_tokens"] == 0


# ----------------------------------------------------------------------
# Cache-resource flow (CacheControlPlacer × shim rebinding)
# ----------------------------------------------------------------------

# Comfortably above the conservative minimum cacheable size the
# policy enforces (~131k chars ≈ the 32,768-token provider floor).
_BIG_SYSTEM = "deploy-triage rules; " * 7_000


def _instrument_with_placer(tmp_path) -> tuple[dict, SQLiteStorage]:
    fakes = install_fake_gemini()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    _seed_run(storage)
    instrument_mod.instrument(
        storage=storage, policies=[CacheControlPlacer()]
    )
    _set_current_run("test-run")
    return fakes, storage


def test_placer_creates_resource_once_and_later_calls_reference_it(
    tmp_path,
) -> None:
    fakes, storage = _instrument_with_placer(tmp_path)

    model = fakes["GenerativeModel"](
        "gemini-1.5-pro", system_instruction=_BIG_SYSTEM
    )
    model.generate_content("first question")
    model.generate_content("second question")

    # One provider-side resource; both dispatches were rebound to it.
    assert len(fakes["cache_creations"]) == 1
    assert [c["cached"] for c in fakes["calls"]] == [True, True]

    kinds = [ev["kind"] for ev in _events(storage)]
    assert kinds.count("llm_call") == 2
    assert kinds.count("cache_resource_created") == 1

    first, second = _llm_payloads(storage)
    # Creation call: the cached count is the one-time write.
    assert first["ledger"]["cache_creation_tokens"] == 256
    assert first["ledger"]["cache_read_tokens"] == 0
    assert first["cache_status"] == "miss"
    # Subsequent call: served from the resource.
    assert second["ledger"]["cache_read_tokens"] == 256
    assert second["ledger"]["cache_creation_tokens"] == 0
    assert second["cache_status"] == "hit"


def test_small_prefix_does_not_create_a_resource(tmp_path) -> None:
    fakes, storage = _instrument_with_placer(tmp_path)

    model = fakes["GenerativeModel"](
        "gemini-1.5-pro", system_instruction="small prefix"
    )
    model.generate_content("hi")

    assert fakes["cache_creations"] == []
    assert [c["cached"] for c in fakes["calls"]] == [False]
    kinds = [ev["kind"] for ev in _events(storage)]
    assert "cache_resource_created" not in kinds


def test_per_call_tools_override_skips_the_rebind(tmp_path) -> None:
    fakes, storage = _instrument_with_placer(tmp_path)

    model = fakes["GenerativeModel"](
        "gemini-1.5-pro", system_instruction=_BIG_SYSTEM
    )
    model.generate_content(
        "hi", tools=[{"function_declarations": [{"name": "wx"}]}]
    )

    # The resource may be created for the new prefix, but a cache-
    # bound model can't take per-call tools — the user's original
    # model object is dispatched, so nothing reads from the cache.
    assert [c["cached"] for c in fakes["calls"]] == [False]
    (payload,) = _llm_payloads(storage)
    assert payload["ledger"]["cache_read_tokens"] == 0


def test_creation_failure_degrades_to_one_advice_event(tmp_path) -> None:
    fakes, storage = _instrument_with_placer(tmp_path)

    def _boom(cls: Any, model: Any = None, **kwargs: Any) -> Any:
        raise RuntimeError("quota exceeded")

    fakes["CachedContent"].create = classmethod(_boom)

    model = fakes["GenerativeModel"](
        "gemini-1.5-pro", system_instruction=_BIG_SYSTEM
    )
    model.generate_content("first")
    model.generate_content("second")

    # Both user calls succeed un-cached.
    assert [c["cached"] for c in fakes["calls"]] == [False, False]
    kinds = [ev["kind"] for ev in _events(storage)]
    assert kinds.count("llm_call") == 2
    # Advice fires once per run, not per call.
    assert kinds.count("cache_control_advice") == 1
    assert kinds.count("cache_resource_created") == 0
