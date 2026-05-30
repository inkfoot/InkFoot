"""Convention pin guard.

The OTel GenAI conventions are still evolving, so Inkfoot pins
against a known spec version in :mod:`inkfoot.otel.conventions`;
this test asserts the pinned literal so a careless edit doesn't
silently switch us to a different convention version.
"""

from __future__ import annotations

from inkfoot.otel import conventions


def test_pinned_genai_convention_version_is_stable():
    # Bump this assertion *only* when intentionally moving to a
    # new upstream version. See the module docstring of
    # conventions.py for the procedure.
    assert conventions.OTEL_GENAI_CONVENTIONS_VERSION == "1.27.0"


def test_genai_attribute_names_match_spec_strings():
    assert conventions.GEN_AI_SYSTEM == "gen_ai.system"
    assert conventions.GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
    assert conventions.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert conventions.GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
    assert conventions.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"
    assert conventions.GEN_AI_RESPONSE_ID == "gen_ai.response.id"


def test_inkfoot_extension_prefix_lives_under_inkfoot_namespace():
    assert conventions.INKFOOT_CAUSE_PREFIX == "inkfoot.cause."
    assert conventions.INKFOOT_ESTIMATION_FLAGS == "inkfoot.estimation_flags"
    assert conventions.INKFOOT_ESTIMATED_NANODOLLARS == "inkfoot.estimated_nanodollars"


def test_cause_fields_match_ledger_categories():
    # Mapping table covers the 13 input-side ledger fields exactly
    # (11 structural causes + 2 cache overlays). Output is handled
    # via the spec's gen_ai.usage.output_tokens attribute, not the
    # cause namespace.
    expected = {
        "system_static_tokens",
        "system_dynamic_tokens",
        "user_input_tokens",
        "tool_schema_tokens",
        "tool_result_tokens",
        "retrieved_context_tokens",
        "memory_tokens",
        "retry_overhead_tokens",
        "summariser_tokens",
        "reasoning_tokens",
        "guardrail_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    }
    assert set(conventions.INKFOOT_CAUSE_FIELDS) == expected


def test_cause_attr_rejects_unknown_field():
    import pytest

    with pytest.raises(KeyError, match="unknown ledger field"):
        conventions.cause_attr("hallucinated_tokens")


def test_cause_attr_produces_namespaced_key():
    assert (
        conventions.cause_attr("system_static_tokens")
        == "inkfoot.cause.system_static_tokens"
    )
