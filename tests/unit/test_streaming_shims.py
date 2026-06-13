"""Per-provider streaming shim tests, driven offline through the fake
SDKs.

These exercise the full path a streamed call takes: the shim detects
``stream=True`` (or the ``messages.stream()`` helper), tees the
returned iterator, and emits exactly one ``llm_call`` event when the
caller finishes draining it — never before. Output tokens come from the
provider's terminal usage when present, and from a tokeniser estimate
(flagged) when it isn't.
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
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_anthropic,
    install_fake_openai,
    uninstall_fake_sdks,
)


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
# The core streaming property: emit on close, not on create
# ----------------------------------------------------------------------


def test_no_event_emitted_until_stream_is_consumed(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    # The call has returned but nothing has been drained yet.
    assert _llm_events(storage, run_id) == []

    list(stream)
    assert len(_llm_events(storage, run_id)) == 1


def test_streamed_chunks_are_passed_through_unchanged(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    _seed(storage)

    client = fakes["OpenAI"]()
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    chunks = list(stream)
    # Every chunk is a chat-completion chunk dict with the same id.
    assert chunks
    assert {c["id"] for c in chunks} == {"chatcmpl_fake_1"}


# ----------------------------------------------------------------------
# OpenAI chat
# ----------------------------------------------------------------------


def test_openai_chat_stream_with_usage_uses_authoritative_output(
    tmp_path,
) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    list(
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "openai"
    assert payload["ledger"]["output_tokens"] == 5  # from the usage chunk
    assert "stream_options_off" not in payload["estimation_flags"]


def test_openai_chat_stream_without_usage_flags_and_estimates(
    tmp_path,
) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    list(
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    )
    (payload,) = _llm_events(storage, run_id)
    assert "stream_options_off" in payload["estimation_flags"]
    assert "output_tokens" in payload["estimation_flags"]
    # "ack" tokenises to a small positive count, not zero.
    assert payload["ledger"]["output_tokens"] > 0


def test_openai_chat_stream_async(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncCompletions"]()

    async def runner() -> None:
        stream = await client.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        async for _ in stream:
            pass

    asyncio.run(runner())
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 6


def test_openai_chat_stream_captures_tool_call_names(tmp_path) -> None:
    from tests.unit import _fake_sdks

    fakes = install_fake_openai()

    # Swap in a tool-call-bearing chunk sequence for this one call.
    def _streamer(self, *args, **kwargs):
        fakes["calls"].append({"variant": "sync", "kwargs": kwargs})
        return iter(
            _fake_sdks.openai_chat_stream_chunks(
                "chatcmpl_tool",
                include_usage=True,
                tool_name="get_weather",
            )
        )

    fakes["Completions"].create = _streamer
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Completions"]()
    list(client.create(model="gpt-4o", messages=[], stream=True))
    (payload,) = _llm_events(storage, run_id)
    assert payload["tools_called"] == ["get_weather"]


# ----------------------------------------------------------------------
# OpenAI Responses
# ----------------------------------------------------------------------


def test_openai_responses_stream_uses_completed_event(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    list(
        client.responses.create(
            model="gpt-4o", input="what's the weather", stream=True
        )
    )
    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "openai"
    assert payload["ledger"]["output_tokens"] == 5
    # A fully-mapped Responses shape carries no shape-unknown / no-usage
    # flags.
    assert not [
        f
        for f in payload["estimation_flags"]
        if f.startswith("responses_shape_unknown:") or f == "stream_no_usage"
    ]


def test_openai_responses_stream_async(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncResponses"]()

    async def runner() -> None:
        stream = await client.create(
            model="gpt-4o", input="hello", stream=True
        )
        async for _ in stream:
            pass

    asyncio.run(runner())
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 6


# ----------------------------------------------------------------------
# OpenAI stream() helpers — the ergonomic ``chat.completions.stream()``
# / ``responses.stream()`` route through the patched ``create``. The
# shim doesn't patch ``stream()`` directly; these lock that routing
# offline (the real-SDK accumulators are verified by the live e2e
# tests). The fakes model the accumulator iterating the (teed) raw
# stream the SDK helper produces.
# ----------------------------------------------------------------------


def test_openai_chat_stream_helper_routes_through_create(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    with client.chat.completions.stream(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        stream_options={"include_usage": True},
    ) as stream:
        chunks = list(stream)

    assert chunks  # the accumulator yielded the teed chunks
    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "openai"
    assert payload["ledger"]["output_tokens"] == 5
    assert "stream_options_off" not in payload["estimation_flags"]


def test_openai_chat_stream_helper_async(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncCompletions"]()

    async def runner() -> None:
        async with client.stream(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            stream_options={"include_usage": True},
        ) as stream:
            async for _ in stream:
                pass

    asyncio.run(runner())
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 6


def test_openai_responses_stream_helper_routes_through_create(
    tmp_path,
) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["OpenAI"]()
    with client.responses.stream(
        model="gpt-4o", input="hello there"
    ) as stream:
        for _ in stream:
            pass
        final = stream.get_final_response()

    assert final["id"] == "resp_fake_1"
    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "openai"
    assert payload["ledger"]["output_tokens"] == 5
    assert not [
        f
        for f in payload["estimation_flags"]
        if f.startswith("responses_shape_unknown:") or f == "stream_no_usage"
    ]


def test_openai_responses_stream_helper_async(tmp_path) -> None:
    fakes = install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncResponses"]()

    async def runner() -> dict:
        async with client.stream(
            model="gpt-4o", input="hello there"
        ) as stream:
            async for _ in stream:
                pass
            return await stream.get_final_response()

    final = asyncio.run(runner())
    assert final["id"] == "resp_fake_1"
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 6


# ----------------------------------------------------------------------
# Anthropic — create(stream=True)
# ----------------------------------------------------------------------


def test_anthropic_create_stream_uses_message_delta_output(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Anthropic"]()
    list(
        client.messages.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    )
    (payload,) = _llm_events(storage, run_id)
    assert payload["provider"] == "anthropic"
    assert payload["ledger"]["output_tokens"] == 5  # from message_delta
    assert "stream_no_usage" not in payload["estimation_flags"]


def test_anthropic_create_stream_async(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncMessages"]()

    async def runner() -> None:
        stream = await client.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        async for _ in stream:
            pass

    asyncio.run(runner())
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 5


# ----------------------------------------------------------------------
# Anthropic — messages.stream() helper (context manager)
# ----------------------------------------------------------------------


def test_anthropic_manager_get_final_message_emits_one_event(
    tmp_path,
) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Anthropic"]()
    with client.messages.stream(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        final = stream.get_final_message()

    assert final["content"][0]["text"] == "ack"
    (payload,) = _llm_events(storage, run_id)
    assert payload["ledger"]["output_tokens"] == 5


def test_anthropic_manager_text_stream_emits_one_event(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Anthropic"]()
    with client.messages.stream(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        text = "".join(part for part in stream.text_stream)

    assert text == "ack"
    assert len(_llm_events(storage, run_id)) == 1


def test_anthropic_async_manager_emits_one_event(tmp_path) -> None:
    fakes = install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["AsyncMessages"]()

    async def runner() -> dict:
        async with client.stream(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            return await stream.get_final_message()

    final = asyncio.run(runner())
    assert final["content"][0]["text"] == "ack-async"
    assert len(_llm_events(storage, run_id)) == 1


def test_anthropic_create_stream_captures_tool_use_names(tmp_path) -> None:
    from tests.unit import _fake_sdks

    fakes = install_fake_anthropic()

    def _streamer(self, *args, **kwargs):
        fakes["calls"].append({"variant": "sync", "kwargs": kwargs})
        return iter(
            _fake_sdks.anthropic_stream_events(
                "msg_tool", tool_name="get_weather"
            )
        )

    fakes["Messages"].create = _streamer
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Messages"]()
    list(client.create(model="claude-sonnet-4-6", messages=[], stream=True))
    (payload,) = _llm_events(storage, run_id)
    assert payload["tools_called"] == ["get_weather"]


def test_manager_without_raw_stream_skips_capture_but_yields(
    tmp_path,
) -> None:
    # If SDK internals drift and a stream exposes no ``_raw_stream``,
    # capture is skipped rather than breaking the user's iteration.
    from inkfoot.shims._streaming import _StreamManagerProxy

    class _NoRawStream:
        def __iter__(self):
            return iter(["a", "b"])

    class _Manager:
        def __enter__(self):
            return _NoRawStream()

        def __exit__(self, *exc):
            return False

    called = {"factory": 0}

    def _factory():
        called["factory"] += 1
        raise AssertionError("recorder must not be built when teeing fails")

    proxy = _StreamManagerProxy(_Manager(), _factory)
    with proxy as stream:
        assert list(stream) == ["a", "b"]
    # No recorder built, no crash.
    assert called["factory"] == 0


# ----------------------------------------------------------------------
# Terminal-less close → stream_no_usage + tokeniser estimate
#
# OpenAI chat has its own ``stream_options_off`` flag (covered above);
# these pin the generic ``stream_no_usage`` path for the providers that
# normally *do* carry terminal usage but didn't (an abandoned or
# truncated stream).
# ----------------------------------------------------------------------


def test_anthropic_stream_without_message_delta_flags_no_usage(
    tmp_path,
) -> None:
    from tests.unit import _fake_sdks

    fakes = install_fake_anthropic()

    def _streamer(self, *args, **kwargs):
        fakes["calls"].append({"variant": "sync", "kwargs": kwargs})
        return iter(
            _fake_sdks.anthropic_stream_events(
                "msg_no_delta", with_message_delta=False
            )
        )

    fakes["Messages"].create = _streamer
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Messages"]()
    list(client.create(model="claude-sonnet-4-6", messages=[], stream=True))
    (payload,) = _llm_events(storage, run_id)
    assert "stream_no_usage" in payload["estimation_flags"]
    assert "output_tokens" in payload["estimation_flags"]
    # "ack" tokenises to a small positive count — never zero.
    assert payload["ledger"]["output_tokens"] > 0


def test_responses_stream_without_completed_flags_no_usage(tmp_path) -> None:
    from tests.unit import _fake_sdks

    fakes = install_fake_openai()

    def _streamer(self, *args, **kwargs):
        fakes["responses_calls"].append({"variant": "sync", "kwargs": kwargs})
        return iter(
            _fake_sdks.openai_responses_stream_events(
                "resp_no_completed", with_completed=False
            )
        )

    fakes["Responses"].create = _streamer
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    instrument_mod.instrument(storage=storage)
    run_id = _seed(storage)

    client = fakes["Responses"]()
    list(client.create(model="gpt-4o", input="hi", stream=True))
    (payload,) = _llm_events(storage, run_id)
    assert "stream_no_usage" in payload["estimation_flags"]
    assert "output_tokens" in payload["estimation_flags"]
    assert payload["ledger"]["output_tokens"] > 0
