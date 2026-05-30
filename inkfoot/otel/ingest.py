"""Local OTLP/JSON ingest listener (Phase 1 / E3-S2).

Spins up an :class:`http.server.ThreadingHTTPServer` on a
configurable port (default 4318, the OTLP HTTP default) and
accepts ``POST /v1/traces`` requests carrying GenAI-shaped spans.
Each accepted span is translated into a :class:`NeutralCall` via
:mod:`inkfoot.otel.mapping` and persisted to the active storage
backend as an ``llm_call`` event.

Per ADR-1-2 a user running both auto-OTel and the native shim
will produce two events per call. We de-duplicate by
``(span_id, response_id)``: the shim's event lands first
(synchronous code path), the OTel hop arrives later through the
collector and is silently dropped. The dedup table is per
process, LRU-bounded so a long-lived collector relationship
can't pin unbounded memory.

OTLP/protobuf is **not** supported in Phase 1 — Content-Type:
application/x-protobuf returns 415 Unsupported Media Type with a
remediation hint. Most collectors can be configured to use the
JSON encoder; that's the lighter-dep path for E3.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional

from ulid import ULID

from inkfoot.otel.conventions import (
    GEN_AI_RESPONSE_ID,
    INKFOOT_RUN_ID,
    INKFOOT_SEQUENCE,
)
from inkfoot.otel.mapping import attrs_to_neutral_call


_LOG = logging.getLogger("inkfoot.otel.ingest")


# Cap on the dedup memo so a heavy ingest pipeline can't leak.
# Older entries fall out; a duplicate that arrives after eviction
# would be re-ingested (acceptable trade-off for a bounded table).
_DEDUP_CACHE_MAX = 4096


# Synthetic-run task used when a span doesn't carry an
# ``inkfoot.run_id`` extension attribute. The aggregate view +
# inkfoot report can be filtered to this task for "ingest-only"
# inspection.
DEFAULT_INGEST_TASK = "otel-ingest"


class IngestError(RuntimeError):
    """Raised when ingest can't translate or persist an incoming span."""


def _attr_value(otel_attr: Mapping[str, Any]) -> Any:
    """Decode one OTLP/JSON `KeyValue` value object.

    OTLP/JSON encodes attribute values inside a one-key dict:
    ``{"stringValue": "..."}`` / ``{"intValue": "42"}`` /
    ``{"doubleValue": 1.5}`` / ``{"boolValue": true}`` /
    ``{"arrayValue": {"values": [...]}}``. We hand back the
    Python-native form so the mapping layer doesn't have to know
    about OTLP shapes.
    """
    if not isinstance(otel_attr, Mapping):
        return None
    if "stringValue" in otel_attr:
        return otel_attr["stringValue"]
    if "intValue" in otel_attr:
        # OTLP encodes 64-bit ints as JSON strings to dodge JSON's
        # 53-bit float precision limit. Cast back to int.
        try:
            return int(otel_attr["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in otel_attr:
        return otel_attr["doubleValue"]
    if "boolValue" in otel_attr:
        return bool(otel_attr["boolValue"])
    if "arrayValue" in otel_attr:
        arr = otel_attr["arrayValue"].get("values") or []
        return [_attr_value(v) for v in arr]
    return None


def _decode_attributes(attrs_list: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten a list of OTLP/JSON `KeyValue` records into a dict."""
    out: dict[str, Any] = {}
    for entry in attrs_list:
        if not isinstance(entry, Mapping):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        out[key] = _attr_value(entry.get("value") or {})
    return out


def _otel_nanos_to_ms(ns: Any) -> int:
    """Convert an OTLP nanosecond timestamp (string or int) to ms."""
    if ns is None:
        return 0
    try:
        return int(int(ns) // 1_000_000)
    except (TypeError, ValueError):
        return 0


class _DedupCache:
    """LRU dict keyed on ``(span_id, response_id)``.

    Tests poke this directly; production code only touches it via
    :meth:`see_or_add`."""

    def __init__(self, max_entries: int = _DEDUP_CACHE_MAX) -> None:
        self._max = max_entries
        self._seen: "OrderedDict[tuple[str, str], None]" = OrderedDict()
        self._lock = threading.Lock()

    def see_or_add(self, span_id: str, response_id: str) -> bool:
        """Return True the first time this key is seen, False on dup."""
        key = (span_id or "", response_id or "")
        if not key[0] and not key[1]:
            # No identifying info at all — never call this dedup.
            return True
        with self._lock:
            if key in self._seen:
                # LRU touch: move to the end so it stays warm.
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


class OTLPHTTPReceiver:
    """Stdlib HTTP receiver for OTLP/JSON traces.

    The receiver is owned by :func:`inkfoot.instrument` — production
    code never instantiates it directly. Tests do, to drive an
    in-process server.

    Lifecycle::

        recv = OTLPHTTPReceiver(host="127.0.0.1", port=0, persist=fn)
        recv.start()           # binds + serves in a background thread
        ...                    # POSTs land in ``persist``
        recv.stop(timeout=2.0) # graceful shutdown

    ``port=0`` lets the OS pick a free port — handy for tests. The
    chosen port is exposed via :attr:`port` after :meth:`start`.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4318,
        persist: Callable[[dict[str, Any]], Optional[bool]],
        dedup: Optional[_DedupCache] = None,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._persist = persist
        self._dedup = dedup or _DedupCache()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stats = {"accepted": 0, "duplicates": 0, "rejected": 0}

    @property
    def port(self) -> int:
        if self._server is None:
            return self._requested_port
        return self._server.server_address[1]

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def dedup_size(self) -> int:
        return len(self._dedup)

    def start(self) -> None:
        if self._server is not None:
            return
        handler = self._build_handler()
        self._server = ThreadingHTTPServer((self._host, self._requested_port), handler)
        # Keep the worker threads daemon-y so an unhandled crash in
        # the host process doesn't leave receiver threads alive.
        self._server.daemon_threads = True
        thread = threading.Thread(
            target=self._server.serve_forever,
            name="inkfoot-otel-ingest",
            daemon=True,
        )
        thread.start()
        self._thread = thread
        _LOG.info(
            "OTel ingest listening on http://%s:%d/v1/traces",
            self._host,
            self.port,
        )

    def stop(self, timeout: float = 2.0) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None

    # ------------------------------------------------------------------
    # Translation core — exercised directly by unit tests.
    # ------------------------------------------------------------------

    def ingest_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        """Translate a parsed OTLP/JSON request body and persist it.

        Returns a stats delta for the request — accepted /
        duplicates / rejected counts — which the handler logs and
        the tests assert on. Exceptions from :meth:`_persist_call`
        are caught per-span so one bad span doesn't drop the rest.
        """
        accepted = 0
        duplicates = 0
        rejected = 0
        for span in _iter_spans(payload):
            try:
                if self._handle_span(span):
                    accepted += 1
                else:
                    duplicates += 1
            except Exception:  # pylint: disable=broad-except
                rejected += 1
                _LOG.warning(
                    "ingest: failed to translate span %s",
                    span.get("spanId"),
                    exc_info=True,
                )
        self._stats["accepted"] += accepted
        self._stats["duplicates"] += duplicates
        self._stats["rejected"] += rejected
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
        }

    def _handle_span(self, span: Mapping[str, Any]) -> bool:
        """Translate + persist one span. Returns False on dedup hit."""
        attrs_list = list(span.get("attributes") or [])
        attrs = _decode_attributes(attrs_list)
        span_id = str(span.get("spanId") or "")
        response_id = str(attrs.get(GEN_AI_RESPONSE_ID) or "")
        if not self._dedup.see_or_add(span_id, response_id):
            return False
        started_at = _otel_nanos_to_ms(span.get("startTimeUnixNano"))
        ended_at = _otel_nanos_to_ms(span.get("endTimeUnixNano"))
        call = attrs_to_neutral_call(
            attrs, started_at=started_at, ended_at=ended_at
        )
        run_id = attrs.get(INKFOOT_RUN_ID)
        sequence_attr = attrs.get(INKFOOT_SEQUENCE)
        try:
            sequence = int(sequence_attr) if sequence_attr is not None else None
        except (TypeError, ValueError):
            sequence = None
        envelope = {
            "neutral_call": call,
            "trace_id": str(span.get("traceId") or ""),
            "span_id": span_id,
            "response_id": response_id,
            "run_id": str(run_id) if run_id else None,
            "sequence": sequence,
        }
        self._persist(envelope)
        return True

    # ------------------------------------------------------------------
    # HTTP handler factory.
    # ------------------------------------------------------------------

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            # Silence the default per-request stderr log; we have our
            # own structured logger.
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                _LOG.debug("ingest-http: " + format, *args)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/traces":
                    self._send_text(HTTPStatus.NOT_FOUND, "not found")
                    return
                ctype = self.headers.get("Content-Type", "")
                if "application/x-protobuf" in ctype:
                    self._send_text(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        "inkfoot Phase 1 ingest accepts application/json only "
                        "(configure your collector to use the OTLP JSON encoder)",
                    )
                    return
                if "application/json" not in ctype:
                    self._send_text(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        "expected Content-Type: application/json",
                    )
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    self._send_text(
                        HTTPStatus.BAD_REQUEST,
                        f"invalid JSON: {exc}",
                    )
                    return
                if not isinstance(payload, Mapping):
                    self._send_text(
                        HTTPStatus.BAD_REQUEST,
                        "OTLP body must be a JSON object",
                    )
                    return
                stats = receiver.ingest_payload(dict(payload))
                self._send_json(HTTPStatus.OK, {"partialSuccess": {}, **{"_inkfoot": stats}})

            def _send_text(self, status: HTTPStatus, body: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return _Handler


def _iter_spans(payload: Mapping[str, Any]):
    """Yield every `Span` object inside an OTLP/JSON request body.

    Shape: ``{ resourceSpans: [ { scopeSpans: [ { spans: [ ... ] } ] } ] }``.
    Each level is optional in malformed inputs; we walk defensively
    so a partial payload still yields whatever spans it does carry.
    """
    for rs in payload.get("resourceSpans") or []:
        if not isinstance(rs, Mapping):
            continue
        scope_spans = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for ss in scope_spans:
            if not isinstance(ss, Mapping):
                continue
            for span in ss.get("spans") or []:
                if isinstance(span, Mapping):
                    yield span


# ----------------------------------------------------------------------
# Storage-backed persistence helper. Used by ``instrument()`` when
# ``otel_ingest_port`` is set.
# ----------------------------------------------------------------------


def storage_persist_factory(
    *,
    storage: Any,
    default_task: str = DEFAULT_INGEST_TASK,
) -> Callable[[dict[str, Any]], None]:
    """Return a ``persist`` callable that writes ingested NeutralCalls
    to ``storage``.

    Run grouping: when a span carries an ``inkfoot.run_id``
    attribute we honour it; otherwise we synthesise a run keyed on
    the OTLP ``trace_id`` so spans of the same upstream trace land
    under one inkfoot run. New runs are inserted on first sight.
    """
    # Cache the trace_id -> run_id mapping so back-to-back spans
    # for the same trace land under one synthesised run.
    trace_to_run: dict[str, str] = {}
    lock = threading.Lock()

    def _resolve_run(envelope: dict[str, Any]) -> str:
        explicit = envelope.get("run_id")
        if explicit:
            return explicit
        trace_id = envelope.get("trace_id") or ""
        with lock:
            existing = trace_to_run.get(trace_id)
            if existing:
                return existing
            run_id = f"run-{ULID()}"
            trace_to_run[trace_id] = run_id
            return run_id

    def persist(envelope: dict[str, Any]) -> None:
        from dataclasses import asdict  # noqa: PLC0415

        from inkfoot.shims._emit import _next_sequence  # noqa: PLC0415

        call = envelope["neutral_call"]
        run_id = _resolve_run(envelope)
        # Start a synthetic run row if we just minted the run_id.
        if envelope.get("run_id") is None:
            try:
                storage.start_run(
                    run_id=run_id,
                    task=default_task,
                    agent_kind="otel-ingest",
                    started_at=int(call.started_at),
                )
            except Exception:  # pragma: no cover — defensive
                # A second span on the same trace lost the race to
                # insert; storage's PK constraint trips. That's the
                # right semantics (one run per trace) — ignore.
                _LOG.debug(
                    "ingest: start_run race ignored for run %s",
                    run_id,
                    exc_info=True,
                )

        sequence_hint = envelope.get("sequence")
        sequence = (
            int(sequence_hint)
            if sequence_hint is not None
            else _next_sequence(run_id)
        )
        payload_json = json.dumps(asdict(call), default=str)
        storage.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=int(call.ended_at),
            sequence=sequence,
            payload_json=payload_json,
            capture_mode="metadata",
        )

    return persist


__all__ = [
    "DEFAULT_INGEST_TASK",
    "IngestError",
    "OTLPHTTPReceiver",
    "storage_persist_factory",
]
