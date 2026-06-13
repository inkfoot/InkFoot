"""Unit tests for the streaming observers.

The observers are pure plumbing: they must yield the provider's chunks
**by identity** (never copy, reorder, or withhold one), tee each to the
recorder, and finalise exactly once however the stream ends —
exhaustion, explicit close, context-manager exit, or a mid-stream
provider error. These tests pin that contract with a recorder spy and
sentinel chunks, with no SDK or storage in the loop.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from inkfoot.shims._streaming import (
    _AsyncStreamedCallObserver,
    _StreamedCallObserver,
)


class _RecorderSpy:
    """Stands in for :class:`_StreamRecorder` — records what the
    observer hands it without touching storage."""

    def __init__(self) -> None:
        self.chunks: list = []
        self.finalised = 0
        self.errors: list = []

    def on_chunk(self, chunk) -> None:
        self.chunks.append(chunk)

    def finalise(self) -> None:
        self.finalised += 1

    def on_error(self, exc) -> None:
        self.errors.append(exc)


def _raising_iter(items, exc):
    yield from items
    raise exc


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


async def _araising(items, exc):
    for item in items:
        yield item
    raise exc


# ----------------------------------------------------------------------
# Sync
# ----------------------------------------------------------------------


def test_sync_yields_chunks_by_identity() -> None:
    c1, c2, c3 = object(), object(), object()
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(iter([c1, c2, c3]), spy)

    out = list(observer)

    assert out == [c1, c2, c3]
    for produced, source in zip(out, (c1, c2, c3)):
        assert produced is source
    for teed, source in zip(spy.chunks, (c1, c2, c3)):
        assert teed is source


def test_sync_finalises_once_on_exhaustion() -> None:
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(iter([1, 2]), spy)
    list(observer)
    assert spy.finalised == 1


def test_sync_finalise_is_idempotent_across_close_after_exhaustion() -> None:
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(iter([1]), spy)
    list(observer)
    observer.close()
    observer.close()
    assert spy.finalised == 1


def test_sync_close_before_exhaustion_finalises() -> None:
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(iter([1, 2, 3]), spy)
    assert next(observer) == 1
    observer.close()
    assert spy.finalised == 1
    assert spy.errors == []


def test_sync_context_manager_exit_finalises() -> None:
    spy = _RecorderSpy()
    with _StreamedCallObserver(iter([1, 2]), spy) as observer:
        assert next(observer) == 1
    assert spy.finalised == 1


def test_sync_provider_error_reports_and_reraises_without_close_emit() -> None:
    spy = _RecorderSpy()
    boom = ValueError("provider blew up")
    observer = _StreamedCallObserver(_raising_iter([1, 2], boom), spy)

    assert next(observer) == 1
    assert next(observer) == 2
    with pytest.raises(ValueError):
        next(observer)

    assert spy.errors == [boom]
    # The error path must not also emit a success event at close.
    assert spy.finalised == 0


def test_sync_base_exception_passes_through_untouched() -> None:
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(
        _raising_iter([1], KeyboardInterrupt()), spy
    )
    assert next(observer) == 1
    with pytest.raises(KeyboardInterrupt):
        next(observer)
    # BaseException is a shutdown signal — neither a failure event nor a
    # success event should be synthesised from it.
    assert spy.errors == []
    assert spy.finalised == 0


def test_sync_abandoned_stream_finalises_on_gc() -> None:
    spy = _RecorderSpy()
    observer = _StreamedCallObserver(iter([1, 2, 3]), spy)
    assert next(observer) == 1  # partially consumed, then abandoned
    del observer
    gc.collect()
    assert spy.finalised == 1


def test_sync_getattr_delegates_to_underlying_stream() -> None:
    class _Stream:
        def __init__(self) -> None:
            self.response = "carried"

        def __iter__(self):
            return iter([1])

    spy = _RecorderSpy()
    observer = _StreamedCallObserver(_Stream(), spy)
    assert observer.response == "carried"


# ----------------------------------------------------------------------
# Async
# ----------------------------------------------------------------------


def test_async_yields_chunks_by_identity() -> None:
    c1, c2 = object(), object()
    spy = _RecorderSpy()
    observer = _AsyncStreamedCallObserver(_AsyncList([c1, c2]), spy)

    async def drain():
        return [chunk async for chunk in observer]

    out = asyncio.run(drain())
    assert out == [c1, c2]
    assert out[0] is c1 and out[1] is c2
    assert spy.chunks[0] is c1 and spy.chunks[1] is c2
    assert spy.finalised == 1


def test_async_context_manager_exit_finalises() -> None:
    spy = _RecorderSpy()

    async def run():
        async with _AsyncStreamedCallObserver(_AsyncList([1, 2]), spy) as obs:
            return await obs.__anext__()

    assert asyncio.run(run()) == 1
    assert spy.finalised == 1


def test_async_abandoned_stream_finalises_on_gc() -> None:
    spy = _RecorderSpy()

    async def partial() -> None:
        observer = _AsyncStreamedCallObserver(_AsyncList([1, 2, 3]), spy)
        assert await observer.__anext__() == 1  # then dropped

    asyncio.run(partial())
    gc.collect()
    # The async observer's __del__ runs the same synchronous finalise as
    # the sync one — the GC-finalize doc claim holds for both.
    assert spy.finalised == 1


def test_async_provider_error_reports_and_reraises() -> None:
    spy = _RecorderSpy()
    boom = RuntimeError("async provider blew up")

    async def run():
        observer = _AsyncStreamedCallObserver(_araising([1], boom), spy)
        seen = []
        with pytest.raises(RuntimeError):
            async for chunk in observer:
                seen.append(chunk)
        return seen

    seen = asyncio.run(run())
    assert seen == [1]
    assert spy.errors == [boom]
    assert spy.finalised == 0
