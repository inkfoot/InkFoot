"""Live ``AnthropicBedrock`` smoke tests (opt-in).

Anthropic's ``AnthropicBedrock`` client speaks the same
``messages.create`` shape as the direct client and reuses the same
resource class, so the Anthropic shim captures it with no extra
wiring. The only difference is the provider tag: the shim detects the
Bedrock client and emits ``provider="anthropic_bedrock"`` so the
Bedrock-namespaced model id resolves against the Bedrock pricing rows.

Opt-in: marked ``live_bedrock`` and skipped unless AWS credentials are
present. The call also needs the ``anthropic[bedrock]`` extra (which
pulls in boto3); the test skips cleanly when either is missing.
"""

from __future__ import annotations

import json
import os

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._run_context import _clear_current_run, _reset_ambient_run
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage


@pytest.fixture(autouse=True)
def reset_state():
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    _reset_ambient_run()
    _clear_current_run()


_HAS_AWS_CREDS = bool(
    os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
)
_MODEL = os.environ.get(
    "INKFOOT_LIVE_ANTHROPIC_BEDROCK_MODEL",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
)
_REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)


def _bedrock_client():
    """A real ``AnthropicBedrock`` client, or a skip when the extra
    isn't installed."""
    anthropic = pytest.importorskip("anthropic")
    pytest.importorskip("boto3")
    bedrock_cls = getattr(anthropic, "AnthropicBedrock", None)
    if bedrock_cls is None:
        pytest.skip("anthropic[bedrock] extra not installed")
    return bedrock_cls(aws_region=_REGION)


def _llm_payloads(storage: SQLiteStorage, run_id: str) -> list[dict]:
    return [
        json.loads(ev["payload_json"])
        for ev in storage.iter_events(run_id)
        if ev["kind"] == "llm_call"
    ]


@pytest.mark.live_bedrock
@pytest.mark.skipif(not _HAS_AWS_CREDS, reason="AWS credentials not set")
def test_live_bedrock_create_is_tagged_and_priced(tmp_path) -> None:
    client = _bedrock_client()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-anthropic-bedrock") as run:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=16,
            messages=[
                {"role": "user", "content": "Reply with the single word OK."}
            ],
        )
        run_id = run.id

    assert response.id.startswith("msg_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "anthropic_bedrock"
    assert payload["model"] == _MODEL
    assert payload["ledger"]["output_tokens"] > 0
    # The Bedrock-namespaced id resolves against the pricing table, so
    # the call carries a real cost rather than the unpriced ``None``.
    assert payload["estimated_nanodollars"] is not None
    assert payload["estimated_nanodollars"] > 0


@pytest.mark.live_bedrock
@pytest.mark.skipif(not _HAS_AWS_CREDS, reason="AWS credentials not set")
def test_live_bedrock_stream_is_tagged(tmp_path) -> None:
    client = _bedrock_client()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, langchain=False)

    with inkfoot.agent_run(task="live-anthropic-bedrock-stream") as run:
        with client.messages.stream(
            model=_MODEL,
            max_tokens=16,
            messages=[
                {"role": "user", "content": "Reply with the single word OK."}
            ],
        ) as stream:
            for _text in stream.text_stream:
                pass
            final = stream.get_final_message()
        run_id = run.id

    assert final.id.startswith("msg_")
    (payload,) = _llm_payloads(storage, run_id)
    assert payload["provider"] == "anthropic_bedrock"
    assert "stream_no_usage" not in payload["estimation_flags"]
    assert payload["ledger"]["output_tokens"] == final.usage.output_tokens
