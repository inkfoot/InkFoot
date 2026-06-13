"""Property-based fuzzing of the streaming chunk-identity invariant.

Whatever a provider's stream yields — however long, whatever the chunk
objects are — the observer must hand the caller back the *same objects,
in the same order, with nothing added or dropped*. Instrumentation that
mutated a stream would be a correctness bug far worse than a missing
metric, so this is fuzzed rather than spot-checked: hypothesis throws
chunk sequences at every provider's wrap (sync and async) — the real
probe + recorder + emit path, only the storage stubbed — and asserts
identity is preserved.

Two strategies feed each wrap:

* **Opaque sentinels** — ``object()`` instances. The strongest identity
  proof: the tee can't have copied or reconstructed something it can't
  even introspect.
* **Per-provider realistic events** — fuzzed Anthropic ``MessageStream``
  events, OpenAI ``ChatCompletionChunk``s, and Responses
  ``ResponseStreamEvent``s (built from the shared fake-SDK builders,
  with terminal/tool/usage shapes varied). These additionally exercise
  each probe's parsing across realistic shapes while the identity
  invariant is asserted.

Wired into the default CI run (not gated behind a live marker) so a
regression here fails fast on every PR.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from inkfoot._run_context import (  # noqa: E402
    _clear_current_run,
    _reset_ambient_run,
)
from inkfoot.ledger import CausalTokenLedger  # noqa: E402
from inkfoot.normalise import NeutralCall  # noqa: E402
from inkfoot.policy import CallContext  # noqa: E402
from inkfoot.shims._streaming import (  # noqa: E402
    _AnthropicStreamProbe,
    _AsyncStreamedCallObserver,
    _OpenAIChatStreamProbe,
    _OpenAIResponsesStreamProbe,
    _StreamedCallObserver,
    _StreamRecorder,
)
from tests.unit._fake_sdks import (  # noqa: E402
    anthropic_stream_events,
    openai_chat_stream_chunks,
    openai_responses_stream_events,
)

PROBES = (
    _AnthropicStreamProbe,
    _OpenAIChatStreamProbe,
    _OpenAIResponsesStreamProbe,
)


class _NoopStorage:
    """Swallows event writes — the property under test is chunk
    identity, not persistence, and the emit path is isolated so a
    no-op storage exercises it without a database."""

    def insert_event(self, **kwargs) -> None:
        return None


class _NullTranslator:
    """Never raises on fuzzed (unrecognised) chunks, so the recorder's
    finalise runs end to end without translator noise."""

    provider = "test"

    def translate(self, **kwargs) -> NeutralCall:
        return NeutralCall(
            provider="test",
            model="gpt-4o",
            started_at=kwargs.get("started_at", 0),
            ended_at=kwargs.get("ended_at", 0),
            ledger=CausalTokenLedger(),
            sequence=kwargs.get("sequence", 0),
            cache_status="n/a",
        )


class _AsyncList:
    def __init__(self, items) -> None:
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _recorder(probe_cls) -> _StreamRecorder:
    ctx = CallContext(
        provider="test",
        model="gpt-4o",
        run_id="prop-run",
        request_kwargs={"model": "gpt-4o", "messages": []},
    )
    return _StreamRecorder(
        ctx=ctx,
        probe=probe_cls(model="gpt-4o"),
        started_at=1,
        storage=_NoopStorage(),
        capture_mode_getter=lambda: "aggregate",
        translator=_NullTranslator(),
        before_decisions=[],
    )


def _reset() -> None:
    _reset_ambient_run()
    _clear_current_run()


def _assert_identity_sync(events, probe_cls) -> None:
    events = list(events)
    _reset()
    try:
        observer = _StreamedCallObserver(iter(events), _recorder(probe_cls))
        out = list(observer)
        assert len(out) == len(events), (
            f"length changed: {len(out)} != {len(events)}"
        )
        for i, (produced, source) in enumerate(zip(out, events)):
            assert produced is source, (
                f"diverged at {i}: {produced!r} != {source!r}"
            )
    finally:
        _reset()


def _assert_identity_async(events, probe_cls) -> None:
    events = list(events)
    _reset()
    try:
        observer = _AsyncStreamedCallObserver(
            _AsyncList(events), _recorder(probe_cls)
        )

        async def drain():
            return [chunk async for chunk in observer]

        out = asyncio.run(drain())
        assert len(out) == len(events), (
            f"length changed: {len(out)} != {len(events)}"
        )
        for i, (produced, source) in enumerate(zip(out, events)):
            assert produced is source, (
                f"diverged at {i}: {produced!r} != {source!r}"
            )
    finally:
        _reset()


# --- opaque-sentinel strategy --------------------------------------------
# Distinct identities matter, content does not — a unique sentinel per
# element lets us assert ``is`` equality on something the tee can't copy.
_chunk_lists = st.lists(st.builds(object), min_size=0, max_size=40)
_probe_index = st.integers(min_value=0, max_value=len(PROBES) - 1)


@settings(max_examples=100, deadline=None)
@given(chunks=_chunk_lists, probe_index=_probe_index)
def test_sync_observer_preserves_opaque_chunk_identity(
    chunks, probe_index
) -> None:
    _assert_identity_sync(chunks, PROBES[probe_index])


@settings(max_examples=100, deadline=None)
@given(chunks=_chunk_lists, probe_index=_probe_index)
def test_async_observer_preserves_opaque_chunk_identity(
    chunks, probe_index
) -> None:
    _assert_identity_async(chunks, PROBES[probe_index])


# --- per-provider realistic-event strategies -----------------------------
# Each builds the provider's on-wire event sequence with fuzzed
# terminal / tool / usage shapes, paired with the probe that parses it.
_tool_name = st.one_of(st.none(), st.text(min_size=1, max_size=12))

_anthropic_case = st.builds(
    anthropic_stream_events,
    message_id=st.just("msg_fuzz"),
    text=st.text(max_size=24),
    input_tokens=st.integers(min_value=0, max_value=2000),
    output_tokens=st.integers(min_value=0, max_value=2000),
    cache_read=st.integers(min_value=0, max_value=1000),
    cache_creation=st.integers(min_value=0, max_value=1000),
    tool_name=_tool_name,
    with_message_delta=st.booleans(),
).map(lambda events: (_AnthropicStreamProbe, events))

_chat_case = st.builds(
    openai_chat_stream_chunks,
    chunk_id=st.just("chatcmpl_fuzz"),
    text=st.text(max_size=24),
    include_usage=st.booleans(),
    prompt_tokens=st.integers(min_value=0, max_value=2000),
    completion_tokens=st.integers(min_value=0, max_value=2000),
    tool_name=_tool_name,
).map(lambda events: (_OpenAIChatStreamProbe, events))

_responses_case = st.builds(
    openai_responses_stream_events,
    response_id=st.just("resp_fuzz"),
    text=st.text(max_size=24),
    model=st.just("gpt-4o"),
    input_tokens=st.integers(min_value=0, max_value=2000),
    output_tokens=st.integers(min_value=0, max_value=2000),
    with_completed=st.booleans(),
).map(lambda events: (_OpenAIResponsesStreamProbe, events))

_provider_case = st.one_of(_anthropic_case, _chat_case, _responses_case)


@settings(max_examples=150, deadline=None)
@given(case=_provider_case)
def test_sync_observer_preserves_realistic_chunk_identity(case) -> None:
    probe_cls, events = case
    _assert_identity_sync(events, probe_cls)


@settings(max_examples=150, deadline=None)
@given(case=_provider_case)
def test_async_observer_preserves_realistic_chunk_identity(case) -> None:
    probe_cls, events = case
    _assert_identity_async(events, probe_cls)
