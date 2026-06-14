"""AnthropicBedrock capture path.

An ``AnthropicBedrock(...).messages.create(...)`` call rides the same
``Messages.create`` patch as the direct Anthropic client, but the shim
detects the Bedrock client and tags the event
``provider="anthropic_bedrock"`` so the Bedrock-namespaced model id
resolves against the Bedrock per-token pricing rows. A direct
Anthropic client is unaffected, and an install without the
``anthropic[bedrock]`` extra behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import inkfoot._instrument as instrument_mod
from inkfoot._run_context import (
    _clear_current_run,
    _reset_ambient_run,
    _set_current_run,
)
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.shims.anthropic import _resolve_provider
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import install_fake_anthropic, uninstall_fake_sdks

_BEDROCK_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


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


def _seed(storage: SQLiteStorage, run_id: str = "test-run") -> str:
    storage.start_run(
        run_id=run_id, task="t", agent_kind="u", started_at=1_700_000_000_000
    )
    _set_current_run(run_id)
    return run_id


def _llm_events(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


# ----------------------------------------------------------------------
# Provider detection (pure helper)
# ----------------------------------------------------------------------


def test_resolve_provider_flags_bedrock_client() -> None:
    fakes = install_fake_anthropic()
    bedrock = fakes["AnthropicBedrock"]()
    assert _resolve_provider(bedrock.messages) == "anthropic_bedrock"


def test_resolve_provider_flags_async_bedrock_client() -> None:
    fakes = install_fake_anthropic()
    bedrock = fakes["AsyncAnthropicBedrock"]()
    assert _resolve_provider(bedrock.messages) == "anthropic_bedrock"


def test_resolve_provider_keeps_direct_client_on_anthropic() -> None:
    fakes = install_fake_anthropic()
    direct = fakes["Anthropic"]()
    assert _resolve_provider(direct.messages) == "anthropic"


def test_resolve_provider_tolerates_missing_client_backref() -> None:
    install_fake_anthropic()
    # A resource with no ``_client`` (or a stray object) must never
    # raise — it resolves to the direct provider.
    assert _resolve_provider(object()) == "anthropic"


def test_resolve_provider_without_bedrock_extra_is_anthropic() -> None:
    # No AnthropicBedrock class on the module → detection can't fire,
    # so the call resolves to the direct provider.
    fakes = install_fake_anthropic(with_bedrock=False)
    direct = fakes["Anthropic"]()
    assert _resolve_provider(direct.messages) == "anthropic"


# ----------------------------------------------------------------------
# End-to-end through the shim
# ----------------------------------------------------------------------


def test_bedrock_sync_call_tags_provider_and_resolves_cost(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AnthropicBedrock"]()
    client.messages.create(
        model=_BEDROCK_MODEL,
        messages=[{"role": "user", "content": "hi"}],
    )

    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic_bedrock"
    assert payload["model"] == _BEDROCK_MODEL
    # The Bedrock model id resolves against the pricing table → a real
    # nanodollar estimate rather than the unpriced ``None``.
    assert payload["estimated_nanodollars"] is not None
    assert int(payload["estimated_nanodollars"]) > 0


def test_bedrock_async_call_tags_provider(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncAnthropicBedrock"]()

    async def go() -> None:
        await client.messages.create(
            model=_BEDROCK_MODEL,
            messages=[{"role": "user", "content": "hi"}],
        )

    asyncio.run(go())

    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic_bedrock"
    assert payload["estimated_nanodollars"] is not None


def test_bedrock_streaming_call_tags_provider(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AnthropicBedrock"]()
    stream = client.messages.create(
        model=_BEDROCK_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    # Streamed events emit at close, not create; drain to trigger it.
    list(stream)

    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic_bedrock"


def test_direct_client_still_tags_anthropic(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Anthropic"]()
    client.messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )

    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic"


def test_without_bedrock_extra_direct_call_is_unchanged(tmp_path) -> None:
    fakes = install_fake_anthropic(with_bedrock=False)
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Anthropic"]()
    result = client.messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )

    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic"
    # The provider response is handed back untouched.
    assert result["content"] == [{"type": "text", "text": "ack"}]


def test_bedrock_and_direct_calls_coexist_in_one_run(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    fakes["Anthropic"]().messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "a"}],
    )
    fakes["AnthropicBedrock"]().messages.create(
        model=_BEDROCK_MODEL,
        messages=[{"role": "user", "content": "b"}],
    )

    providers = sorted(p["provider"] for p in _llm_events(storage, run_id))
    assert providers == ["anthropic", "anthropic_bedrock"]
