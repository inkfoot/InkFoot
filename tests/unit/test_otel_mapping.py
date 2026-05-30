"""Bidirectional mapping tests (Phase 1 / E3-S1 T3)."""

from __future__ import annotations

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.otel import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_ESTIMATED_NANODOLLARS,
    INKFOOT_ESTIMATION_FLAGS,
    attrs_to_neutral_call,
    neutral_call_to_attrs,
)
from inkfoot.otel.conventions import INKFOOT_CAUSE_FIELDS, cause_attr


def _populated_ledger() -> CausalTokenLedger:
    # Distinct value per cause so a field swap or off-by-one in
    # mapping shows up immediately rather than getting masked by
    # identical numbers.
    return CausalTokenLedger(
        system_static_tokens=100,
        system_dynamic_tokens=20,
        user_input_tokens=30,
        tool_schema_tokens=40,
        tool_result_tokens=50,
        retrieved_context_tokens=60,
        memory_tokens=70,
        retry_overhead_tokens=80,
        summariser_tokens=90,
        reasoning_tokens=110,
        guardrail_tokens=120,
        cache_creation_tokens=130,
        cache_read_tokens=140,
        output_tokens=250,
    )


def _populated_call() -> NeutralCall:
    return NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        started_at=1_700_000_000_000,
        ended_at=1_700_000_001_000,
        ledger=_populated_ledger(),
        estimated_nanodollars=42_500_000,
        estimation_flags=("tokeniser_fallback", "cache_inferred"),
        sequence=7,
    )


def test_outbound_carries_provider_and_model():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call, response_id="resp-abc")
    assert attrs[GEN_AI_SYSTEM] == "anthropic"
    assert attrs[GEN_AI_REQUEST_MODEL] == "claude-haiku-4-5-20251001"
    assert attrs[GEN_AI_OPERATION_NAME] == "chat"
    assert attrs[GEN_AI_RESPONSE_ID] == "resp-abc"


def test_outbound_input_tokens_is_sum_of_all_thirteen_causes():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call)
    expected = sum(
        getattr(call.ledger, f) for f in INKFOOT_CAUSE_FIELDS
    )
    assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == expected


def test_outbound_output_tokens_matches_ledger():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call)
    assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 250


def test_outbound_each_cause_field_lands_under_inkfoot_cause_namespace():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call)
    for field_name in INKFOOT_CAUSE_FIELDS:
        assert attrs[cause_attr(field_name)] == getattr(
            call.ledger, field_name
        )


def test_outbound_csv_encodes_estimation_flags():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call)
    assert attrs[INKFOOT_ESTIMATION_FLAGS] == "tokeniser_fallback,cache_inferred"


def test_outbound_skips_estimation_flags_when_empty():
    call = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=0,
        ended_at=1,
        ledger=CausalTokenLedger(),
    )
    attrs = neutral_call_to_attrs(call)
    assert INKFOOT_ESTIMATION_FLAGS not in attrs


def test_outbound_skips_estimated_nanodollars_when_none():
    call = NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=0,
        ended_at=1,
        ledger=CausalTokenLedger(),
        estimated_nanodollars=None,
    )
    attrs = neutral_call_to_attrs(call)
    assert INKFOOT_ESTIMATED_NANODOLLARS not in attrs


def test_outbound_omits_response_id_when_unspecified():
    call = _populated_call()
    attrs = neutral_call_to_attrs(call)
    assert GEN_AI_RESPONSE_ID not in attrs


def test_round_trip_through_attrs_preserves_ledger_and_metadata():
    original = _populated_call()
    attrs = neutral_call_to_attrs(original)
    restored = attrs_to_neutral_call(
        attrs,
        started_at=original.started_at,
        ended_at=original.ended_at,
        sequence=original.sequence,
    )
    assert restored.provider == original.provider
    assert restored.model == original.model
    assert restored.estimated_nanodollars == original.estimated_nanodollars
    assert restored.estimation_flags == original.estimation_flags
    # Field-by-field ledger assertion.
    for field_name in INKFOOT_CAUSE_FIELDS:
        assert getattr(restored.ledger, field_name) == getattr(
            original.ledger, field_name
        )
    assert restored.ledger.output_tokens == original.ledger.output_tokens


def test_inbound_defaults_unknown_provider_and_model_to_unknown():
    restored = attrs_to_neutral_call({}, started_at=0, ended_at=1)
    assert restored.provider == "unknown"
    assert restored.model == "unknown"
    # All ledger fields default to zero on a missing-attrs span.
    for field_name in INKFOOT_CAUSE_FIELDS:
        assert getattr(restored.ledger, field_name) == 0
    assert restored.ledger.output_tokens == 0


def test_inbound_tolerates_non_numeric_cause_values():
    # A collector that ships strings rather than numbers shouldn't
    # crash the ingest path — fall through to zero on that field.
    attrs = {
        GEN_AI_SYSTEM: "openai",
        GEN_AI_REQUEST_MODEL: "gpt-4",
        cause_attr("user_input_tokens"): "not-a-number",
        cause_attr("system_static_tokens"): 50,
    }
    restored = attrs_to_neutral_call(attrs, started_at=0, ended_at=1)
    assert restored.ledger.user_input_tokens == 0
    assert restored.ledger.system_static_tokens == 50


def test_inbound_accepts_estimation_flags_as_list():
    # Some pipelines explode CSV-encoded lists into native arrays.
    # The ingest path should accept both shapes.
    attrs = {INKFOOT_ESTIMATION_FLAGS: ["a", "b", "c"]}
    restored = attrs_to_neutral_call(attrs, started_at=0, ended_at=1)
    assert restored.estimation_flags == ("a", "b", "c")


def test_inbound_estimation_flags_empty_string_is_empty_tuple():
    attrs = {INKFOOT_ESTIMATION_FLAGS: ""}
    restored = attrs_to_neutral_call(attrs, started_at=0, ended_at=1)
    assert restored.estimation_flags == ()
