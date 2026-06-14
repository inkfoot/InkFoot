"""Fake Anthropic + OpenAI + Gemini SDK modules for shim tests.

We don't want to depend on the real SDKs in unit tests — they're
heavy, network-coupled, and version-sensitive. The shims monkey-patch
at the *attribute* level, so we stand up just enough of the module
hierarchy:

* ``anthropic.resources.messages.Messages.create`` (sync)
* ``anthropic.resources.messages.AsyncMessages.create`` (async)
* ``openai.resources.chat.completions.Completions.create``
* ``openai.resources.chat.completions.AsyncCompletions.create``
* ``openai.resources.responses.Responses.create``
* ``openai.resources.responses.AsyncResponses.create``
* ``google.generativeai.generative_models.GenerativeModel
  .generate_content`` (+ ``generate_content_async``)

Each fake entry point records its invocation in a call log and
returns a dict that mimics the real provider's usage shape so the
translator can build a ledger. The fake Gemini model also implements
``from_cached_content`` and a ``caching.CachedContent.create``
factory so cache-resource flows can run end to end offline; a
cache-bound model reports 256 cached tokens in its usage.
"""

from __future__ import annotations

import sys
import types
from typing import Any


# ----------------------------------------------------------------------
# Streaming fixtures
# ----------------------------------------------------------------------
#
# Streamed calls hand the shim an *iterator* (or a context manager
# wrapping one) instead of a finished response. The builders below
# return the provider's on-wire event/chunk sequence as plain dicts —
# the probes read them through a dict/attr accessor, so dicts are
# enough to exercise every code path offline.


class _AsyncListIterator:
    """Minimal async iterator over a fixed list — stands in for an
    SDK's async streaming response."""

    def __init__(self, items: list) -> None:
        self._it = iter(items)

    def __aiter__(self) -> "_AsyncListIterator":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def anthropic_stream_events(
    message_id: str,
    *,
    text: str = "ack",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_creation: int = 0,
    tool_name: str | None = None,
    with_message_delta: bool = True,
) -> list[dict]:
    """Anthropic ``messages.stream`` event sequence. ``message_delta``
    carries the terminal cumulative output count; omit it
    (``with_message_delta=False``) to model an abandoned stream that
    forces a tokeniser estimate."""
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    if tool_name is not None:
        events.extend(
            [
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_fake",
                        "name": tool_name,
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": "{}"},
                },
                {"type": "content_block_stop", "index": 1},
            ]
        )
    if with_message_delta:
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            }
        )
    events.append({"type": "message_stop"})
    return events


def openai_chat_stream_chunks(
    chunk_id: str,
    *,
    text: str = "ack",
    include_usage: bool = False,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    tool_name: str | None = None,
) -> list[dict]:
    """OpenAI Chat Completions stream chunks. The trailing usage chunk
    only appears with ``include_usage=True`` (the caller passed
    ``stream_options={"include_usage": True}``)."""
    first_delta: dict[str, Any] = {"role": "assistant", "content": ""}
    chunks: list[dict] = [
        {"id": chunk_id, "choices": [{"index": 0, "delta": first_delta}]},
        {
            "id": chunk_id,
            "choices": [{"index": 0, "delta": {"content": text}}],
        },
    ]
    if tool_name is not None:
        chunks.append(
            {
                "id": chunk_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_fake",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        )
    chunks.append(
        {
            "id": chunk_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    if include_usage:
        chunks.append(
            {
                "id": chunk_id,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )
    return chunks


def openai_responses_stream_events(
    response_id: str,
    *,
    text: str = "ack",
    model: str = "gpt-4o",
    input_tokens: int = 10,
    output_tokens: int = 5,
    with_completed: bool = True,
) -> list[dict]:
    """OpenAI Responses stream events. ``response.completed`` carries
    the finished object (full ``output`` + ``usage``); omit it
    (``with_completed=False``) to model an abandoned stream."""
    events: list[dict] = [
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_text.delta", "delta": text},
    ]
    if with_completed:
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "model": model,
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": text}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": input_tokens + output_tokens,
                    },
                },
            }
        )
    return events


def _accumulate_final_message(events_seen: list[dict], message_id: str) -> dict:
    """Reduce a list of consumed events into the final-message shape the
    real ``get_final_message`` returns."""
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    mid = message_id
    for event in events_seen:
        etype = event.get("type")
        if etype == "message_start":
            mid = (event.get("message") or {}).get("id", mid)
            usage.update((event.get("message") or {}).get("usage") or {})
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
        elif etype == "message_delta":
            usage.update(event.get("usage") or {})
    return {
        "id": mid,
        "content": [{"type": "text", "text": "".join(text_parts)}],
        "usage": usage,
    }


class _FakeMessageStream:
    """Sync ``MessageStream`` stand-in. Every consumption mode pulls
    from ``self._raw_stream`` — the exact contract the shim relies on
    when it swaps that attribute for an observer."""

    def __init__(self, events: list[dict], message_id: str) -> None:
        self._raw_stream: Any = iter(events)
        self._message_id = message_id

    def __iter__(self) -> Any:
        for event in self._raw_stream:
            yield event

    @property
    def text_stream(self) -> Any:
        for event in self._raw_stream:
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")

    def get_final_message(self) -> dict:
        return _accumulate_final_message(
            list(self._raw_stream), self._message_id
        )

    def until_done(self) -> "_FakeMessageStream":
        for _ in self._raw_stream:
            pass
        return self


class _FakeMessageStreamManager:
    def __init__(self, events: list[dict], message_id: str) -> None:
        self._events = events
        self._message_id = message_id

    def __enter__(self) -> _FakeMessageStream:
        return _FakeMessageStream(self._events, self._message_id)

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeAsyncMessageStream:
    def __init__(self, events: list[dict], message_id: str) -> None:
        self._raw_stream: Any = _AsyncListIterator(events)
        self._message_id = message_id

    def __aiter__(self) -> Any:
        return self._event_aiter()

    async def _event_aiter(self) -> Any:
        async for event in self._raw_stream:
            yield event

    @property
    def text_stream(self) -> Any:
        return self._text_aiter()

    async def _text_aiter(self) -> Any:
        async for event in self._raw_stream:
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")

    async def get_final_message(self) -> dict:
        seen: list[dict] = []
        async for event in self._raw_stream:
            seen.append(event)
        return _accumulate_final_message(seen, self._message_id)

    async def until_done(self) -> "_FakeAsyncMessageStream":
        async for _ in self._raw_stream:
            pass
        return self


class _FakeAsyncMessageStreamManager:
    def __init__(self, events: list[dict], message_id: str) -> None:
        self._events = events
        self._message_id = message_id

    async def __aenter__(self) -> _FakeAsyncMessageStream:
        return _FakeAsyncMessageStream(self._events, self._message_id)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def install_fake_anthropic(*, with_bedrock: bool = True) -> dict:
    """Install a fake ``anthropic`` module hierarchy and return its
    call log. Repeated calls return the same log (idempotent).

    ``with_bedrock=True`` also exposes ``AnthropicBedrock`` /
    ``AsyncAnthropicBedrock`` on the module and gives the ``Messages``
    resource a ``_client`` back-reference, so the shim's Bedrock
    detection can run offline. ``with_bedrock=False`` models an
    install without the ``anthropic[bedrock]`` extra — the classes are
    absent, exactly the shape the shim must treat as plain Anthropic.
    """
    if "anthropic" in sys.modules:
        # Test isolation: tear down any leftover fake.
        for key in list(sys.modules):
            if key == "anthropic" or key.startswith("anthropic."):
                del sys.modules[key]

    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    calls: list[dict[str, Any]] = []
    # Exceptions queued here are raised (FIFO) by the next ``create``
    # call — after the attempt is recorded in ``calls`` — so tests can
    # drive the shim's error path offline.
    errors: list[BaseException] = []

    # Response ids mimic the real SDK's ``msg_...`` ids and are unique
    # per call (the emit-path dedup keys on them; a repeated id would
    # wrongly collapse two distinct calls).

    class Messages:
        def __init__(self, client: Any = None) -> None:
            # The real SDK resource stores a back-reference to its
            # owning client here; the shim reads it to tell an
            # AnthropicBedrock client apart from a direct one.
            self._client = client

        def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "sync", "args": args, "kwargs": kwargs})
            if errors:
                raise errors.pop(0)
            if kwargs.get("stream"):
                return iter(
                    anthropic_stream_events(f"msg_fake_{len(calls)}")
                )
            return {
                "id": f"msg_fake_{len(calls)}",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ack"}],
            }

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "stream", "args": args, "kwargs": kwargs})
            if errors:
                raise errors.pop(0)
            message_id = f"msg_fake_{len(calls)}"
            return _FakeMessageStreamManager(
                anthropic_stream_events(message_id), message_id
            )

    class AsyncMessages:
        def __init__(self, client: Any = None) -> None:
            self._client = client

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "async", "args": args, "kwargs": kwargs})
            if errors:
                raise errors.pop(0)
            if kwargs.get("stream"):
                return _AsyncListIterator(
                    anthropic_stream_events(
                        f"msg_fake_{len(calls)}", text="ack-async"
                    )
                )
            return {
                "id": f"msg_fake_{len(calls)}",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 6,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ack-async"}],
            }

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            # Like the real SDK, the async ``stream()`` returns its
            # manager synchronously; the request fires on ``__aenter__``.
            calls.append(
                {"variant": "async-stream", "args": args, "kwargs": kwargs}
            )
            message_id = f"msg_fake_{len(calls)}"
            return _FakeAsyncMessageStreamManager(
                anthropic_stream_events(message_id, text="ack-async"),
                message_id,
            )

    class Anthropic:
        """Client facade like the real SDK's — ``client.messages``
        is an instance of the same ``Messages`` class the shim
        patches, so client calls flow through the patched method."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages(self)

    class AnthropicBedrock:
        """``anthropic[bedrock]`` client facade. It reuses the patched
        ``Messages`` resource class, so calls flow through the same
        wrapper as the direct client; the shim distinguishes the two
        by the client type reachable via ``messages._client``."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages(self)

    class AsyncAnthropicBedrock:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = AsyncMessages(self)

    messages_mod.Messages = Messages
    messages_mod.AsyncMessages = AsyncMessages
    resources_mod.messages = messages_mod
    anthropic_mod.resources = resources_mod
    anthropic_mod.Anthropic = Anthropic
    if with_bedrock:
        anthropic_mod.AnthropicBedrock = AnthropicBedrock
        anthropic_mod.AsyncAnthropicBedrock = AsyncAnthropicBedrock

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod

    return {
        "calls": calls,
        "errors": errors,
        "Messages": Messages,
        "AsyncMessages": AsyncMessages,
        "Anthropic": Anthropic,
        "AnthropicBedrock": AnthropicBedrock,
        "AsyncAnthropicBedrock": AsyncAnthropicBedrock,
        "module": anthropic_mod,
    }


def _event_type(event: Any) -> Any:
    return event.get("type") if isinstance(event, dict) else getattr(
        event, "type", None
    )


def _event_response(event: Any) -> Any:
    return (
        event.get("response")
        if isinstance(event, dict)
        else getattr(event, "response", None)
    )


# --- OpenAI ``stream()`` helper stand-ins --------------------------------
#
# The real ``chat.completions.stream()`` / ``responses.stream()`` helpers
# don't post on their own — their context manager calls
# ``self.create(..., stream=True)`` and wraps the returned ``Stream`` in
# an accumulator that the caller iterates. The shim patches ``create``,
# so these stand-ins reproduce that routing: ``__enter__`` calls the
# (patched) ``create`` and the accumulator iterates the returned object.
# That's the contract the shim relies on instead of patching ``stream()``
# directly — these fakes lock it offline; the live e2e tests verify it
# against the real SDK accumulators.


class _FakeChatCompletionStream:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __iter__(self) -> Any:
        for chunk in self._raw:
            yield chunk

    def __enter__(self) -> "_FakeChatCompletionStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        close = getattr(self._raw, "close", None)
        if callable(close):
            close()
        return False

    def get_final_completion(self) -> None:
        for _ in self._raw:
            pass


class _FakeChatCompletionStreamManager:
    def __init__(self, completions: Any, args: tuple, kwargs: dict) -> None:
        self._completions = completions
        self._args = args
        self._kwargs = kwargs
        self._stream: Any = None

    def __enter__(self) -> _FakeChatCompletionStream:
        raw = self._completions.create(
            *self._args, stream=True, **self._kwargs
        )
        self._stream = _FakeChatCompletionStream(raw)
        return self._stream

    def __exit__(self, *exc: Any) -> bool:
        if self._stream is not None:
            return self._stream.__exit__(*exc)
        return False


class _FakeAsyncChatCompletionStream:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        async for chunk in self._raw:
            yield chunk

    async def __aenter__(self) -> "_FakeAsyncChatCompletionStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        close = getattr(self._raw, "aclose", None)
        if callable(close):
            await close()
        return False


class _FakeAsyncChatCompletionStreamManager:
    def __init__(self, completions: Any, args: tuple, kwargs: dict) -> None:
        self._completions = completions
        self._args = args
        self._kwargs = kwargs
        self._stream: Any = None

    async def __aenter__(self) -> _FakeAsyncChatCompletionStream:
        raw = await self._completions.create(
            *self._args, stream=True, **self._kwargs
        )
        self._stream = _FakeAsyncChatCompletionStream(raw)
        return self._stream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeResponseStream:
    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._final: Any = None

    def __iter__(self) -> Any:
        for event in self._raw:
            if _event_type(event) == "response.completed":
                self._final = _event_response(event)
            yield event

    def get_final_response(self) -> Any:
        for _ in self:
            pass
        return self._final

    def __enter__(self) -> "_FakeResponseStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        close = getattr(self._raw, "close", None)
        if callable(close):
            close()
        return False


class _FakeResponseStreamManager:
    def __init__(self, responses: Any, args: tuple, kwargs: dict) -> None:
        self._responses = responses
        self._args = args
        self._kwargs = kwargs
        self._stream: Any = None

    def __enter__(self) -> _FakeResponseStream:
        raw = self._responses.create(
            *self._args, stream=True, **self._kwargs
        )
        self._stream = _FakeResponseStream(raw)
        return self._stream

    def __exit__(self, *exc: Any) -> bool:
        if self._stream is not None:
            return self._stream.__exit__(*exc)
        return False


class _FakeAsyncResponseStream:
    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._final: Any = None

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        async for event in self._raw:
            if _event_type(event) == "response.completed":
                self._final = _event_response(event)
            yield event

    async def get_final_response(self) -> Any:
        async for event in self._raw:
            if _event_type(event) == "response.completed":
                self._final = _event_response(event)
        return self._final

    async def __aenter__(self) -> "_FakeAsyncResponseStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        close = getattr(self._raw, "aclose", None)
        if callable(close):
            await close()
        return False


class _FakeAsyncResponseStreamManager:
    def __init__(self, responses: Any, args: tuple, kwargs: dict) -> None:
        self._responses = responses
        self._args = args
        self._kwargs = kwargs
        self._stream: Any = None

    async def __aenter__(self) -> _FakeAsyncResponseStream:
        raw = await self._responses.create(
            *self._args, stream=True, **self._kwargs
        )
        self._stream = _FakeAsyncResponseStream(raw)
        return self._stream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def install_fake_openai(*, with_responses: bool = True) -> dict:
    """Install a fake ``openai`` module hierarchy and return its
    call log.

    ``with_responses=False`` mimics an SDK version that predates the
    Responses API — ``openai.resources.responses`` is absent, which
    is exactly the shape the Responses shim must skip gracefully.
    """
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            del sys.modules[key]

    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")
    responses_mod = types.ModuleType("openai.resources.responses")

    calls: list[dict[str, Any]] = []
    responses_calls: list[dict[str, Any]] = []
    # Exceptions queued here are raised (FIFO) by the next Responses
    # ``create`` call — after the attempt is recorded — so tests can
    # drive the shim's error path offline.
    responses_errors: list[BaseException] = []

    def _chat_stream_kwargs(kwargs: dict) -> bool:
        return bool((kwargs.get("stream_options") or {}).get("include_usage"))

    class Completions:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "sync", "args": args, "kwargs": kwargs})
            if kwargs.get("stream"):
                return iter(
                    openai_chat_stream_chunks(
                        f"chatcmpl_fake_{len(calls)}",
                        include_usage=_chat_stream_kwargs(kwargs),
                    )
                )
            return {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "choices": [
                    {"message": {"role": "assistant", "content": "ack"}}
                ],
            }

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            # Routes through the (patched) ``create`` like the real SDK.
            return _FakeChatCompletionStreamManager(self, args, kwargs)

    class AsyncCompletions:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "async", "args": args, "kwargs": kwargs})
            if kwargs.get("stream"):
                return _AsyncListIterator(
                    openai_chat_stream_chunks(
                        f"chatcmpl_fake_{len(calls)}",
                        text="ack-async",
                        include_usage=_chat_stream_kwargs(kwargs),
                        prompt_tokens=11,
                        completion_tokens=6,
                    )
                )
            return {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 6,
                    "total_tokens": 17,
                },
                "choices": [
                    {"message": {"role": "assistant", "content": "ack-async"}}
                ],
            }

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeAsyncChatCompletionStreamManager(self, args, kwargs)

    # Response ids mimic the real API's ``resp_...`` ids and are
    # unique per call (the emit-path dedup keys on them; a repeated
    # id would wrongly collapse two distinct calls).

    def _responses_payload(variant: str, kwargs: dict) -> dict:
        return {
            "id": f"resp_fake_{len(responses_calls)}",
            "object": "response",
            "status": "completed",
            "model": kwargs.get("model", ""),
            "output": [
                {
                    "type": "message",
                    "id": "msg_fake",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ack" if variant == "sync" else "ack-async",
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 10 if variant == "sync" else 11,
                "output_tokens": 5 if variant == "sync" else 6,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15 if variant == "sync" else 17,
            },
        }

    class Responses:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            responses_calls.append(
                {"variant": "sync", "args": args, "kwargs": kwargs}
            )
            if responses_errors:
                raise responses_errors.pop(0)
            if kwargs.get("stream"):
                return iter(
                    openai_responses_stream_events(
                        f"resp_fake_{len(responses_calls)}",
                        model=kwargs.get("model", ""),
                    )
                )
            return _responses_payload("sync", kwargs)

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponseStreamManager(self, args, kwargs)

    class AsyncResponses:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            responses_calls.append(
                {"variant": "async", "args": args, "kwargs": kwargs}
            )
            if responses_errors:
                raise responses_errors.pop(0)
            if kwargs.get("stream"):
                return _AsyncListIterator(
                    openai_responses_stream_events(
                        f"resp_fake_{len(responses_calls)}",
                        text="ack-async",
                        model=kwargs.get("model", ""),
                        input_tokens=11,
                        output_tokens=6,
                    )
                )
            return _responses_payload("async", kwargs)

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeAsyncResponseStreamManager(self, args, kwargs)

    # Embeddings surface. ``embedding_options`` lets a test toggle
    # whether the response carries provider usage (exercising the
    # shim's reported-vs-tokeniser-estimate split).
    embedding_calls: list[dict[str, Any]] = []
    embedding_options: dict[str, Any] = {
        "include_usage": True,
        "prompt_tokens": 7,
    }

    def _embedding_payload(kwargs: dict) -> dict:
        inp = kwargs.get("input")
        if isinstance(inp, str):
            count = 1
        elif isinstance(inp, (list, tuple)):
            count = len(inp) or 1
        else:
            count = 1
        payload: dict[str, Any] = {
            "object": "list",
            "model": kwargs.get("model", ""),
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.0, 0.1, 0.2]}
                for i in range(count)
            ],
        }
        if embedding_options.get("include_usage"):
            pt = int(embedding_options.get("prompt_tokens", 0))
            payload["usage"] = {"prompt_tokens": pt, "total_tokens": pt}
        return payload

    class Embeddings:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            embedding_calls.append(
                {"variant": "sync", "args": args, "kwargs": kwargs}
            )
            return _embedding_payload(kwargs)

    class AsyncEmbeddings:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            embedding_calls.append(
                {"variant": "async", "args": args, "kwargs": kwargs}
            )
            return _embedding_payload(kwargs)

    class _Chat:
        def __init__(self) -> None:
            self.completions = Completions()

    class OpenAI:
        """Client facade like the real SDK's — ``client.chat.completions``
        and ``client.responses`` are instances of the same classes
        the shims patch, so client calls flow through the patched
        methods."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.chat = _Chat()
            self.embeddings = Embeddings()
            if with_responses:
                self.responses = Responses()

    embeddings_mod = types.ModuleType("openai.resources.embeddings")

    completions_mod.Completions = Completions
    completions_mod.AsyncCompletions = AsyncCompletions
    chat_mod.completions = completions_mod
    resources_mod.chat = chat_mod
    embeddings_mod.Embeddings = Embeddings
    embeddings_mod.AsyncEmbeddings = AsyncEmbeddings
    resources_mod.embeddings = embeddings_mod
    openai_mod.resources = resources_mod
    openai_mod.OpenAI = OpenAI

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod
    sys.modules["openai.resources.embeddings"] = embeddings_mod

    if with_responses:
        responses_mod.Responses = Responses
        responses_mod.AsyncResponses = AsyncResponses
        resources_mod.responses = responses_mod
        sys.modules["openai.resources.responses"] = responses_mod

    return {
        "calls": calls,
        "responses_calls": responses_calls,
        "responses_errors": responses_errors,
        "embedding_calls": embedding_calls,
        "embedding_options": embedding_options,
        "Completions": Completions,
        "AsyncCompletions": AsyncCompletions,
        "Responses": Responses,
        "AsyncResponses": AsyncResponses,
        "Embeddings": Embeddings,
        "AsyncEmbeddings": AsyncEmbeddings,
        "OpenAI": OpenAI,
        "module": openai_mod,
    }


def install_fake_gemini() -> dict:
    """Install a fake ``google.generativeai`` module hierarchy and
    return its call log.

    The fake mirrors the SDK surfaces the shim and policies touch:

    * ``GenerativeModel`` with the private construction-time attrs
      the shim reads (``_system_instruction``, ``_tools``,
      ``_generation_config``, ``_safety_settings``), the
      ``model_name`` property (``models/``-prefixed, like the real
      SDK), and both ``generate_content`` variants.
    * ``GenerativeModel.from_cached_content`` — returns a new model
      bound to the cache resource; bound models report 256 cached
      tokens so cache-read/creation attribution is observable.
    * ``caching.CachedContent.create`` — records each creation in
      ``cache_creations`` and returns a uniquely named resource.
    """
    for key in list(sys.modules):
        if key == "google" or key.startswith("google."):
            del sys.modules[key]

    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.generativeai")
    generative_models_mod = types.ModuleType(
        "google.generativeai.generative_models"
    )
    caching_mod = types.ModuleType("google.generativeai.caching")

    calls: list[dict[str, Any]] = []
    cache_creations: list[dict[str, Any]] = []

    class CachedContent:
        _counter = 0

        def __init__(self, name: str, model: Any) -> None:
            self.name = name
            self.model = model

        @classmethod
        def create(cls, model: Any = None, **kwargs: Any) -> "CachedContent":
            cls._counter += 1
            cache_creations.append({"model": model, "kwargs": kwargs})
            return cls(f"cachedContents/fake-{cls._counter}", model)

    class GenerativeModel:
        def __init__(
            self,
            model_name: str = "gemini-1.5-pro",
            *,
            system_instruction: Any = None,
            tools: Any = None,
            generation_config: Any = None,
            safety_settings: Any = None,
            **kwargs: Any,
        ) -> None:
            name = str(model_name)
            if not name.startswith("models/"):
                name = f"models/{name}"
            self._model_name = name
            self._system_instruction = system_instruction
            self._tools = tools
            self._generation_config = generation_config
            self._safety_settings = safety_settings
            self.cached_content: Any = None

        @property
        def model_name(self) -> str:
            return self._model_name

        @classmethod
        def from_cached_content(
            cls, cached_content: Any, **kwargs: Any
        ) -> "GenerativeModel":
            model = (
                getattr(cached_content, "model", None) or "gemini-1.5-pro"
            )
            instance = cls(str(model), **kwargs)
            instance.cached_content = cached_content
            return instance

        def _usage(self, base_input: int, base_output: int) -> dict:
            cached = 256 if self.cached_content is not None else 0
            return {
                "prompt_token_count": base_input + cached,
                "candidates_token_count": base_output,
                "cached_content_token_count": cached,
            }

        def generate_content(
            self, contents: Any = None, **kwargs: Any
        ) -> Any:
            calls.append(
                {
                    "variant": "sync",
                    "model": self,
                    "contents": contents,
                    "kwargs": kwargs,
                    "cached": self.cached_content is not None,
                }
            )
            return {
                "usage_metadata": self._usage(10, 5),
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ack"}],
                        }
                    }
                ],
            }

        async def generate_content_async(
            self, contents: Any = None, **kwargs: Any
        ) -> Any:
            calls.append(
                {
                    "variant": "async",
                    "model": self,
                    "contents": contents,
                    "kwargs": kwargs,
                    "cached": self.cached_content is not None,
                }
            )
            return {
                "usage_metadata": self._usage(11, 6),
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ack-async"}],
                        }
                    }
                ],
            }

    generative_models_mod.GenerativeModel = GenerativeModel
    caching_mod.CachedContent = CachedContent
    genai_mod.GenerativeModel = GenerativeModel
    genai_mod.generative_models = generative_models_mod
    genai_mod.caching = caching_mod
    google_mod.generativeai = genai_mod

    sys.modules["google"] = google_mod
    sys.modules["google.generativeai"] = genai_mod
    sys.modules["google.generativeai.generative_models"] = (
        generative_models_mod
    )
    sys.modules["google.generativeai.caching"] = caching_mod

    return {
        "calls": calls,
        "cache_creations": cache_creations,
        "GenerativeModel": GenerativeModel,
        "CachedContent": CachedContent,
        "module": genai_mod,
    }


def uninstall_fake_sdks() -> None:
    """Drop all fakes from ``sys.modules``."""
    for prefix in ("anthropic", "openai", "google"):
        for key in list(sys.modules):
            if key == prefix or key.startswith(f"{prefix}."):
                del sys.modules[key]
