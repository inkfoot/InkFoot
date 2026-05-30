"""Test that ``inkfoot.instrument(otel_*=...)`` wires the receiver +
exporter correctly and that ``shutdown()`` tears them down.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from inkfoot import _instrument
from inkfoot._instrument import instrument, shutdown
from inkfoot.otel.conventions import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    cause_attr,
)


def _collector_server() -> tuple[ThreadingHTTPServer, list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any, **kw: Any) -> None:  # noqa: A003
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                received.append(json.loads(raw))
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


@pytest.fixture(autouse=True)
def _isolation():
    shutdown()
    yield
    shutdown()


def test_instrument_starts_ingest_listener_when_port_set(tmp_path):
    from inkfoot.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(path=tmp_path / "wired.db")
    instrument(storage=storage, otel_ingest_port=0, otel_ingest_host="127.0.0.1")
    try:
        receiver = _instrument._OTEL_INGEST
        assert receiver is not None
        # POST a span and assert it lands in storage.
        body = json.dumps(
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "tt",
                                        "spanId": "ss",
                                        "startTimeUnixNano": "1",
                                        "endTimeUnixNano": "2",
                                        "attributes": [
                                            {
                                                "key": GEN_AI_SYSTEM,
                                                "value": {"stringValue": "anthropic"},
                                            },
                                            {
                                                "key": GEN_AI_REQUEST_MODEL,
                                                "value": {
                                                    "stringValue": "claude-haiku-4-5"
                                                },
                                            },
                                            {
                                                "key": cause_attr("user_input_tokens"),
                                                "value": {"intValue": "10"},
                                            },
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ).encode("utf-8")
        url = f"http://127.0.0.1:{receiver.port}/v1/traces"
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        )
        conn = storage._conn()
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE kind='llm_call'")
        assert cur.fetchone()[0] == 1
    finally:
        shutdown()
    assert _instrument._OTEL_INGEST is None


def test_instrument_wires_export_tap_when_endpoint_set(tmp_path):
    from inkfoot.shims._emit import _next_sequence
    from inkfoot.storage.sqlite import SQLiteStorage
    from ulid import ULID

    server, received = _collector_server()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    storage = SQLiteStorage(path=tmp_path / "wired-export.db")
    instrument(storage=storage, otel_export_endpoint=base_url)
    try:
        exporter = _instrument._OTEL_EXPORTER
        assert exporter is not None
        from dataclasses import asdict

        from inkfoot.ledger import CausalTokenLedger
        from inkfoot.normalise import NeutralCall

        call = NeutralCall(
            provider="anthropic",
            model="claude-haiku-4-5",
            started_at=1,
            ended_at=2,
            ledger=CausalTokenLedger(user_input_tokens=5, output_tokens=2),
        )
        run_id = "run-wired"
        storage.start_run(
            run_id=run_id, task="wired", agent_kind="test", started_at=1
        )
        # Note: _STORAGE is the WRAPPED storage. insert_event goes
        # through the tap → exporter queue → HTTP POST.
        _instrument._STORAGE.insert_event(
            event_id=str(ULID()),
            run_id=run_id,
            kind="llm_call",
            occurred_at=2,
            sequence=_next_sequence(run_id),
            payload_json=json.dumps(asdict(call)),
            capture_mode="metadata",
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not received:
            time.sleep(0.02)
        assert received, "exporter never reached collector"
    finally:
        shutdown()
        server.shutdown()
        server.server_close()
    assert _instrument._OTEL_EXPORTER is None


def test_instrument_with_no_otel_kwargs_leaves_optional_handles_unset(tmp_path):
    from inkfoot.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(path=tmp_path / "vanilla.db")
    instrument(storage=storage)
    try:
        assert _instrument._OTEL_INGEST is None
        assert _instrument._OTEL_EXPORTER is None
    finally:
        shutdown()
