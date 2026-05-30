"""OpenTelemetry ingest + export.

Inkfoot speaks the OTel GenAI semantic conventions both ways:

* :mod:`inkfoot.otel.conventions` — pinned spec version + the
  attribute-name constants used by the rest of this package. Bumping
  the convention version requires an explicit edit there, never an
  auto-pull (see :data:`OTEL_GENAI_CONVENTIONS_VERSION`).
* :mod:`inkfoot.otel.mapping` — pure bidirectional translator
  between :class:`inkfoot.normalise.NeutralCall` and the OTel
  attribute dict used by Inkfoot OTLP mapping.
* :mod:`inkfoot.otel.ingest` — stdlib HTTP receiver that accepts
  OTLP/JSON `POST /v1/traces` requests, maps each ``gen_ai.*`` span
  into a :class:`NeutralCall`, and persists it to storage. Per
  dedup contract ingest de-duplicates against the native shim using
  ``(span_id, response_id)`` so a user running both auto-OTel and
  our SDK shim doesn't double-count calls.
* :mod:`inkfoot.otel.export` — taps the event stream emitted by
  the shim and forwards each ``llm_call`` event as an OTLP/JSON
  span to any configured OTel collector. Smell + outcome events
  forward as OTel logs (one log record per event).

The OTel SDK is **not** a hard dependency: the package speaks
OTLP/JSON over plain HTTP using the stdlib only. Consumers who
already run an OTel collector point it at our ingest port and
configure our export endpoint, and that's all.
"""

from __future__ import annotations

from inkfoot.otel.conventions import (
    OTEL_GENAI_CONVENTIONS_VERSION,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_ID,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    INKFOOT_CAUSE_PREFIX,
    INKFOOT_ESTIMATED_NANODOLLARS,
    INKFOOT_ESTIMATION_FLAGS,
)
from inkfoot.otel.mapping import (
    AttrMap,
    attrs_to_neutral_call,
    neutral_call_to_attrs,
)

__all__ = [
    "OTEL_GENAI_CONVENTIONS_VERSION",
    "GEN_AI_SYSTEM",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_RESPONSE_ID",
    "INKFOOT_CAUSE_PREFIX",
    "INKFOOT_ESTIMATION_FLAGS",
    "INKFOOT_ESTIMATED_NANODOLLARS",
    "AttrMap",
    "attrs_to_neutral_call",
    "neutral_call_to_attrs",
]
