"""Unit tests for the OTel ingest translator + dedup core
(Phase 1 / E3-S2). HTTP integration tests live in
``tests/integration/test_otel_ingest.py``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from inkfoot.otel.conventions import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_CAUSE_FIELDS,
    INKFOOT_ESTIMATION_FLAGS,
    INKFOOT_RUN_ID,
    INKFOOT_SEQUENCE,
    cause_attr,
)
from inkfoot.otel.ingest import (
    OTLPHTTPReceiver,
    _DedupCache,
    _decode_attributes,
    _iter_spans,
    _otel_nanos_to_ms,
)


def _kv(key: str, value: Any) -> dict[str, Any]:
    """Build one OTLP/JSON KeyValue entry."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    if isinstance(value, (list, tuple)):
        return {
            "key": key,
            "value": {"arrayValue": {"values": [_kv("", v)["value"] for v in value]}},
        }
    raise TypeError(f"unsupported attr value type: {type(value).__name__}")


def _span(
    span_id: str,
    *,
    trace_id: str = "trace-1",
    start_ns: int = 1_700_000_000_000_000_000,
    end_ns: int = 1_700_000_001_000_000_000,
    attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [_kv(k, v) for k, v in (attrs or {}).items()],
    }


def _otlp_payload(*spans: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {"scope": {"name": "test"}, "spans": list(spans)}
                ],
            }
        ]
    }


# ----------------------------------------------------------------------
# Attribute decoding
# ----------------------------------------------------------------------


def test_decode_attributes_handles_each_otel_value_kind():
    attrs_list = [
        _kv("s", "hello"),
        _kv("i", 42),
        _kv("d", 3.14),
        _kv("b", True),
        _kv("arr", ["a", "b"]),
    ]
    decoded = _decode_attributes(attrs_list)
    assert decoded["s"] == "hello"
    assert decoded["i"] == 42
    assert decoded["d"] == 3.14
    assert decoded["b"] is True
    assert decoded["arr"] == ["a", "b"]


def test_otel_nanos_to_ms_handles_string_input():
    assert _otel_nanos_to_ms("1700000001000000000") == 1_700_000_001_000


def test_otel_nanos_to_ms_handles_missing_input():
    assert _otel_nanos_to_ms(None) == 0
    assert _otel_nanos_to_ms("garbage") == 0


# ----------------------------------------------------------------------
# Dedup cache (ADR-1-2)
# ----------------------------------------------------------------------


def test_dedup_cache_returns_true_on_first_see():
    cache = _DedupCache()
    assert cache.see_or_add("span-1", "resp-1") is True


def test_dedup_cache_returns_false_on_duplicate():
    cache = _DedupCache()
    cache.see_or_add("span-1", "resp-1")
    assert cache.see_or_add("span-1", "resp-1") is False


def test_dedup_cache_treats_distinct_response_ids_as_separate():
    cache = _DedupCache()
    cache.see_or_add("span-1", "resp-1")
    assert cache.see_or_add("span-1", "resp-2") is True


def test_dedup_cache_skips_dedup_for_empty_keys():
    # A span with neither a span_id nor a response_id can't be
    # safely deduplicated — treat as fresh every time rather than
    # collapsing distinct "empty" calls onto one row.
    cache = _DedupCache()
    assert cache.see_or_add("", "") is True
    assert cache.see_or_add("", "") is True


def test_dedup_cache_evicts_oldest_when_capacity_exceeded():
    cache = _DedupCache(max_entries=3)
    cache.see_or_add("a", "1")
    cache.see_or_add("b", "2")
    cache.see_or_add("c", "3")
    cache.see_or_add("d", "4")  # evicts ("a", "1")
    assert cache.see_or_add("a", "1") is True  # treated as fresh
    assert len(cache) == 3


def test_dedup_cache_lru_touch_on_repeat_keeps_entry_warm():
    cache = _DedupCache(max_entries=3)
    cache.see_or_add("a", "1")
    cache.see_or_add("b", "2")
    cache.see_or_add("c", "3")
    # Repeat 'a' -> it's the most-recent again.
    cache.see_or_add("a", "1")
    cache.see_or_add("d", "4")  # evicts 'b' (now oldest), not 'a'
    assert cache.see_or_add("b", "2") is True
    assert cache.see_or_add("a", "1") is False


def test_dedup_cache_is_thread_safe():
    # 100 threads each insert the same key; only one should win.
    cache = _DedupCache()
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        v = cache.see_or_add("span-X", "resp-X")
        with lock:
            results.append(v)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1


# ----------------------------------------------------------------------
# OTLP iter_spans + receiver translation
# ----------------------------------------------------------------------


def test_iter_spans_yields_all_spans_across_resource_and_scope_levels():
    payload = {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"spanId": "a"}, {"spanId": "b"}]}]},
            {"scopeSpans": [{"spans": [{"spanId": "c"}]}]},
        ]
    }
    span_ids = [s.get("spanId") for s in _iter_spans(payload)]
    assert span_ids == ["a", "b", "c"]


def test_iter_spans_tolerates_legacy_instrumentation_library_key():
    # OTLP older schema used `instrumentationLibrarySpans`; we
    # accept both shapes so a vintage collector can still ship to us.
    payload = {
        "resourceSpans": [
            {"instrumentationLibrarySpans": [{"spans": [{"spanId": "x"}]}]}
        ]
    }
    assert [s["spanId"] for s in _iter_spans(payload)] == ["x"]


def test_iter_spans_skips_malformed_levels_defensively():
    payload = {
        "resourceSpans": [
            None,
            {"scopeSpans": [None, {"spans": [None, {"spanId": "ok"}]}]},
        ]
    }
    assert [s["spanId"] for s in _iter_spans(payload)] == ["ok"]


def test_receiver_translates_span_into_neutral_call_envelope():
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    payload = _otlp_payload(
        _span(
            "span-1",
            attrs={
                GEN_AI_SYSTEM: "anthropic",
                GEN_AI_REQUEST_MODEL: "claude-haiku-4-5",
                GEN_AI_RESPONSE_ID: "resp-xyz",
                cause_attr("user_input_tokens"): 50,
                cause_attr("system_static_tokens"): 100,
                GEN_AI_USAGE_OUTPUT_TOKENS: 25,
                INKFOOT_ESTIMATION_FLAGS: "tokeniser_fallback",
                INKFOOT_RUN_ID: "run-explicit",
                INKFOOT_SEQUENCE: 12,
            },
        )
    )
    stats = recv.ingest_payload(payload)
    assert stats == {
        "accepted": 1,
        "duplicates": 0,
        "rejected": 0,
        "skipped_non_genai": 0,
    }
    assert len(persisted) == 1
    envelope = persisted[0]
    assert envelope["span_id"] == "span-1"
    assert envelope["response_id"] == "resp-xyz"
    assert envelope["run_id"] == "run-explicit"
    assert envelope["sequence"] == 12
    call = envelope["neutral_call"]
    assert call.provider == "anthropic"
    assert call.model == "claude-haiku-4-5"
    assert call.ledger.user_input_tokens == 50
    assert call.ledger.system_static_tokens == 100
    assert call.ledger.output_tokens == 25
    assert call.estimation_flags == ("tokeniser_fallback",)


def test_receiver_dedupes_duplicate_span_id_and_response_id():
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    span = _span(
        "span-dup",
        attrs={
            GEN_AI_SYSTEM: "openai",
            GEN_AI_REQUEST_MODEL: "gpt-4",
            GEN_AI_RESPONSE_ID: "resp-1",
        },
    )
    recv.ingest_payload(_otlp_payload(span))
    stats = recv.ingest_payload(_otlp_payload(span))
    assert stats == {
        "accepted": 0,
        "duplicates": 1,
        "rejected": 0,
        "skipped_non_genai": 0,
    }
    assert len(persisted) == 1


def test_receiver_accepts_unique_response_id_after_seen_span_id():
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    recv.ingest_payload(
        _otlp_payload(
            _span(
                "span-shared",
                attrs={GEN_AI_RESPONSE_ID: "resp-A"},
            )
        )
    )
    recv.ingest_payload(
        _otlp_payload(
            _span(
                "span-shared",
                attrs={GEN_AI_RESPONSE_ID: "resp-B"},
            )
        )
    )
    assert len(persisted) == 2


def test_receiver_records_rejected_count_when_persist_raises():
    def boom(envelope: dict[str, Any]) -> None:
        raise RuntimeError("storage offline")

    recv = OTLPHTTPReceiver(persist=boom, port=0)
    stats = recv.ingest_payload(
        _otlp_payload(_span("span-1", attrs={GEN_AI_SYSTEM: "anthropic"}))
    )
    assert stats == {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 1,
        "skipped_non_genai": 0,
    }


def test_receiver_empty_payload_returns_zero_accepted():
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    stats = recv.ingest_payload({})
    assert stats == {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 0,
        "skipped_non_genai": 0,
    }
    assert persisted == []


# ----------------------------------------------------------------------
# Round-trip with the mapping module so a renamed field anywhere
# in the chain trips this test, not the mapping suite.
# ----------------------------------------------------------------------


def test_receiver_inherits_resource_level_genai_attributes():
    # Review #8: a collector that pins gen_ai.system on the
    # resource and lets spans inherit must still produce a fully
    # typed NeutralCall.
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _kv(GEN_AI_SYSTEM, "openai"),
                        _kv(GEN_AI_REQUEST_MODEL, "gpt-4"),
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "s",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                                "attributes": [
                                    _kv(cause_attr("user_input_tokens"), 20)
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    recv.ingest_payload(payload)
    assert len(persisted) == 1
    call = persisted[0]["neutral_call"]
    assert call.provider == "openai"
    assert call.model == "gpt-4"
    assert call.ledger.user_input_tokens == 20


def test_receiver_span_attribute_wins_over_resource_attribute():
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_kv(GEN_AI_SYSTEM, "openai")]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "s",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                                "attributes": [_kv(GEN_AI_SYSTEM, "anthropic")],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    recv.ingest_payload(payload)
    assert persisted[0]["neutral_call"].provider == "anthropic"


def test_receiver_skips_spans_without_genai_attribute():
    # Review #4: non-GenAI spans (HTTP / DB / queue) skipped, and
    # the skipped count surfaces in the stats delta.
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    payload = _otlp_payload(
        _span("span-http", attrs={"http.method": "GET"})
    )
    stats = recv.ingest_payload(payload)
    assert stats == {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 0,
        "skipped_non_genai": 1,
    }
    assert persisted == []


def test_receiver_kvlist_attribute_decoded_as_dict():
    # Review #7: ``kvlistValue`` round-trips into a Python dict so
    # downstream code can introspect future GenAI extensions
    # without a new receiver release.
    from inkfoot.otel.ingest import _attr_value

    decoded = _attr_value(
        {
            "kvlistValue": {
                "values": [
                    {"key": "model.family", "value": {"stringValue": "claude"}}
                ]
            }
        }
    )
    assert decoded == {"model.family": "claude"}


def test_storage_persist_factory_evicts_oldest_trace_when_capacity_exceeded():
    # Review #3: long-lived ingest can't grow trace_to_run
    # without bound. Evicted entries get a fresh run id on the
    # second visit — recorded by inserting a new run row.
    from inkfoot.otel.ingest import storage_persist_factory

    inserted: list[dict[str, Any]] = []

    class _Fake:
        def start_run(self, **kwargs):
            inserted.append(kwargs)

        def insert_event(self, **kwargs):
            pass

    persist = storage_persist_factory(storage=_Fake(), trace_map_max=2)
    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.normalise import NeutralCall

    call = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=1,
        ended_at=2,
        ledger=CausalTokenLedger(),
    )
    persist({"neutral_call": call, "trace_id": "A", "run_id": None})
    persist({"neutral_call": call, "trace_id": "B", "run_id": None})
    persist({"neutral_call": call, "trace_id": "C", "run_id": None})  # evicts A
    persist({"neutral_call": call, "trace_id": "A", "run_id": None})  # fresh run
    # 4 distinct run rows because A was evicted before its revival.
    run_ids = [row["run_id"] for row in inserted]
    assert len(set(run_ids)) == 4


def test_full_round_trip_through_otlp_ingest_path():
    from inkfoot.normalise import NeutralCall
    from inkfoot.ledger import CausalTokenLedger
    from inkfoot.otel.mapping import neutral_call_to_attrs

    original = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=1_000,
        ended_at=2_000,
        ledger=CausalTokenLedger(
            **{name: idx + 1 for idx, name in enumerate(INKFOOT_CAUSE_FIELDS)},
            output_tokens=999,
        ),
        estimated_nanodollars=12_345,
    )
    attrs = neutral_call_to_attrs(original, response_id="rr-1")

    span = {
        "traceId": "abc",
        "spanId": "span-roundtrip",
        "startTimeUnixNano": str(original.started_at * 1_000_000),
        "endTimeUnixNano": str(original.ended_at * 1_000_000),
        "attributes": [_kv(k, v) for k, v in attrs.items()],
    }
    persisted: list[dict[str, Any]] = []
    recv = OTLPHTTPReceiver(persist=persisted.append, port=0)
    recv.ingest_payload(_otlp_payload(span))
    assert len(persisted) == 1
    restored = persisted[0]["neutral_call"]
    for name in INKFOOT_CAUSE_FIELDS:
        assert getattr(restored.ledger, name) == getattr(original.ledger, name)
    assert restored.ledger.output_tokens == 999
    assert restored.estimated_nanodollars == 12_345
