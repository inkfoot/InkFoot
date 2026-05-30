"""Pinned OTel GenAI semantic-conventions version + attribute names.

The OTel GenAI conventions are still evolving as of mid-2026
We pin against
a specific spec version here so an upstream rename can't silently
shift our wire format. Bumping the version requires:

1. Update :data:`OTEL_GENAI_CONVENTIONS_VERSION`.
2. Add / rename any constants in this module.
3. Update :mod:`inkfoot.otel.mapping` and the round-trip test.

The "bump test" in ``tests/unit/test_otel_conventions.py`` pins
the version literal so a careless dependency update can't change
it without a test flagging the diff.

We use the spec strings rather than importing them from
``opentelemetry-semantic-conventions``. That keeps the OTel SDK
optional (it's a heavy install) while still giving us a single
source of truth.
"""

from __future__ import annotations


# The version we're pinned against. Independent of the inkfoot
# SemVer — bump deliberately when the upstream spec stabilises
# something we want to track.
OTEL_GENAI_CONVENTIONS_VERSION = "1.27.0"


# Core GenAI attributes (mirrors the OTLP mapping table).
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_RESPONSE_ID = "gen_ai.response.id"


# Inkfoot extension namespace. The OTel spec explicitly allows
# vendor extensions under a vendor-owned prefix; we use
# ``inkfoot.`` so a backend that hasn't heard of us can still
# render the span (the extra attrs just appear as opaque keys).
INKFOOT_CAUSE_PREFIX = "inkfoot.cause."
INKFOOT_ESTIMATION_FLAGS = "inkfoot.estimation_flags"
INKFOOT_ESTIMATED_NANODOLLARS = "inkfoot.estimated_nanodollars"

# Per-call provenance attrs (helpful when ingest dedup needs to
# trace a span back to its source pipeline). The current release only writes
# these on export; future Cloud reads them on ingest.
INKFOOT_RUN_ID = "inkfoot.run_id"
INKFOOT_EVENT_KIND = "inkfoot.event_kind"
INKFOOT_SEQUENCE = "inkfoot.sequence"


# The 11 structural causes + 2 cache overlays exposed as
# ``inkfoot.cause.<field>``. Order matches the current ledger
# declaration order so a renderer iterating this list emits in
# the same order the report CLI does.
INKFOOT_CAUSE_FIELDS: tuple[str, ...] = (
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
)


def cause_attr(field_name: str) -> str:
    """Return the OTel attribute name for ledger field ``field_name``.

    Raises ``KeyError`` for unknown fields so a typo in a callsite
    can't silently produce ``inkfoot.cause.<garbage>`` keys.
    """
    if field_name not in INKFOOT_CAUSE_FIELDS:
        raise KeyError(
            f"cause_attr: unknown ledger field {field_name!r}. "
            f"Known fields: {INKFOOT_CAUSE_FIELDS}"
        )
    return f"{INKFOOT_CAUSE_PREFIX}{field_name}"
