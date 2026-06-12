"""OpenAI-compat provider integration tests.

Compat endpoints are reached through the OpenAI SDK pointed at the
operator's ``base_url`` — there is no separate shim. The offline
test exercises the supported path on a realistic Chat Completions
response body; the live test is opt-in: marked ``live_ollama`` and
skipped unless a local Ollama instance answers on the default port
(and the OpenAI SDK is installed).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from inkfoot.ledger import CausalTokenLedger
from inkfoot.pricing import estimate_nanodollars
from inkfoot.providers import OpenAICompatProvider

_OLLAMA_BASE_URL = "http://localhost:11434/v1"
_OLLAMA_VERSION_URL = "http://localhost:11434/api/version"


# ----------------------------------------------------------------------
# Offline: wire-shape round trip
# ----------------------------------------------------------------------


def test_chat_completions_response_round_trips_to_a_zero_estimate() -> None:
    provider = OpenAICompatProvider(
        base_url=_OLLAMA_BASE_URL, model="llama3.2"
    )
    # A realistic Ollama /v1/chat/completions body.
    response = {
        "id": "chatcmpl-431",
        "object": "chat.completion",
        "model": "llama3.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 31,
            "completion_tokens": 2,
            "total_tokens": 33,
        },
    }

    usage = provider.map_usage(response)
    assert usage.input_tokens == 31
    assert usage.output_tokens == 2
    assert usage.cache_status == "n/a"

    ledger = CausalTokenLedger(
        user_input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    # The wildcard row prices self-hosted at exactly $0 — present,
    # not unknown.
    assert estimate_nanodollars("openai_compat", "llama3.2", ledger) == 0


# ----------------------------------------------------------------------
# Live smoke (opt-in)
# ----------------------------------------------------------------------


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(_OLLAMA_VERSION_URL, timeout=2)
    except (urllib.error.URLError, OSError):
        return False
    return True


@pytest.mark.live_ollama
def test_live_ollama_call_maps_usage() -> None:
    openai = pytest.importorskip("openai")
    if not _ollama_reachable():
        pytest.skip("no Ollama instance reachable on localhost:11434")

    model = os.environ.get("INKFOOT_LIVE_OLLAMA_MODEL") or "llama3.2"
    provider = OpenAICompatProvider(
        base_url=_OLLAMA_BASE_URL, model=model, api_key="ollama"
    )
    client = openai.OpenAI(
        base_url=provider.base_url, api_key=provider.api_key
    )
    response = client.chat.completions.create(
        model=provider.model,
        messages=[
            {
                "role": "user",
                "content": "Reply with the single word OK.",
            }
        ],
        max_tokens=16,
    )

    usage = provider.map_usage(response)
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0

    ledger = CausalTokenLedger(
        user_input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    assert estimate_nanodollars("openai_compat", model, ledger) == 0
