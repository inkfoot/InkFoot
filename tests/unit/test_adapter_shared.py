"""review finding #5 — parametrized tests for
:func:`inkfoot.adapters._shared.extract_tool_call`.

Different OpenAI / Anthropic Agent SDK builds dispatch tool calls
with different signatures. ``extract_tool_call`` is the single point
that normalises them into ``(tool_name, tool_args)`` for the
``tool_dispatched`` event. The four shapes below cover what we've
seen in the wild plus the dict-with-name shape OpenAI uses on the
streaming path.
"""

from __future__ import annotations

from typing import Any

import pytest

from inkfoot.adapters._shared import (
    extract_tool_call,
    stable_args_hash,
)


class _ToolWithNameAttr:
    """Stand-in for a Tool / ``StructuredTool`` instance — exposes
    ``.name`` and ``.args`` attributes the dispatcher might pass
    through unchanged."""

    def __init__(self, name: str, args: Any) -> None:
        self.name = name
        self.args = args


@pytest.mark.parametrize(
    "args,kwargs,expected_name,expected_args",
    [
        # Shape (a): kwargs-only call.
        # _call_tool(tool_name="search", tool_args={"q": "x"})
        ((), {"tool_name": "search", "tool_args": {"q": "x"}}, "search", {"q": "x"}),
        # Shape (a'): "name" / "args" kwargs (older SDK build).
        ((), {"name": "search", "args": {"q": "x"}}, "search", {"q": "x"}),
        # Shape (b): positional (tool_name, tool_args).
        # _call_tool("search", {"q": "x"})
        (("search", {"q": "x"}), {}, "search", {"q": "x"}),
        # Shape (c): positional tool object with .name + .args.
        # _call_tool(StructuredTool(name="search", args=...))
        (
            (_ToolWithNameAttr("search", {"q": "x"}),),
            {},
            "search",
            {"q": "x"},
        ),
        # Shape (d): OpenAI-style dict {"name": ..., "arguments": ...}.
        # _call_tool({"name": "search", "arguments": "{\"q\":\"x\"}"})
        (
            ({"name": "search", "arguments": '{"q":"x"}'},),
            {},
            "search",
            '{"q":"x"}',
        ),
        # Shape (d'): alt dict shape — {"name": ..., "args": ...}.
        (
            ({"name": "search", "args": {"q": "x"}},),
            {},
            "search",
            {"q": "x"},
        ),
        # Edge: empty call → "unknown" / None.
        ((), {}, "unknown", None),
        # Edge: only a name, no args.
        (("search",), {}, "search", None),
        # Edge: tool object without name attribute.
        ((object(),), {}, "unknown", None),
    ],
)
def test_extract_tool_call_handles_known_signature_shapes(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_name: str,
    expected_args: Any,
) -> None:
    name, tool_args = extract_tool_call(args, kwargs)
    assert name == expected_name
    assert tool_args == expected_args


def test_extract_tool_call_prefers_kwargs_over_positional() -> None:
    """When both kwargs and positional shapes are present, kwargs
    win. Real SDKs don't mix shapes but the fallback chain has to
    pick one — kwargs is the more-explicit signal."""
    name, args = extract_tool_call(
        ("positional_name",),
        {"tool_name": "kwarg_name"},
    )
    assert name == "kwarg_name"


def test_extract_tool_call_does_not_misread_string_kwarg_value() -> None:
    """A tool literally named ``"tool_name"`` (unlikely but legal)
    should still resolve to ``"tool_name"`` — the implementation
    reads the kwarg *value*, not the key, so the literal string
    round-trips."""
    name, args = extract_tool_call((), {"tool_name": "tool_name"})
    assert name == "tool_name"


def test_stable_args_hash_is_stable_across_key_orderings() -> None:
    h1 = stable_args_hash({"q": "x", "city": "Tokyo"})
    h2 = stable_args_hash({"city": "Tokyo", "q": "x"})
    assert h1 == h2
    assert len(h1) == 16


def test_stable_args_hash_handles_unjsonable_args() -> None:
    """Lambdas + open files don't JSON-serialise — the implementation
    falls back to ``repr(args)`` rather than raising."""
    h = stable_args_hash({"fn": lambda: None})
    assert isinstance(h, str) and len(h) == 16
