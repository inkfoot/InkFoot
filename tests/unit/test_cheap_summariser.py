"""Unit tests for the ``CheapSummariser`` modification policy.

The cheap-model round-trip helpers are monkeypatched throughout, so
no SDK (real or fake) is involved; the shim-dispatched sub-call path
is covered in ``tests/integration``. A/B trust mode has its own file
(``test_cheap_summariser_ab.py``).
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
import tiktoken

from inkfoot.ledger import CausalTokenLedger
from inkfoot.normalise import NeutralCall
from inkfoot.policy import CallContext, IntegrationPattern
from inkfoot.policy.cheap_summariser import (
    CheapSummariser,
    KILL_SWITCH_TAG,
    _clear_disabled_tasks,
    _in_summariser_call,
    _truncate_to_tokens,
    disable_summariser_for_task,
    summariser_disabled_for_task,
)
from inkfoot.shims._emit import (
    SUMMARISER_CALL_METADATA_KEY,
    _fold_into_summariser_tokens,
)
from inkfoot.storage.sqlite import SQLiteStorage


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


THRESHOLD = 50

# Clearly above any 50-token threshold under every tokeniser; varied
# enough that no encoder collapses it.
BIG_TEXT = " ".join(f"row-{i} status=ok latency={i % 97}ms" for i in range(200))
OTHER_BIG_TEXT = " ".join(f"item-{i} weight={i % 13}kg" for i in range(200))


@pytest.fixture(autouse=True)
def clean_kill_switch():
    _clear_disabled_tasks()
    yield
    _clear_disabled_tasks()


@pytest.fixture()
def events(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser.emit_policy_event",
        lambda run_id, kind, payload: captured.append((kind, payload)),
    )
    return captured


@pytest.fixture()
def model_calls(monkeypatch) -> list[dict[str, Any]]:
    """Replace both provider round-trips with a canned summary."""
    calls: list[dict[str, Any]] = []

    def fake_anthropic(model: str, prompt: str, max_tokens: int) -> Optional[str]:
        calls.append({"provider": "anthropic", "model": model, "prompt": prompt})
        return "condensed summary"

    def fake_openai(model: str, prompt: str, max_tokens: int) -> Optional[str]:
        calls.append({"provider": "openai", "model": model, "prompt": prompt})
        return "condensed summary"

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._anthropic_summary", fake_anthropic
    )
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._openai_summary", fake_openai
    )
    return calls


def _anthropic_ctx(result_text: str, *, run_id: str = "run-1") -> CallContext:
    return CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id=run_id,
        request_kwargs={
            "messages": [
                {"role": "user", "content": "what failed in the deploy?"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": result_text,
                        }
                    ],
                },
            ]
        },
    )


def _openai_ctx(result_text: str, *, run_id: str = "run-1") -> CallContext:
    return CallContext(
        provider="openai",
        model="gpt-4o",
        run_id=run_id,
        request_kwargs={
            "messages": [
                {"role": "user", "content": "what failed in the deploy?"},
                {"role": "tool", "tool_call_id": "call_1", "content": result_text},
            ]
        },
    )


def _result_content(ctx: CallContext) -> Any:
    """The (possibly rewritten) tool-result content of the harness ctx."""
    for msg in ctx.request_kwargs["messages"]:
        if msg.get("role") == "tool":
            return msg["content"]
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    return block["content"]
    return None


# ----------------------------------------------------------------------
# Constructor validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold_tokens": 0},
        {"max_summary_tokens": 0},
        {"ab_sample_rate": -0.1},
        {"ab_sample_rate": 1.5},
        {"regression_threshold": -0.01},
        {"regression_threshold": 1.01},
        {"regression_min_runs": 0},
    ],
)
def test_invalid_constructor_args_raise(kwargs) -> None:
    with pytest.raises(ValueError, match="CheapSummariser"):
        CheapSummariser(**kwargs)


def test_supported_patterns_is_pattern_c_only() -> None:
    assert CheapSummariser.SUPPORTED_PATTERNS == {IntegrationPattern.C}


# ----------------------------------------------------------------------
# Replacement
# ----------------------------------------------------------------------


def test_small_tool_result_passes_through(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx("tiny result")
    policy.before_call(ctx)
    assert _result_content(ctx) == "tiny result"
    assert model_calls == []
    assert events == []


def test_oversized_anthropic_tool_result_is_replaced(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    decision = policy.before_call(ctx)

    assert decision.action == "allow"
    assert _result_content(ctx) == "condensed summary"
    assert len(model_calls) == 1
    assert model_calls[0]["provider"] == "anthropic"
    assert model_calls[0]["model"] == "claude-haiku-4-5"
    # The summariser prompt carries the raw result + the user question.
    assert BIG_TEXT in model_calls[0]["prompt"]
    assert "what failed in the deploy?" in model_calls[0]["prompt"]


def test_oversized_openai_tool_message_is_replaced(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _openai_ctx(BIG_TEXT)
    policy.before_call(ctx)
    assert _result_content(ctx) == "condensed summary"
    assert model_calls[0]["provider"] == "openai"
    assert model_calls[0]["model"] == "gpt-4o-mini"


def test_replaced_event_payload(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)

    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "summariser_replaced"
    assert payload["summariser_model"] == "claude-haiku-4-5"
    assert payload["tool_id"] == "toolu_1"
    assert payload["original_tokens"] > THRESHOLD
    assert payload["summary_tokens"] <= 600
    assert payload["raw"] == BIG_TEXT  # preserve_for_replay default


def test_preserve_for_replay_false_omits_raw(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD, preserve_for_replay=False)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)
    _, payload = events[0]
    assert "raw" not in payload


def test_multipart_text_tool_result_is_replaced(model_calls) -> None:
    """Anthropic tool_result content may be a list of text blocks."""
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id="run-1",
        request_kwargs={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [
                                {"type": "text", "text": BIG_TEXT[: len(BIG_TEXT) // 2]},
                                {"type": "text", "text": BIG_TEXT[len(BIG_TEXT) // 2 :]},
                            ],
                        }
                    ],
                },
            ]
        },
    )
    policy.before_call(ctx)
    block = ctx.request_kwargs["messages"][0]["content"][0]
    assert block["content"] == "condensed summary"


def test_non_text_tool_result_is_left_alone(events, model_calls) -> None:
    """Image-bearing results must not be replaced with a string."""
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    image_content = [
        {"type": "image", "source": {"data": "x" * 5000}},
    ]
    ctx = CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id="run-1",
        request_kwargs={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": image_content,
                        }
                    ],
                }
            ]
        },
    )
    policy.before_call(ctx)
    block = ctx.request_kwargs["messages"][0]["content"][0]
    assert block["content"] is image_content
    assert model_calls == []
    assert events == []


# ----------------------------------------------------------------------
# Fallbacks + budget enforcement
# ----------------------------------------------------------------------


def test_unknown_provider_falls_back_to_truncation(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD, max_summary_tokens=40)
    ctx = CallContext(
        provider="mystery",
        model="mystery-large",
        run_id="run-1",
        request_kwargs={
            "messages": [
                {"role": "tool", "tool_call_id": "c1", "content": BIG_TEXT}
            ]
        },
    )
    policy.before_call(ctx)
    replaced = _result_content(ctx)
    assert replaced != BIG_TEXT
    assert replaced.endswith("[truncated by inkfoot]")
    assert model_calls == []
    _, payload = events[0]
    assert payload["summariser_model"] == "truncation"


def test_cheap_model_failure_falls_back_to_truncation(
    events, monkeypatch
) -> None:
    def boom(model: str, prompt: str, max_tokens: int) -> Optional[str]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._anthropic_summary", boom
    )
    policy = CheapSummariser(threshold_tokens=THRESHOLD, max_summary_tokens=40)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)  # must not raise

    replaced = _result_content(ctx)
    assert replaced.endswith("[truncated by inkfoot]")
    _, payload = events[0]
    assert payload["summariser_model"] == "truncation"


def test_overlong_model_summary_is_truncated_to_budget(
    events, monkeypatch
) -> None:
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._anthropic_summary",
        lambda model, prompt, max_tokens: OTHER_BIG_TEXT,
    )
    policy = CheapSummariser(threshold_tokens=THRESHOLD, max_summary_tokens=40)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)

    replaced = _result_content(ctx)
    encoding = tiktoken.get_encoding("o200k_base")
    assert len(encoding.encode(replaced)) <= 40


def test_twelve_thousand_token_result_fits_summary_budget(events, monkeypatch) -> None:
    """A ~12,500-token tool result is reduced to at most
    ``max_summary_tokens`` (default 600) even on the worst path
    (no cheap model available -> mechanical truncation)."""
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._anthropic_summary",
        lambda model, prompt, max_tokens: None,
    )
    encoding = tiktoken.get_encoding("o200k_base")
    huge = " ".join(f"log-{i} err={i % 7}" for i in range(2600))
    while len(encoding.encode(huge)) < 12_500:
        huge += " " + huge[: len(huge) // 2]

    policy = CheapSummariser()  # default thresholds: 1500 / 600
    ctx = _anthropic_ctx(huge)
    policy.before_call(ctx)

    replaced = _result_content(ctx)
    assert len(encoding.encode(replaced)) <= 600
    _, payload = events[0]
    assert payload["original_tokens"] > 1500
    assert payload["summary_tokens"] <= 600


def test_truncate_to_tokens_respects_budget_including_marker() -> None:
    encoding = tiktoken.get_encoding("o200k_base")
    out = _truncate_to_tokens(BIG_TEXT, 30, "claude-sonnet-4-6")
    assert len(encoding.encode(out)) <= 30
    assert out.endswith("[truncated by inkfoot]")


def test_truncate_to_tokens_short_text_is_unchanged() -> None:
    assert _truncate_to_tokens("short", 30, "claude-sonnet-4-6") == "short"


def test_truncate_respects_budget_when_model_tokeniser_disagrees(
    monkeypatch,
) -> None:
    """The budget binds in the *model's* tokeniser, not tiktoken's.
    With a model tokeniser that counts 1.5x tiktoken (the Anthropic
    direction of disagreement), a tiktoken-sized cut would overrun —
    the re-measure loop must shrink it under the budget."""
    from inkfoot.tokenisers import TokenCount

    encoding = tiktoken.get_encoding("o200k_base")

    def inflating_tokenise(text: str, model: str) -> TokenCount:
        return TokenCount(value=(len(encoding.encode(text)) * 3) // 2, estimated=False)

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser.tokenise", inflating_tokenise
    )

    out = _truncate_to_tokens(BIG_TEXT, 60, "claude-sonnet-4-6")
    assert inflating_tokenise(out, "claude-sonnet-4-6").value <= 60
    assert out.endswith("[truncated by inkfoot]")


# ----------------------------------------------------------------------
# Idempotence (content-hash cache)
# ----------------------------------------------------------------------


def test_repeated_result_uses_cache_without_second_call(
    events, model_calls
) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    policy.before_call(_anthropic_ctx(BIG_TEXT))
    assert len(model_calls) == 1
    assert len(events) == 1

    # Conversation history resends the same raw result on turn 2.
    ctx2 = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx2)
    assert _result_content(ctx2) == "condensed summary"
    assert len(model_calls) == 1  # no second round-trip
    assert len(events) == 1  # no duplicate event


def test_distinct_results_are_summarised_separately(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    policy.before_call(_anthropic_ctx(BIG_TEXT))
    policy.before_call(_anthropic_ctx(OTHER_BIG_TEXT))
    assert len(model_calls) == 2
    assert len(events) == 2


def test_reset_clears_summary_cache(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    policy.before_call(_anthropic_ctx(BIG_TEXT))
    policy.reset()
    policy.before_call(_anthropic_ctx(BIG_TEXT))
    assert len(model_calls) == 2


def test_task_is_read_from_storage_once_per_run(
    events, model_calls, monkeypatch
) -> None:
    reads: list[str] = []

    def counting_task_for_run(run_id: str) -> str:
        reads.append(run_id)
        return "triage"

    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", counting_task_for_run
    )
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    policy.before_call(_anthropic_ctx(BIG_TEXT))
    policy.before_call(_anthropic_ctx(OTHER_BIG_TEXT))
    assert reads == ["run-1"]

    # A different run is its own cache entry.
    policy.before_call(_anthropic_ctx(BIG_TEXT, run_id="run-2"))
    assert reads == ["run-1", "run-2"]


# ----------------------------------------------------------------------
# Re-entrancy guard
# ----------------------------------------------------------------------


def test_own_sub_call_is_skipped_and_stamped(events, model_calls) -> None:
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    token = _in_summariser_call.set(True)
    try:
        decision = policy.before_call(ctx)
    finally:
        _in_summariser_call.reset(token)

    assert decision.action == "allow"
    assert ctx.metadata[SUMMARISER_CALL_METADATA_KEY] is True
    assert _result_content(ctx) == BIG_TEXT  # untouched
    assert model_calls == []
    assert events == []


# ----------------------------------------------------------------------
# Kill switch
# ----------------------------------------------------------------------


def test_disabled_task_is_not_summarised(events, model_calls, monkeypatch) -> None:
    monkeypatch.setattr(
        "inkfoot.policy.cheap_summariser._task_for_run", lambda run_id: "triage"
    )
    disable_summariser_for_task("triage")
    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)
    assert _result_content(ctx) == BIG_TEXT
    assert model_calls == []
    assert events == []


def test_user_tag_kill_switch_disables_task(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    """``inkfoot.tag('disable_summariser', True)`` recorded as a
    ``user_tag`` event disables the run's task at the first trigger."""
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    storage.start_run(
        run_id="run-1", task="triage", agent_kind=None, started_at=1
    )
    storage.insert_event(
        event_id="ev-tag",
        run_id="run-1",
        kind="user_tag",
        occurred_at=2,
        sequence=1,
        payload_json=json.dumps({"key": KILL_SWITCH_TAG, "value": True}),
    )
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)

    assert _result_content(ctx) == BIG_TEXT
    assert model_calls == []
    assert summariser_disabled_for_task("triage")


def test_tag_with_falsey_value_does_not_disable(
    tmp_path, events, model_calls, monkeypatch
) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    storage.connect()
    storage.start_run(
        run_id="run-1", task="triage", agent_kind=None, started_at=1
    )
    storage.insert_event(
        event_id="ev-tag",
        run_id="run-1",
        kind="user_tag",
        occurred_at=2,
        sequence=1,
        payload_json=json.dumps({"key": KILL_SWITCH_TAG, "value": False}),
    )
    monkeypatch.setattr("inkfoot._instrument._STORAGE", storage)

    policy = CheapSummariser(threshold_tokens=THRESHOLD)
    ctx = _anthropic_ctx(BIG_TEXT)
    policy.before_call(ctx)

    assert _result_content(ctx) == "condensed summary"
    assert not summariser_disabled_for_task("triage")


# ----------------------------------------------------------------------
# Ledger re-attribution (emit-path fold)
# ----------------------------------------------------------------------


def _neutral_call(**ledger_kwargs: int) -> NeutralCall:
    return NeutralCall(
        provider="anthropic",
        model="claude-haiku-4-5",
        started_at=1,
        ended_at=2,
        ledger=CausalTokenLedger(**ledger_kwargs),
        sequence=1,
    )


def test_fold_moves_structural_input_into_summariser_tokens() -> None:
    call = _neutral_call(
        system_static_tokens=100,
        user_input_tokens=2000,
        tool_result_tokens=300,
        cache_read_tokens=80,
        output_tokens=55,
    )
    before_total = call.ledger.input_total

    folded = _fold_into_summariser_tokens(call)

    assert folded.ledger.summariser_tokens == before_total
    assert folded.ledger.input_total == before_total  # pricing-neutral
    assert folded.ledger.user_input_tokens == 0
    assert folded.ledger.tool_result_tokens == 0
    assert folded.ledger.cache_read_tokens == 80
    assert folded.ledger.output_tokens == 55
    assert folded.metadata[SUMMARISER_CALL_METADATA_KEY] is True


def test_fold_does_not_mutate_the_original_call() -> None:
    call = _neutral_call(user_input_tokens=500)
    _fold_into_summariser_tokens(call)
    assert call.ledger.user_input_tokens == 500
    assert call.ledger.summariser_tokens == 0
    assert SUMMARISER_CALL_METADATA_KEY not in call.metadata
