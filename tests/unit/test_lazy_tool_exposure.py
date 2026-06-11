"""Unit tests for the ``LazyToolExposure`` modification policy.

All tests drive ``before_call``/``after_call`` directly with a
hand-built :class:`CallContext`; the event sink is monkeypatched so
no storage is needed. The end-to-end path (shim dispatch + real
storage) is covered in ``tests/integration``.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from inkfoot.policy import CallContext, IntegrationPattern
from inkfoot.policy.lazy_tool_exposure import LazyToolExposure


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


@pytest.fixture()
def events(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture policy events instead of writing to storage."""
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "inkfoot.policy.lazy_tool_exposure.emit_policy_event",
        lambda run_id, kind, payload: captured.append((kind, payload)),
    )
    return captured


def _anthropic_tool(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": f"{name} tool", **extra}


def _openai_tool(name: str, **extra: Any) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, **extra}}


def _ctx(
    tools: Any,
    *,
    messages: Optional[list] = None,
    run_id: str = "run-1",
) -> CallContext:
    return CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id=run_id,
        request_kwargs={"tools": tools, "messages": messages or []},
    )


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


# ----------------------------------------------------------------------
# Constructor validation
# ----------------------------------------------------------------------


def test_stale_after_turns_zero_raises() -> None:
    with pytest.raises(ValueError, match="stale_after_turns"):
        LazyToolExposure(stale_after_turns=0)


def test_stale_after_turns_negative_raises() -> None:
    with pytest.raises(ValueError, match="stale_after_turns"):
        LazyToolExposure(stale_after_turns=-3)


def test_stale_after_turns_non_int_raises() -> None:
    with pytest.raises(ValueError, match="stale_after_turns"):
        LazyToolExposure(stale_after_turns=2.5)  # type: ignore[arg-type]


def test_core_tools_empty_string_raises() -> None:
    with pytest.raises(ValueError, match="core_tools"):
        LazyToolExposure(core_tools=["search", ""])


def test_supported_patterns_is_pattern_c_only() -> None:
    assert LazyToolExposure.SUPPORTED_PATTERNS == {IntegrationPattern.C}


# ----------------------------------------------------------------------
# Pass-through cases
# ----------------------------------------------------------------------


def test_request_without_tools_passes_through(events) -> None:
    policy = LazyToolExposure()
    ctx = CallContext(
        provider="anthropic",
        model="claude-sonnet-4-6",
        run_id="r",
        request_kwargs={"messages": [_user("hi")]},
    )
    decision = policy.before_call(ctx)
    assert decision.action == "allow"
    assert "tools" not in ctx.request_kwargs
    assert events == []


def test_empty_tools_list_passes_through(events) -> None:
    policy = LazyToolExposure()
    ctx = _ctx([])
    policy.before_call(ctx)
    assert ctx.request_kwargs["tools"] == []
    assert events == []


def test_fresh_tools_are_all_kept(events) -> None:
    policy = LazyToolExposure(stale_after_turns=3)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    ctx = _ctx(tools)
    policy.before_call(ctx)
    assert ctx.request_kwargs["tools"] == tools
    assert events == []


# ----------------------------------------------------------------------
# Staleness window
# ----------------------------------------------------------------------


def test_tool_unused_for_three_turns_is_dropped_on_next_call(events) -> None:
    """Default window: a tool last relevant at turn 1 survives turns
    2-4 (three idle turns) and is dropped from the 5th call."""
    policy = LazyToolExposure(stale_after_turns=3)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]

    for turn in range(1, 5):  # turns 1..4: calc still inside window
        ctx = _ctx(list(tools), messages=[_user("use search please")])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names, f"calc dropped too early at turn {turn}"

    ctx = _ctx(list(tools), messages=[_user("use search please")])
    policy.before_call(ctx)
    names = [t["name"] for t in ctx.request_kwargs["tools"]]
    assert names == ["search"]
    assert events == [("lazy_tool_dropped", {"dropped": ["calc"], "turn": 5})]


def test_mention_in_user_question_keeps_tool_fresh(events) -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]

    for _ in range(4):
        ctx = _ctx(list(tools), messages=[_user("search and calc this")])
        policy.before_call(ctx)
        assert len(ctx.request_kwargs["tools"]) == 2
    assert events == []


def test_called_tool_window_is_refreshed_by_after_call(events) -> None:
    """A tool invoked in the response stays exposed even when it is
    never mentioned in text."""
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    anthropic_response = {
        "content": [{"type": "tool_use", "name": "calc", "input": {}}]
    }

    for turn in range(1, 5):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names, f"calc dropped at turn {turn} despite being called"
        policy.after_call(ctx, anthropic_response)
    assert events == []


def test_after_call_handles_openai_tool_calls_shape() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_openai_tool("search"), _openai_tool("calc")]
    openai_response = {
        "choices": [
            {"message": {"tool_calls": [{"function": {"name": "calc"}}]}}
        ]
    }

    for _ in range(4):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["function"]["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names
        policy.after_call(ctx, openai_response)


# ----------------------------------------------------------------------
# Core tools
# ----------------------------------------------------------------------


def test_core_tools_by_name_are_never_dropped(events) -> None:
    policy = LazyToolExposure(stale_after_turns=1, core_tools=["calc"])
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]

    for _ in range(5):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names


def test_core_marker_on_anthropic_tool_dict_exempts_it() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [
        _anthropic_tool("search"),
        _anthropic_tool("calc", inkfoot_core=True),
    ]
    for _ in range(5):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names


def test_core_marker_inside_openai_function_dict_exempts_it() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [
        _openai_tool("search"),
        _openai_tool("calc", inkfoot_core=True),
    ]
    for _ in range(5):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["function"]["name"] for t in ctx.request_kwargs["tools"]]
        assert "calc" in names


# ----------------------------------------------------------------------
# Restoration
# ----------------------------------------------------------------------


def _drop_calc(policy: LazyToolExposure, tools: list) -> None:
    """Advance turns until ``calc`` is dropped (search kept fresh by
    the user question)."""
    for _ in range(10):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        if "calc" not in names:
            return
    pytest.fail("calc was never dropped within 10 turns")


def test_dropped_tool_restored_when_assistant_text_references_it(events) -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    _drop_calc(policy, tools)
    assert events[-1][0] == "lazy_tool_dropped"

    ctx = _ctx(
        list(tools),
        messages=[
            _user("search for it"),
            _assistant("I'd need the calc tool to finish this."),
        ],
    )
    policy.before_call(ctx)
    names = [t["name"] for t in ctx.request_kwargs["tools"]]
    assert "calc" in names
    assert events[-1][0] == "lazy_tool_restored"
    assert events[-1][1]["restored"] == ["calc"]


def test_dropped_tool_restored_when_user_question_references_it(events) -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    _drop_calc(policy, tools)

    ctx = _ctx(list(tools), messages=[_user("now calc the total")])
    policy.before_call(ctx)
    names = [t["name"] for t in ctx.request_kwargs["tools"]]
    assert "calc" in names
    assert events[-1][0] == "lazy_tool_restored"


def test_drop_event_fires_once_per_episode(events) -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    _drop_calc(policy, tools)
    for _ in range(3):  # calc stays dropped: no further events
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
    dropped = [e for e in events if e[0] == "lazy_tool_dropped"]
    assert len(dropped) == 1


def test_mention_matching_is_whole_word() -> None:
    """``calc`` inside ``recalculate`` must not count as a mention."""
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    dropped_seen = False
    for _ in range(4):
        ctx = _ctx(
            list(tools),
            messages=[_user("search and recalculate the totals")],
        )
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        if "calc" not in names:
            dropped_seen = True
    assert dropped_seen


def test_tool_name_inside_tool_result_is_not_a_mention() -> None:
    """Tool-result blocks ride on user messages; a tool name inside
    its own result must not refresh the window."""
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "search for it"},
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "calc returned 42; calc is great",
                },
            ],
        }
    ]
    dropped_seen = False
    for _ in range(4):
        ctx = _ctx(list(tools), messages=[dict(m) for m in messages])
        policy.before_call(ctx)
        names = [t["name"] for t in ctx.request_kwargs["tools"]]
        if "calc" not in names:
            dropped_seen = True
    assert dropped_seen


# ----------------------------------------------------------------------
# Safety properties
# ----------------------------------------------------------------------


def test_never_narrows_to_empty_tools_list(events) -> None:
    """When every tool is stale the request is left unchanged —
    some provider APIs reject an empty tools array."""
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    for _ in range(5):
        ctx = _ctx(list(tools), messages=[_user("hello there")])
        policy.before_call(ctx)
        assert len(ctx.request_kwargs["tools"]) == 2
    assert events == []


def test_callers_list_object_is_not_mutated() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    original = [_anthropic_tool("search"), _anthropic_tool("calc")]
    for _ in range(4):
        ctx = _ctx(original, messages=[_user("search for it")])
        policy.before_call(ctx)
    # The original list still holds both tools even after narrowing.
    assert [t["name"] for t in original] == ["search", "calc"]
    assert ctx.request_kwargs["tools"] is not original
    assert [t["name"] for t in ctx.request_kwargs["tools"]] == ["search"]


def test_unnameable_tools_are_kept() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    weird = {"schema_only": True}
    tools = [_anthropic_tool("search"), weird, _anthropic_tool("calc")]
    for _ in range(4):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
    assert weird in ctx.request_kwargs["tools"]


def test_runs_have_independent_state() -> None:
    """Turn counters and staleness windows are per-run."""
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    for _ in range(4):  # age run-1 until calc is dropped
        ctx1 = _ctx(list(tools), messages=[_user("search for it")], run_id="run-1")
        policy.before_call(ctx1)
    assert [t["name"] for t in ctx1.request_kwargs["tools"]] == ["search"]

    ctx2 = _ctx(list(tools), messages=[_user("search for it")], run_id="run-2")
    policy.before_call(ctx2)
    assert len(ctx2.request_kwargs["tools"]) == 2


def test_reset_clears_run_state() -> None:
    policy = LazyToolExposure(stale_after_turns=1)
    tools = [_anthropic_tool("search"), _anthropic_tool("calc")]
    for _ in range(4):
        ctx = _ctx(list(tools), messages=[_user("search for it")])
        policy.before_call(ctx)
    assert [t["name"] for t in ctx.request_kwargs["tools"]] == ["search"]

    policy.reset()
    ctx = _ctx(list(tools), messages=[_user("search for it")])
    policy.before_call(ctx)
    assert len(ctx.request_kwargs["tools"]) == 2
