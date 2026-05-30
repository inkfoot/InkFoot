"""Unit tests for the OTLP exporter."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Any

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.otel.conventions import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_EVENT_KIND,
    INKFOOT_RUN_ID,
    cause_attr,
)
from inkfoot.otel.export import (
    OTLPExporter,
    _encode_attrs,
    _restore_neutral_call,
    tap_storage,
)


class _FakeTransport:
    def __init__(self, *, fail_first_n: int = 0) -> None:
        self.traces: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._fail_first_n = fail_first_n
        self.call_count = 0

    def post_traces(self, body: dict[str, Any]) -> None:
        with self._lock:
            self.call_count += 1
            if self.call_count <= self._fail_first_n:
                raise RuntimeError("simulated transport failure")
            self.traces.append(body)

    def post_logs(self, body: dict[str, Any]) -> None:
        with self._lock:
            self.call_count += 1
            self.logs.append(body)


def _llm_call_event(
    *,
    event_id: str = "evt-1",
    run_id: str = "run-1",
    sequence: int = 1,
    nanodollars: int = 1_000_000,
) -> dict[str, Any]:
    call = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=1_700_000_000_000,
        ended_at=1_700_000_001_000,
        ledger=CausalTokenLedger(
            user_input_tokens=50,
            system_static_tokens=100,
            output_tokens=25,
        ),
        estimated_nanodollars=nanodollars,
        sequence=sequence,
    )
    return {
        "event_id": event_id,
        "run_id": run_id,
        "kind": "llm_call",
        "occurred_at": call.ended_at,
        "sequence": sequence,
        "payload_json": json.dumps(asdict(call)),
        "capture_mode": "metadata",
    }


def _exporter_with(
    transport: _FakeTransport, **kwargs: Any
) -> OTLPExporter:
    return OTLPExporter(
        transport=transport,
        batch_size=kwargs.get("batch_size", 4),
        batch_interval_s=kwargs.get("batch_interval_s", 0.05),
        queue_capacity=kwargs.get("queue_capacity", 32),
    )


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------------------
# Span building
# ----------------------------------------------------------------------


def test_llm_call_event_becomes_one_span_carrying_full_ledger():
    transport = _FakeTransport()
    exporter = _exporter_with(transport)
    exporter.start()
    try:
        exporter.enqueue_event(_llm_call_event())
        assert _wait_until(lambda: len(transport.traces) >= 1)
    finally:
        exporter.stop()

    body = transport.traces[0]
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1
    attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
    # gen_ai.system + per-cause attrs both present
    assert attrs[GEN_AI_SYSTEM]["stringValue"] == "anthropic"
    assert attrs[GEN_AI_REQUEST_MODEL]["stringValue"] == "claude-haiku-4-5"
    assert int(attrs[cause_attr("user_input_tokens")]["intValue"]) == 50
    assert int(attrs[GEN_AI_USAGE_OUTPUT_TOKENS]["intValue"]) == 25
    assert attrs[INKFOOT_EVENT_KIND]["stringValue"] == "llm_call"


def test_smell_event_becomes_one_log_record():
    transport = _FakeTransport()
    exporter = _exporter_with(transport)
    exporter.start()
    try:
        exporter.enqueue_event(
            {
                "event_id": "evt-smell",
                "run_id": "run-1",
                "kind": "smell",
                "occurred_at": 1_700_000_002_000,
                "sequence": 5,
                "payload_json": json.dumps({"smell_id": "unstable-prompt-prefix"}),
                "capture_mode": "metadata",
            }
        )
        assert _wait_until(lambda: len(transport.logs) >= 1)
    finally:
        exporter.stop()

    body = transport.logs[0]
    records = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(records) == 1
    record = records[0]
    assert record["severityText"] == "WARN"
    assert "unstable-prompt-prefix" in record["body"]["stringValue"]


def test_exported_counter_equals_event_count_not_batch_count():
    # Review #2: the stats counter previously incremented by 1
    # per batch regardless of the span count. With a batch of N
    # events the counter must read N afterwards.
    transport = _FakeTransport()
    exporter = _exporter_with(transport, batch_size=4)
    exporter.start()
    try:
        for i in range(4):
            exporter.enqueue_event(_llm_call_event(event_id=f"evt-{i}"))
        assert _wait_until(lambda: exporter.stats["exported"] == 4)
    finally:
        exporter.stop()
    # Exactly one HTTP batch was sent (batch_size=4) carrying
    # four spans; the counter must reflect events, not batches.
    assert len(transport.traces) == 1
    assert exporter.stats["exported"] == 4


def test_exported_counter_counts_log_records_per_event():
    transport = _FakeTransport()
    exporter = _exporter_with(transport, batch_size=3)
    exporter.start()
    try:
        for i in range(3):
            exporter.enqueue_event(
                {
                    "event_id": f"evt-smell-{i}",
                    "run_id": "run-1",
                    "kind": "smell",
                    "occurred_at": 1_700_000_000_000,
                    "sequence": i,
                    "payload_json": json.dumps(
                        {"smell_id": "unstable-prompt-prefix"}
                    ),
                }
            )
        assert _wait_until(lambda: exporter.stats["exported"] == 3)
    finally:
        exporter.stop()
    assert len(transport.logs) == 1
    assert exporter.stats["exported"] == 3


def test_log_body_preserves_original_payload_text():
    # Review #9: backends indexing log bodies as strings should
    # see the same text that storage wrote, not a json.loads /
    # json.dumps re-encoding.
    transport = _FakeTransport()
    exporter = _exporter_with(transport, batch_size=1)
    original_body = '{"smell_id":"unstable-prompt-prefix","extra":1}'
    exporter.start()
    try:
        exporter.enqueue_event(
            {
                "event_id": "evt-smell-orig",
                "run_id": "run-1",
                "kind": "smell",
                "occurred_at": 1_700_000_000_000,
                "sequence": 1,
                "payload_json": original_body,
            }
        )
        assert _wait_until(lambda: transport.logs)
    finally:
        exporter.stop()
    record = transport.logs[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["body"]["stringValue"] == original_body


def test_outcome_event_becomes_info_log():
    transport = _FakeTransport()
    exporter = _exporter_with(transport)
    exporter.start()
    try:
        exporter.enqueue_event(
            {
                "event_id": "evt-out",
                "run_id": "run-1",
                "kind": "outcome",
                "occurred_at": 1_700_000_003_000,
                "sequence": 6,
                "payload_json": json.dumps(
                    {"outcome": "success", "quality_score": 0.95}
                ),
                "capture_mode": "metadata",
            }
        )
        assert _wait_until(lambda: len(transport.logs) >= 1)
    finally:
        exporter.stop()
    record = transport.logs[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["severityText"] == "INFO"


def test_unrelated_event_kinds_are_silently_skipped():
    transport = _FakeTransport()
    exporter = _exporter_with(transport, batch_size=1)
    exporter.start()
    try:
        exporter.enqueue_event(
            {
                "event_id": "evt-checkpoint",
                "run_id": "run-1",
                "kind": "checkpoint",
                "occurred_at": 1,
                "sequence": 1,
                "payload_json": json.dumps({"label": "x"}),
            }
        )
        # Give the worker a chance to flush — nothing should arrive
        # because checkpoint isn't on the export list.
        time.sleep(0.2)
    finally:
        exporter.stop()
    assert transport.traces == []
    assert transport.logs == []


# ----------------------------------------------------------------------
# Failure semantics
# ----------------------------------------------------------------------


def test_exporter_continues_after_transport_failure(caplog):
    transport = _FakeTransport(fail_first_n=1)
    exporter = _exporter_with(transport, batch_size=1)
    with caplog.at_level(logging.WARNING, logger="inkfoot.otel.export"):
        exporter.start()
        try:
            exporter.enqueue_event(_llm_call_event(event_id="evt-fail"))
            # Allow first flush attempt to fail and recovery flush
            # to succeed.
            exporter.enqueue_event(_llm_call_event(event_id="evt-ok"))
            assert _wait_until(lambda: len(transport.traces) >= 1)
        finally:
            exporter.stop()
    assert exporter.stats["failures"] == 1
    assert any(
        "export failed" in r.message or "export raised" in r.message
        for r in caplog.records
    )


def test_queue_overflow_drops_event_and_logs_warning(caplog):
    transport = _FakeTransport()
    # Queue capacity 1 so the second enqueue overflows. Worker is
    # not started, so nothing drains the queue between enqueues.
    exporter = OTLPExporter(
        transport=transport,
        queue_capacity=1,
        batch_interval_s=0.05,
    )
    with caplog.at_level(logging.WARNING, logger="inkfoot.otel.export"):
        exporter.enqueue_event(_llm_call_event(event_id="evt-1"))
        exporter.enqueue_event(_llm_call_event(event_id="evt-2"))
    assert exporter.stats["dropped"] == 1
    assert any("queue full" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# Storage tap
# ----------------------------------------------------------------------


class _FakeStorage:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def insert_event(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def proxy_only_method(self) -> str:
        # Not overridden by _ExportingStorage; we assert
        # __getattr__ fallthrough preserves this.
        return "proxied"


def test_tap_storage_mirrors_inserts_to_exporter():
    storage = _FakeStorage()
    transport = _FakeTransport()
    exporter = _exporter_with(transport, batch_size=1)
    wrapped = tap_storage(storage, exporter)
    wrapped.insert_event(**_llm_call_event(event_id="evt-mirror"))
    assert len(storage.calls) == 1
    assert exporter.stats["queued"] == 1


def test_tap_storage_proxies_unrelated_methods():
    storage = _FakeStorage()
    exporter = _exporter_with(_FakeTransport())
    wrapped = tap_storage(storage, exporter)
    assert wrapped.proxy_only_method() == "proxied"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def test_restore_neutral_call_round_trips():
    event = _llm_call_event()
    call = _restore_neutral_call(event)
    assert call.provider == "anthropic"
    assert call.ledger.user_input_tokens == 50


def test_encode_attrs_handles_various_value_kinds():
    encoded = _encode_attrs(
        {
            "s": "hi",
            "i": 42,
            "f": 1.5,
            "b": True,
            "l": [1, 2, 3],
        }
    )
    by_key = {a["key"]: a["value"] for a in encoded}
    assert by_key["s"] == {"stringValue": "hi"}
    assert by_key["i"] == {"intValue": "42"}
    assert by_key["f"] == {"doubleValue": 1.5}
    assert by_key["b"] == {"boolValue": True}
    # Lists fall through to a JSON-stringified attr so backends
    # without array support still see the value.
    assert "stringValue" in by_key["l"]
