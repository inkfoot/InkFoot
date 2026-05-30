"""OTLP/JSON exporter.

Taps the event stream emitted by the shim and forwards each event
to an OTel collector over OTLP/JSON HTTP. ``llm_call`` events
become spans (``POST /v1/traces``); ``smell`` / ``outcome``
events become logs (``POST /v1/logs``).

Design constraints:

* **Never block the agent.** The shim's hot path is sensitive
  (§9.1 perf budgets). Exports run on a background thread fed by
  a bounded queue; ``llm_call``s under load that overflow the
  queue are dropped with a WARN rather than back-pressuring the
  shim.
* **Fail-safe.** A failing collector logs a WARN and the exporter
  continues. The agent keeps running even if the OTel backend is
  unreachable.
* **Stdlib-only.** OTLP/JSON is a documented wire format;
  ``urllib`` handles the HTTP. Avoids pulling in
  ``opentelemetry-sdk`` for a feature most users won't enable.

The exporter is wired in via :func:`inkfoot.instrument` when the
``otel_export_endpoint`` kwarg is set. It wraps the storage's
``insert_event`` so the tap is exactly where new events are born.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any, Callable, Mapping, Optional

from inkfoot.normalise import NeutralCall, dict_to_neutral_call
from inkfoot.otel.conventions import (
    INKFOOT_EVENT_KIND,
    INKFOOT_RUN_ID,
    INKFOOT_SEQUENCE,
)
from inkfoot.otel.mapping import neutral_call_to_attrs


_LOG = logging.getLogger("inkfoot.otel.export")


# Defaults tuned for "fits a single OTLP request without splitting".
# A real workload will rarely emit fast enough to fill the queue;
# the bound exists so a stalled collector can't exhaust memory.
DEFAULT_BATCH_SIZE = 64
DEFAULT_BATCH_INTERVAL_S = 1.0
DEFAULT_QUEUE_CAPACITY = 1024
DEFAULT_EXPORT_TIMEOUT_S = 5.0


class ExportTransport:
    """Pluggable transport — tests inject a fake to assert payloads.

    Default implementation POSTs OTLP/JSON via urllib. The base
    URL is split into ``/v1/traces`` and ``/v1/logs`` per the OTLP
    HTTP spec.
    """

    def __init__(
        self, *, endpoint: str, timeout: float = DEFAULT_EXPORT_TIMEOUT_S
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    def post_traces(self, body: Mapping[str, Any]) -> None:
        self._post(f"{self._endpoint}/v1/traces", body)

    def post_logs(self, body: Mapping[str, Any]) -> None:
        self._post(f"{self._endpoint}/v1/logs", body)

    def _post(self, url: str, body: Mapping[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            # OTLP success responses are usually empty. We read +
            # discard the body so the socket gets closed cleanly.
            resp.read()


class OTLPExporter:
    """Batched event-to-OTLP forwarder.

    Public API: :meth:`enqueue_event` is called by the storage tap
    (see :func:`tap_storage`). Internally a background thread
    drains the queue every :attr:`batch_interval_s` (or sooner if
    the queue reaches :attr:`batch_size`) and posts to the
    transport.
    """

    def __init__(
        self,
        *,
        transport: ExportTransport,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_interval_s: float = DEFAULT_BATCH_INTERVAL_S,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        service_name: str = "inkfoot",
    ) -> None:
        self._transport = transport
        self._batch_size = max(1, batch_size)
        self._batch_interval_s = max(0.05, batch_interval_s)
        self._queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(
            maxsize=queue_capacity
        )
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._service_name = service_name
        self._stats = {"queued": 0, "dropped": 0, "exported": 0, "failures": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run_loop,
            name="inkfoot-otel-export",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        # Sentinel wakes the worker even if the queue is empty.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        # Drain anything left over so an explicit stop() doesn't
        # silently lose events the agent already produced.
        self._flush_remaining()

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Event tap
    # ------------------------------------------------------------------

    def enqueue_event(self, event: dict[str, Any]) -> None:
        """Hand an event row to the exporter. Never raises.

        The shim's hot path calls this every ``insert_event``; it
        must be cheap and safe to invoke without locks. Queue full
        is recorded as a drop and a WARN, never as backpressure.
        """
        try:
            self._queue.put_nowait(event)
            self._stats["queued"] += 1
        except queue.Full:
            self._stats["dropped"] += 1
            _LOG.warning(
                "OTel export queue full (%d) — dropping event %s; "
                "consider raising queue_capacity or check that the "
                "collector at the configured endpoint is healthy",
                self._queue.maxsize,
                event.get("event_id") or "<unknown>",
            )

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            # Block briefly so we batch under sustained load but
            # also flush promptly when traffic is sparse.
            timeout = max(0.01, self._batch_interval_s - (time.monotonic() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                if batch:
                    self._flush(batch)
                    batch = []
                last_flush = time.monotonic()
                continue
            if item is None:
                if batch:
                    self._flush(batch)
                break
            batch.append(item)
            if len(batch) >= self._batch_size:
                self._flush(batch)
                batch = []
                last_flush = time.monotonic()

    def _flush_remaining(self) -> None:
        leftovers: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            leftovers.append(item)
        if leftovers:
            self._flush(leftovers)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        spans: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []
        for ev in batch:
            kind = ev.get("kind")
            try:
                if kind == "llm_call":
                    spans.append(self._build_span(ev))
                elif kind in {"smell", "outcome"}:
                    logs.append(self._build_log(ev))
                # Other event kinds (run_start / run_end / checkpoint
                # / policy events) are intentionally not exported in
                # they would clutter the GenAI view without
                # adding signal an OTel backend can use. A future Cloud backend
                # can export them as logs.
            except Exception:  # pylint: disable=broad-except
                _LOG.warning(
                    "OTel export: failed to build payload for event %s",
                    ev.get("event_id"),
                    exc_info=True,
                )
        if spans:
            self._send(
                "traces",
                {"resourceSpans": [self._wrap_spans(spans)]},
                count=len(spans),
            )
        if logs:
            self._send(
                "logs",
                {"resourceLogs": [self._wrap_logs(logs)]},
                count=len(logs),
            )

    def _send(
        self, kind: str, body: Mapping[str, Any], *, count: int
    ) -> None:
        """Post one OTLP batch.

        The caller passes ``count`` (the number of spans / log
        records in this batch) so the exported counter reflects
        events, not batches (round-2 review #2)."""
        try:
            if kind == "traces":
                self._transport.post_traces(body)
            else:
                self._transport.post_logs(body)
            self._stats["exported"] += int(count)
        except urllib.error.URLError as exc:
            self._stats["failures"] += 1
            _LOG.warning(
                "OTel %s export failed: %s — continuing without blocking the agent",
                kind,
                exc,
            )
        except Exception:  # pylint: disable=broad-except
            self._stats["failures"] += 1
            _LOG.warning(
                "OTel %s export raised; continuing without blocking the agent",
                kind,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Span / log builders
    # ------------------------------------------------------------------

    def _build_span(self, event: Mapping[str, Any]) -> dict[str, Any]:
        call = _restore_neutral_call(event)
        attrs = neutral_call_to_attrs(
            call,
            response_id=event.get("event_id"),
            run_id=event.get("run_id"),
            sequence=int(event.get("sequence") or 0),
        )
        attrs[INKFOOT_EVENT_KIND] = "llm_call"
        start_ns = int(call.started_at) * 1_000_000
        end_ns = int(call.ended_at) * 1_000_000
        return {
            "traceId": _trace_id_for(event.get("run_id") or "unknown"),
            "spanId": _span_id_for(event.get("event_id") or "unknown"),
            "name": f"{call.provider}.{call.model}",
            "kind": 3,  # SPAN_KIND_CLIENT
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": _encode_attrs(attrs),
        }

    def _build_log(self, event: Mapping[str, Any]) -> dict[str, Any]:
        attrs = {
            INKFOOT_EVENT_KIND: str(event.get("kind") or ""),
            INKFOOT_RUN_ID: str(event.get("run_id") or ""),
            INKFOOT_SEQUENCE: int(event.get("sequence") or 0),
        }
        body = event.get("payload_json")
        # Pass the original JSON text through unchanged (round-2
        # review #9). Re-encoding via ``json.loads/dumps`` only
        # normalises whitespace at the cost of a wasted round-trip
        # and an indexer's view that doesn't match what storage
        # actually wrote.
        if isinstance(body, bytes):
            body_text = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            body_text = body
        else:
            body_text = json.dumps(body or {})
        occurred = int(event.get("occurred_at") or 0) * 1_000_000
        # Severity: smells warn, outcomes are info.
        severity = 9 if event.get("kind") == "smell" else 5
        return {
            "timeUnixNano": str(occurred),
            "observedTimeUnixNano": str(occurred),
            "severityNumber": severity,
            "severityText": "WARN" if severity == 9 else "INFO",
            "body": {"stringValue": body_text},
            "attributes": _encode_attrs(attrs),
        }

    def _wrap_spans(self, spans: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "resource": {
                "attributes": _encode_attrs(
                    {"service.name": self._service_name}
                )
            },
            "scopeSpans": [
                {
                    "scope": {"name": "inkfoot.otel.export"},
                    "spans": spans,
                }
            ],
        }

    def _wrap_logs(self, logs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "resource": {
                "attributes": _encode_attrs(
                    {"service.name": self._service_name}
                )
            },
            "scopeLogs": [
                {
                    "scope": {"name": "inkfoot.otel.export"},
                    "logRecords": logs,
                }
            ],
        }


# ----------------------------------------------------------------------
# Storage tap
# ----------------------------------------------------------------------


class _ExportingStorage:
    """Proxy around a real Storage that tees ``insert_event`` to an
    :class:`OTLPExporter`. Other Storage methods pass through
    unchanged."""

    def __init__(
        self,
        *,
        wrapped: Any,
        exporter: OTLPExporter,
    ) -> None:
        self._wrapped = wrapped
        self._exporter = exporter

    def __getattr__(self, name: str) -> Any:
        # Anything not explicitly overridden falls through to the
        # wrapped storage. Defines ``__getattr__`` not ``__getattribute__``
        # so explicit overrides (e.g. ``insert_event``) take priority.
        return getattr(self._wrapped, name)

    def insert_event(self, **kwargs: Any) -> None:
        self._wrapped.insert_event(**kwargs)
        # Build the export envelope from the kwargs we already have;
        # never read back from storage on the hot path.
        self._exporter.enqueue_event(dict(kwargs))


def tap_storage(storage: Any, exporter: OTLPExporter) -> Any:
    """Return a Storage-shaped wrapper that mirrors writes to ``exporter``."""
    return _ExportingStorage(wrapped=storage, exporter=exporter)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _restore_neutral_call(event: Mapping[str, Any]) -> NeutralCall:
    """Deserialise the payload_json back into a NeutralCall."""
    raw = event.get("payload_json")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        payload = json.loads(raw)
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise ValueError(
            f"OTel export: event {event.get('event_id')!r} has no usable payload_json"
        )
    return dict_to_neutral_call(payload)


def _trace_id_for(run_id: str) -> str:
    """Derive a 32-hex-char trace id from a run id (deterministic)."""
    return _hash_hex(run_id, 32)


def _span_id_for(event_id: str) -> str:
    """Derive a 16-hex-char span id from an event id."""
    return _hash_hex(event_id, 16)


def _hash_hex(value: str, length: int) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _encode_attrs(attrs: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, value in attrs.items():
        if isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        elif isinstance(value, float):
            out.append({"key": key, "value": {"doubleValue": value}})
        elif isinstance(value, str):
            out.append({"key": key, "value": {"stringValue": value}})
        else:
            out.append({"key": key, "value": {"stringValue": json.dumps(value)}})
    return out


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_BATCH_INTERVAL_S",
    "DEFAULT_QUEUE_CAPACITY",
    "ExportTransport",
    "OTLPExporter",
    "tap_storage",
]
