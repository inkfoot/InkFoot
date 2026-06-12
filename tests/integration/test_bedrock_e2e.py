"""Bedrock provider integration tests.

There is no Bedrock shim — boto3 builds its clients dynamically
from service definitions, so there's no stable module-level method
to patch the way the first-party SDK shims do. Instrumentation is
at the provider level: callers run ``client.converse(...)``
themselves and hand the response to :class:`BedrockProvider`.

The offline tests exercise that supported path end to end — a
realistic Converse response dict through ``map_usage`` into a
ledger and a nanodollar estimate. The live smoke test is opt-in:
marked ``live_bedrock`` and skipped unless AWS credentials are
present and boto3 is installed.
"""

from __future__ import annotations

import os

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import estimate_nanodollars
from inkfoot.providers.bedrock import BedrockProvider

_SONNET = "anthropic.claude-3-5-sonnet-20241022-v2:0"
_LLAMA = "meta.llama3-2-3b-instruct-v1:0"


def _converse_response(usage: dict) -> dict:
    """A realistic ``bedrock-runtime`` Converse response body."""
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Deploy looks healthy."}],
            }
        },
        "stopReason": "end_turn",
        "metrics": {"latencyMs": 412},
        "usage": usage,
    }


def _ledger_from_usage(provider: BedrockProvider, response: dict):
    usage = provider.map_usage(response)
    ledger = CausalTokenLedger(
        user_input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
    )
    return usage, ledger


# ----------------------------------------------------------------------
# Offline: map_usage × estimate_nanodollars round trip
# ----------------------------------------------------------------------


def test_cache_hit_response_round_trips_to_a_nanodollar_estimate() -> None:
    provider = BedrockProvider(_SONNET)
    response = _converse_response(
        {
            "inputTokens": 1_200,
            "outputTokens": 250,
            "cacheReadInputTokens": 8_000,
            "cacheWriteInputTokens": 0,
        }
    )
    usage, ledger = _ledger_from_usage(provider, response)

    assert usage.input_tokens == 9_200
    assert usage.cache_status == "hit"

    estimate = estimate_nanodollars("bedrock", _SONNET, ledger)
    # 1 200 fresh × 3 000 + 8 000 reads × 300 + 250 out × 15 000.
    assert estimate == 3_600_000 + 2_400_000 + 3_750_000


def test_cache_write_response_bills_at_the_write_rate() -> None:
    provider = BedrockProvider(_SONNET)
    response = _converse_response(
        {
            "inputTokens": 1_200,
            "outputTokens": 250,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 8_000,
        }
    )
    usage, ledger = _ledger_from_usage(provider, response)

    assert usage.cache_status == "miss"

    estimate = estimate_nanodollars("bedrock", _SONNET, ledger)
    # 1 200 fresh × 3 000 + 8 000 writes × 3 750 + 250 out × 15 000.
    assert estimate == 3_600_000 + 30_000_000 + 3_750_000


def test_unpriced_family_reports_tokens_but_no_estimate() -> None:
    provider = BedrockProvider(_LLAMA)
    response = _converse_response(
        {"inputTokens": 900, "outputTokens": 120}
    )
    usage, ledger = _ledger_from_usage(provider, response)

    assert usage.input_tokens == 900
    assert usage.output_tokens == 120
    # Non-Anthropic Bedrock pricing varies by region and purchasing
    # model, so the table has no row — tokens-only reporting.
    assert estimate_nanodollars("bedrock", _LLAMA, ledger) is None


# ----------------------------------------------------------------------
# Live smoke (opt-in)
# ----------------------------------------------------------------------

_HAS_AWS_CREDS = bool(
    os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
)


@pytest.mark.live_bedrock
@pytest.mark.skipif(
    not _HAS_AWS_CREDS,
    reason="AWS_ACCESS_KEY_ID / AWS_PROFILE not set",
)
def test_live_converse_call_maps_usage() -> None:
    boto3 = pytest.importorskip("boto3")

    model = (
        os.environ.get("INKFOOT_LIVE_BEDROCK_MODEL")
        or BedrockProvider.DEFAULT_MODEL
    )
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    client = boto3.client("bedrock-runtime", region_name=region)
    response = client.converse(
        modelId=model,
        messages=[
            {
                "role": "user",
                "content": [{"text": "Reply with the single word OK."}],
            }
        ],
        inferenceConfig={"maxTokens": 16},
    )

    usage = BedrockProvider(model).map_usage(response)
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.cache_status in {"hit", "partial", "miss", "n/a"}
