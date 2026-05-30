"""Integration test for the OTLP HTTP ingest path (Phase 1 / E3-S2 T5).

Boots the receiver on an ephemeral port, issues a real
``POST /v1/traces`` over loopback, and asserts the resulting event
landed in storage. End-to-end through the HTTP handler, JSON
parsing, attribute decoding, dedup, and the storage persistence
factory.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any

import pytest

from inkfoot.otel.conventions import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    cause_attr,
)
from inkfoot.otel.ingest import OTLPHTTPReceiver, storage_persist_factory
from inkfoot.storage.sqlite import SQLiteStorage


def _kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    raise TypeError(type(value).__name__)


def _otlp_request(span_id: str, *, response_id: str) -> dict[str, Any]:
    attrs = {
        GEN_AI_SYSTEM: "anthropic",
        GEN_AI_REQUEST_MODEL: "claude-haiku-4-5",
        GEN_AI_RESPONSE_ID: response_id,
        cause_attr("user_input_tokens"): 100,
        cause_attr("system_static_tokens"): 50,
        GEN_AI_USAGE_OUTPUT_TOKENS: 25,
    }
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "trace-itest",
                                "spanId": span_id,
                                "startTimeUnixNano": str(1_700_000_000_000_000_000),
                                "endTimeUnixNano": str(1_700_000_001_000_000_000),
                                "attributes": [
                                    _kv(k, v) for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def _post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def storage(tmp_path):
    s = SQLiteStorage(path=tmp_path / "ingest.db")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def receiver(storage):
    recv = OTLPHTTPReceiver(
        host="127.0.0.1",
        port=0,
        persist=storage_persist_factory(storage=storage),
    )
    recv.start()
    try:
        yield recv
    finally:
        recv.stop()


def test_post_traces_persists_an_llm_call_event(storage, receiver):
    status, body = _post(
        f"http://127.0.0.1:{receiver.port}/v1/traces",
        _otlp_request("span-int-1", response_id="resp-int-1"),
    )
    assert status == 200
    assert body["_inkfoot"]["accepted"] == 1
    # Drain any events under the synthesised run. There's exactly
    # one because the ingest fixture sent one span.
    conn = storage._conn()
    cur = conn.execute(
        "SELECT kind FROM events ORDER BY rowid"
    )
    kinds = [row[0] for row in cur.fetchall()]
    # First event is the synthetic run's start event from start_run
    # (none — start_run only writes to runs table, not events).
    # So the only event row should be the llm_call.
    assert kinds == ["llm_call"]


def test_post_traces_deduplicates_on_repeat(storage, receiver):
    payload = _otlp_request("span-dup", response_id="resp-dup")
    _post(f"http://127.0.0.1:{receiver.port}/v1/traces", payload)
    status, body = _post(
        f"http://127.0.0.1:{receiver.port}/v1/traces", payload
    )
    assert status == 200
    assert body["_inkfoot"]["accepted"] == 0
    assert body["_inkfoot"]["duplicates"] == 1


def test_post_traces_rejects_protobuf_with_415(storage, receiver):
    req = urllib.request.Request(
        f"http://127.0.0.1:{receiver.port}/v1/traces",
        method="POST",
        data=b"\x00\x01\x02",
        headers={"Content-Type": "application/x-protobuf"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 415


def test_post_traces_unknown_path_returns_404(storage, receiver):
    req = urllib.request.Request(
        f"http://127.0.0.1:{receiver.port}/elsewhere",
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404


def test_post_traces_invalid_json_returns_400(storage, receiver):
    req = urllib.request.Request(
        f"http://127.0.0.1:{receiver.port}/v1/traces",
        method="POST",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


def test_two_distinct_traces_land_under_two_runs(storage, receiver):
    _post(
        f"http://127.0.0.1:{receiver.port}/v1/traces",
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "trace-A",
                                    "spanId": "span-A",
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                    "attributes": [
                                        _kv(GEN_AI_SYSTEM, "anthropic")
                                    ],
                                },
                                {
                                    "traceId": "trace-B",
                                    "spanId": "span-B",
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                    "attributes": [
                                        _kv(GEN_AI_SYSTEM, "anthropic")
                                    ],
                                },
                            ]
                        }
                    ]
                }
            ]
        },
    )
    conn = storage._conn()
    cur = conn.execute("SELECT COUNT(DISTINCT run_id) FROM events")
    assert cur.fetchone()[0] == 2
