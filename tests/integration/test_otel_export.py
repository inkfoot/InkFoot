"""Integration test for the OTLP exporter against an in-process
HTTP server playing the role of an OTel collector.

A real ``ThreadingHTTPServer`` accepts the OTLP/JSON POSTs the
exporter emits; the test asserts that exactly one span lands per
``llm_call`` event and that all 14 ledger fields survive the
round-trip.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.otel.conventions import (
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_CAUSE_FIELDS,
    INKFOOT_ESTIMATED_NANODOLLARS,
    cause_attr,
)
from inkfoot.otel.export import ExportTransport, OTLPExporter


class _CollectorServer:
    """Tiny in-process OTel collector stand-in."""

    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any, **kw: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                body = json.loads(raw or b"{}")
                with collector._lock:
                    if self.path == "/v1/traces":
                        collector.traces.append(body)
                    elif self.path == "/v1/logs":
                        collector.logs.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


@pytest.fixture
def collector():
    c = _CollectorServer()
    c.start()
    yield c
    c.stop()


def _populated_event(event_id: str = "evt-1") -> dict[str, Any]:
    ledger_kwargs = {name: idx + 1 for idx, name in enumerate(INKFOOT_CAUSE_FIELDS)}
    call = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=1_700_000_000_000,
        ended_at=1_700_000_001_000,
        ledger=CausalTokenLedger(**ledger_kwargs, output_tokens=999),
        estimated_nanodollars=12_345,
        estimation_flags=("tokeniser_fallback",),
    )
    return {
        "event_id": event_id,
        "run_id": "run-int",
        "kind": "llm_call",
        "occurred_at": call.ended_at,
        "sequence": 1,
        "payload_json": json.dumps(asdict(call)),
        "capture_mode": "metadata",
    }


def _wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_exporter_round_trips_all_fourteen_ledger_fields(collector):
    transport = ExportTransport(endpoint=collector.base_url)
    exporter = OTLPExporter(
        transport=transport, batch_size=1, batch_interval_s=0.05
    )
    exporter.start()
    try:
        exporter.enqueue_event(_populated_event())
        assert _wait_until(lambda: len(collector.traces) >= 1)
    finally:
        exporter.stop()

    span = collector.traces[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    # 13 cause attrs + output token attr — 14 ledger fields in total.
    for idx, field_name in enumerate(INKFOOT_CAUSE_FIELDS):
        assert int(attrs[cause_attr(field_name)]["intValue"]) == idx + 1
    assert int(attrs[GEN_AI_USAGE_OUTPUT_TOKENS]["intValue"]) == 999
    assert int(attrs[INKFOOT_ESTIMATED_NANODOLLARS]["intValue"]) == 12_345


def test_exporter_continues_when_collector_is_unreachable(caplog):
    # No collector — endpoint points to a closed port. The
    # exporter must not raise; the failure surfaces as a WARN log
    # and the stats counter.
    import logging

    transport = ExportTransport(endpoint="http://127.0.0.1:1")
    exporter = OTLPExporter(
        transport=transport, batch_size=1, batch_interval_s=0.05
    )
    with caplog.at_level(logging.WARNING, logger="inkfoot.otel.export"):
        exporter.start()
        try:
            exporter.enqueue_event(_populated_event())
            assert _wait_until(lambda: exporter.stats["failures"] >= 1, timeout=3)
        finally:
            exporter.stop()
    assert any("export failed" in r.message for r in caplog.records)
