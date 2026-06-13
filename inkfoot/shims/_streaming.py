"""Streaming-capture framework shared by every provider shim.

A non-streaming call hands the shim a finished response object, so
the existing ``emit_llm_call`` pipeline runs synchronously inside the
SDK call. A streaming call returns *immediately* with an iterator (or
a context manager wrapping one); the usage numbers only exist once the
caller has drained it. So the capture has to ride along with that
iterator and fire when it ends.

The pieces:

* :class:`_StreamedCallObserver` / :class:`_AsyncStreamedCallObserver`
  are transparent proxies. They yield the provider's chunks **byte for
  byte** — never copied, reordered, or withheld — tee each one to a
  per-call probe, and finalise (emit one ``llm_call`` event) when the
  underlying iterator is exhausted, closed, or the surrounding ``with``
  block exits.

* A :class:`_StreamProbe` is the per-provider terminal detector. Its
  ``observe(chunk)`` accumulates the assistant text / tool names / id
  the translator needs and records the authoritative usage the moment
  it sees the terminal chunk (Anthropic's ``message_delta``, OpenAI's
  final usage chunk, the Responses ``response.completed`` event), which
  ``has_terminal_usage()`` then reports. At close the recorder calls
  ``build_response()`` to hand the accumulated state to the translator;
  when no terminal chunk arrived, the probe estimates the output tokens
  from the text it buffered and the event is flagged accordingly.

* :class:`_StreamRecorder` bridges a probe to ``emit_llm_call``. It
  carries the cross-layer dedup invariant that the non-streaming path
  gets for free. ``emit_llm_call``'s gate is first-emit-wins; a
  stream-close emit can land *after* the LangChain handler's
  ``on_llm_end`` for the same call, which would let the handler's
  thinner event win. The recorder makes the gate effectively
  directional by *claiming* the provider response id at the first chunk
  that exposes it — always before the handler can run, because the
  handler only emits once the same stream is fully consumed. At close
  the recorder emits with ``skip_dedup=True`` if it won the claim, and
  stays silent if it lost.

Provider shims own the install/patch wiring; this module owns the
proxy mechanics, the probes, and the recorder.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from inkfoot.providers.base import coerce_token_count
from inkfoot.shims._emit import (
    _now_ms,
    emit_llm_call,
    emit_llm_call_error,
)
from inkfoot.shims._isolation import safely_run
from inkfoot.tokenisers import tokenise

_LOG = logging.getLogger("inkfoot.shims.streaming")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a dict (test fixtures) or as an attribute
    (real SDK pydantic events)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _estimate_output_tokens(text: str, model: str) -> int:
    """Tokeniser estimate of an assistant message we only have as text
    (the stream ended before any usage-bearing chunk). Never raises —
    a tokeniser failure falls back to a coarse chars/4 guess so the
    event still carries a plausible number rather than zero."""
    if not text:
        return 0
    try:
        return tokenise(text, model).value
    except Exception:  # pylint: disable=broad-except
        return max(1, len(text) // 4)


# ----------------------------------------------------------------------
# Per-provider probes
# ----------------------------------------------------------------------


class _StreamProbe:
    """Per-provider terminal detector. ``observe`` accumulates the
    state the translator needs (assistant text, tool names, id, and the
    authoritative usage from the terminal chunk). Terminal detection is
    exposed via :meth:`has_terminal_usage`; the accumulated state is
    rendered by :meth:`build_response` at close. ``observe`` returns
    nothing — the recorder reads the probe's state rather than a
    per-chunk return value."""

    provider = ""

    def observe(self, chunk: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def response_id(self) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def has_terminal_usage(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def build_response(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def estimation_flags(self) -> tuple[str, ...]:
        """Flags merged onto the emitted event. ``output_tokens`` joins
        the generic ``stream_no_usage`` whenever the output had to be
        tokeniser-estimated, mirroring how the translators flag an
        estimated input category by its name."""
        if self.has_terminal_usage():
            return ()
        return ("stream_no_usage", "output_tokens")


class _AnthropicStreamProbe(_StreamProbe):
    """Anthropic message-stream events. ``message_start`` carries the
    id and the input/cache counters; text and tool-use names arrive in
    ``content_block_*`` events; ``message_delta`` carries the final
    cumulative ``output_tokens`` (the terminal); ``message_stop`` is a
    bare close marker."""

    provider = "anthropic"

    def __init__(self, model: str) -> None:
        self._model = model
        self._id: Optional[str] = None
        self._fresh_input = 0
        self._cache_read = 0
        self._cache_creation = 0
        self._output_tokens: Optional[int] = None
        self._text_parts: list[str] = []
        self._tool_blocks: list[dict[str, Any]] = []

    def observe(self, chunk: Any) -> None:
        etype = _get(chunk, "type")
        if etype == "message_start":
            message = _get(chunk, "message")
            rid = _get(message, "id")
            if isinstance(rid, str) and rid:
                self._id = rid
            usage = _get(message, "usage")
            if usage is not None:
                self._fresh_input = coerce_token_count(
                    _get(usage, "input_tokens")
                )
                self._cache_read = coerce_token_count(
                    _get(usage, "cache_read_input_tokens")
                )
                self._cache_creation = coerce_token_count(
                    _get(usage, "cache_creation_input_tokens")
                )
        elif etype == "content_block_start":
            block = _get(chunk, "content_block")
            if _get(block, "type") == "tool_use":
                name = _get(block, "name")
                if isinstance(name, str) and name:
                    self._tool_blocks.append(
                        {"type": "tool_use", "name": name}
                    )
        elif etype == "content_block_delta":
            delta = _get(chunk, "delta")
            if _get(delta, "type") == "text_delta":
                text = _get(delta, "text")
                if isinstance(text, str):
                    self._text_parts.append(text)
        elif etype == "message_delta":
            usage = _get(chunk, "usage")
            if usage is not None:
                out = _get(usage, "output_tokens")
                if isinstance(out, int) and not isinstance(out, bool):
                    self._output_tokens = out

    def response_id(self) -> Optional[str]:
        return self._id

    def has_terminal_usage(self) -> bool:
        return self._output_tokens is not None

    def build_response(self) -> dict[str, Any]:
        text = "".join(self._text_parts)
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(self._tool_blocks)
        output_tokens = (
            self._output_tokens
            if self._output_tokens is not None
            else _estimate_output_tokens(text, self._model)
        )
        # Anthropic wire shape: ``input_tokens`` is the *fresh* count
        # (cache excluded); ``map_usage`` re-adds the cache portions.
        return {
            "id": self._id,
            "content": content,
            "usage": {
                "input_tokens": self._fresh_input,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": self._cache_read,
                "cache_creation_input_tokens": self._cache_creation,
            },
        }


class _OpenAIChatStreamProbe(_StreamProbe):
    """OpenAI Chat Completions stream chunks. The id and the assistant
    text/tool-call names accumulate from each ``choices[0].delta``; the
    terminal usage only exists when the caller passed
    ``stream_options={"include_usage": True}`` — then a final chunk with
    empty ``choices`` carries ``usage``. Without it the output is
    estimated and the event is flagged ``stream_options_off``."""

    provider = "openai"

    def __init__(self, model: str) -> None:
        self._model = model
        self._id: Optional[str] = None
        self._text_parts: list[str] = []
        self._tool_names: dict[int, str] = {}
        self._usage: Optional[dict[str, Any]] = None

    def observe(self, chunk: Any) -> None:
        rid = _get(chunk, "id")
        if isinstance(rid, str) and rid:
            self._id = rid
        choices = _get(chunk, "choices") or []
        if choices:
            delta = _get(choices[0], "delta")
            if delta is not None:
                content = _get(delta, "content")
                if isinstance(content, str):
                    self._text_parts.append(content)
                for call in _get(delta, "tool_calls") or []:
                    index = _get(call, "index", 0) or 0
                    fn = _get(call, "function")
                    name = _get(fn, "name") if fn is not None else None
                    if isinstance(name, str) and name:
                        self._tool_names[index] = name
        usage = _get(chunk, "usage")
        if usage is not None:
            self._usage = _chat_usage_to_dict(usage)

    def response_id(self) -> Optional[str]:
        return self._id

    def has_terminal_usage(self) -> bool:
        return self._usage is not None

    def estimation_flags(self) -> tuple[str, ...]:
        if self.has_terminal_usage():
            return ()
        return ("stream_options_off", "output_tokens")

    def build_response(self) -> dict[str, Any]:
        text = "".join(self._text_parts)
        tool_calls = [
            {"function": {"name": name}}
            for _, name in sorted(self._tool_names.items())
        ]
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if self._usage is not None:
            usage = self._usage
        else:
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": _estimate_output_tokens(
                    text, self._model
                ),
            }
        return {
            "id": self._id,
            "choices": [{"message": message}],
            "usage": usage,
        }


def _chat_usage_to_dict(usage: Any) -> dict[str, Any]:
    if isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {
        "prompt_tokens": _get(usage, "prompt_tokens", 0),
        "completion_tokens": _get(usage, "completion_tokens", 0),
        "total_tokens": _get(usage, "total_tokens", 0),
    }
    pt_details = _get(usage, "prompt_tokens_details")
    if pt_details is not None:
        out["prompt_tokens_details"] = {
            "cached_tokens": _get(pt_details, "cached_tokens", 0),
        }
    ct_details = _get(usage, "completion_tokens_details")
    if ct_details is not None:
        out["completion_tokens_details"] = {
            "reasoning_tokens": _get(ct_details, "reasoning_tokens", 0),
        }
    return out


class _OpenAIResponsesStreamProbe(_StreamProbe):
    """OpenAI Responses stream events. Every event echoes the
    ``response`` object (so the id is available from the first event);
    ``response.completed`` carries the finished object — full
    ``output`` and ``usage`` — which is handed to the translator
    verbatim. A stream that ends without it falls back to an estimate
    from the buffered ``output_text`` deltas."""

    provider = "openai"

    def __init__(self, model: str) -> None:
        self._model = model
        self._id: Optional[str] = None
        self._response: Any = None
        self._text_parts: list[str] = []

    def observe(self, chunk: Any) -> None:
        response = _get(chunk, "response")
        if response is not None:
            rid = _get(response, "id")
            if isinstance(rid, str) and rid:
                self._id = rid
        etype = _get(chunk, "type")
        if etype == "response.completed" and response is not None:
            # The completed event carries the finished Response object —
            # ``build_response`` hands it to the translator verbatim, so
            # there's no usage to compute here.
            self._response = response
        elif etype == "response.output_text.delta":
            delta = _get(chunk, "delta")
            if isinstance(delta, str):
                self._text_parts.append(delta)

    def response_id(self) -> Optional[str]:
        return self._id

    def has_terminal_usage(self) -> bool:
        return self._response is not None

    def build_response(self) -> Any:
        if self._response is not None:
            return self._response
        text = "".join(self._text_parts)
        return {
            "id": self._id,
            "output": [],
            "usage": {
                "input_tokens": 0,
                "output_tokens": _estimate_output_tokens(text, self._model),
            },
        }


# ----------------------------------------------------------------------
# Recorder — claim-early / emit-late dedup bridge
# ----------------------------------------------------------------------


class _StreamRecorder:
    """Drives a probe through a streamed call and emits exactly one
    event at the end. Owns the cross-layer dedup claim (see module
    docstring) so the shim's richer event wins over a LangChain handler
    observing the same call."""

    def __init__(
        self,
        *,
        ctx: Any,
        probe: _StreamProbe,
        started_at: int,
        storage: Any,
        capture_mode_getter: Callable[[], str],
        translator: Any,
        before_decisions: list,
    ) -> None:
        self._ctx = ctx
        self._probe = probe
        self._started_at = started_at
        self._storage = storage
        self._capture_mode_getter = capture_mode_getter
        self._translator = translator
        self._before_decisions = before_decisions
        # Tri-state: None = no id claimed yet, True = we own the id,
        # False = another layer already owns it (skip the emit).
        self._claim: Optional[bool] = None
        self._done = False

    def on_chunk(self, chunk: Any) -> None:
        safely_run(
            self._probe.observe, chunk, hook_label="stream_probe.observe"
        )
        self._maybe_claim()

    def _maybe_claim(self) -> None:
        if self._claim is not None:
            return
        rid = safely_run(
            self._probe.response_id, hook_label="stream_probe.response_id"
        )
        if not rid:
            return
        from inkfoot._run_lifecycle import (  # noqa: PLC0415
            _record_emitted_response_id,
        )

        self._claim = safely_run(
            _record_emitted_response_id,
            self._ctx.run_id,
            rid,
            fallback=True,
            hook_label="_record_emitted_response_id",
        )

    def finalise(self) -> None:
        if self._done:
            return
        self._done = True
        # Last chance to claim — some streams only reveal the id on the
        # terminal event.
        self._maybe_claim()
        if self._claim is False:
            _LOG.debug(
                "stream response already recorded for run %s; "
                "skipping duplicate emit",
                self._ctx.run_id,
            )
            return
        ended_at = _now_ms()
        response = safely_run(
            self._probe.build_response,
            hook_label="stream_probe.build_response",
        )
        flags = (
            safely_run(
                self._probe.estimation_flags,
                fallback=(),
                hook_label="stream_probe.estimation_flags",
            )
            or ()
        )
        rid = safely_run(
            self._probe.response_id, hook_label="stream_probe.response_id"
        )
        # ``skip_dedup`` is always safe here: when we won the claim the
        # id is already recorded (a second record would suppress our own
        # emit); when no id ever appeared we fail open exactly like the
        # non-streaming path.
        safely_run(
            emit_llm_call,
            ctx=self._ctx,
            response=response,
            started_at=self._started_at,
            ended_at=ended_at,
            storage=self._storage,
            capture_mode=self._capture_mode_getter(),
            translator=self._translator,
            before_decisions=self._before_decisions,
            response_id=rid,
            skip_dedup=True,
            extra_estimation_flags=tuple(flags),
            hook_label="emit_llm_call",
        )
        from inkfoot.policy.registry import PolicyRegistry  # noqa: PLC0415

        safely_run(
            PolicyRegistry.after_call,
            self._ctx,
            response,
            hook_label="PolicyRegistry.after_call",
        )

    def on_error(self, exc: BaseException) -> None:
        if self._done:
            return
        self._done = True
        ended_at = _now_ms()
        safely_run(
            emit_llm_call_error,
            ctx=self._ctx,
            exc=exc,
            started_at=self._started_at,
            ended_at=ended_at,
            storage=self._storage,
            capture_mode=self._capture_mode_getter(),
            before_decisions=self._before_decisions,
            hook_label="emit_llm_call_error",
        )


# ----------------------------------------------------------------------
# Observers — transparent stream proxies
# ----------------------------------------------------------------------


class _StreamedCallObserver:
    """Synchronous tee. Yields the underlying iterator's chunks
    unchanged, hands each to the recorder, and finalises when the
    iterator is exhausted, the proxy is closed, or its ``with`` block
    exits."""

    def __init__(self, stream: Any, recorder: _StreamRecorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iterator: Any = None
        self._finalised = False

    def __iter__(self) -> "_StreamedCallObserver":
        return self

    def _ensure_iter(self) -> Any:
        if self._iterator is None:
            self._iterator = iter(self._stream)
        return self._iterator

    def __next__(self) -> Any:
        iterator = self._ensure_iter()
        try:
            chunk = next(iterator)
        except StopIteration:
            self._finalise()
            raise
        except Exception as exc:  # provider error mid-stream
            # Mark finalised so a later close()/__exit__/__del__ can't
            # emit a second (success) event for the same call.
            self._finalised = True
            self._recorder.on_error(exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

    def _finalise(self) -> None:
        if self._finalised:
            return
        self._finalised = True
        self._recorder.finalise()

    def close(self) -> None:
        self._finalise()
        closer = getattr(self._stream, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> "_StreamedCallObserver":
        enter = getattr(self._stream, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        self._finalise()
        exiter = getattr(self._stream, "__exit__", None)
        if callable(exiter):
            return exiter(*exc_info)
        return False

    def __getattr__(self, name: str) -> Any:
        stream = self.__dict__.get("_stream")
        if stream is None:
            raise AttributeError(name)
        return getattr(stream, name)

    def __del__(self) -> None:  # last-resort finalise for abandoned streams
        try:
            self._finalise()
        except Exception:  # pylint: disable=broad-except
            pass


class _AsyncStreamedCallObserver:
    """Asynchronous tee — :class:`_StreamedCallObserver` for
    ``async for`` / ``async with`` streams. The emit itself stays
    synchronous (local SQLite writes), matching the non-streaming async
    path."""

    def __init__(self, stream: Any, recorder: _StreamRecorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iterator: Any = None
        self._finalised = False

    def __aiter__(self) -> "_AsyncStreamedCallObserver":
        return self

    def _ensure_aiter(self) -> Any:
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        return self._iterator

    async def __anext__(self) -> Any:
        iterator = self._ensure_aiter()
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            self._finalise()
            raise
        except Exception as exc:  # provider error mid-stream
            self._finalised = True
            self._recorder.on_error(exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

    def _finalise(self) -> None:
        if self._finalised:
            return
        self._finalised = True
        self._recorder.finalise()

    async def aclose(self) -> None:
        self._finalise()
        closer = getattr(self._stream, "aclose", None)
        if callable(closer):
            await closer()

    async def __aenter__(self) -> "_AsyncStreamedCallObserver":
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        self._finalise()
        exiter = getattr(self._stream, "__aexit__", None)
        if exiter is not None:
            return await exiter(*exc_info)
        return False

    def __getattr__(self, name: str) -> Any:
        stream = self.__dict__.get("_stream")
        if stream is None:
            raise AttributeError(name)
        return getattr(stream, name)

    def __del__(self) -> None:
        # Last-resort finalise for an async stream abandoned without
        # ``aclose`` / ``async with`` / full drain. ``_finalise`` and the
        # emit it triggers are synchronous (local SQLite writes), so this
        # needs no event loop — matching the sync observer's __del__.
        try:
            self._finalise()
        except Exception:  # pylint: disable=broad-except
            pass


# ----------------------------------------------------------------------
# Manager proxies — for the helper APIs that hand back a context
# manager (Anthropic's ``messages.stream()``) instead of a raw iterator
# ----------------------------------------------------------------------


class _StreamManagerProxy:
    """Wraps a sync stream *manager* whose ``__enter__`` builds a rich
    stream object that pulls every consumption mode (text iterator,
    event iterator, ``get_final_message``, ``until_done``) from a
    single ``_raw_stream`` attribute. Swapping that attribute with an
    observer on entry tees all of them at once; if the attribute is
    absent (SDK internals drifted) capture is skipped rather than
    risking the user's stream."""

    def __init__(
        self,
        manager: Any,
        recorder_factory: Callable[[], _StreamRecorder],
    ) -> None:
        self._manager = manager
        self._recorder_factory = recorder_factory
        self._observer: Optional[_StreamedCallObserver] = None

    def __enter__(self) -> Any:
        stream = self._manager.__enter__()
        raw = getattr(stream, "_raw_stream", None)
        if raw is None:
            _LOG.debug(
                "stream manager exposed no _raw_stream; capture skipped"
            )
            return stream
        self._observer = _StreamedCallObserver(raw, self._recorder_factory())
        try:
            stream._raw_stream = self._observer
        except Exception:  # pylint: disable=broad-except
            _LOG.debug("could not tee _raw_stream; capture skipped")
            self._observer = None
        return stream

    def __exit__(self, *exc_info: Any) -> Any:
        if self._observer is not None:
            self._observer._finalise()
        return self._manager.__exit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        manager = self.__dict__.get("_manager")
        if manager is None:
            raise AttributeError(name)
        return getattr(manager, name)


class _AsyncStreamManagerProxy:
    """:class:`_StreamManagerProxy` for ``async with`` managers."""

    def __init__(
        self,
        manager: Any,
        recorder_factory: Callable[[], _StreamRecorder],
    ) -> None:
        self._manager = manager
        self._recorder_factory = recorder_factory
        self._observer: Optional[_AsyncStreamedCallObserver] = None

    async def __aenter__(self) -> Any:
        stream = await self._manager.__aenter__()
        raw = getattr(stream, "_raw_stream", None)
        if raw is None:
            _LOG.debug(
                "async stream manager exposed no _raw_stream; "
                "capture skipped"
            )
            return stream
        self._observer = _AsyncStreamedCallObserver(
            raw, self._recorder_factory()
        )
        try:
            stream._raw_stream = self._observer
        except Exception:  # pylint: disable=broad-except
            _LOG.debug("could not tee _raw_stream; capture skipped")
            self._observer = None
        return stream

    async def __aexit__(self, *exc_info: Any) -> Any:
        if self._observer is not None:
            self._observer._finalise()
        return await self._manager.__aexit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        manager = self.__dict__.get("_manager")
        if manager is None:
            raise AttributeError(name)
        return getattr(manager, name)
