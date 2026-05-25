"""Tokeniser tests (E2-S3 acceptance).

Covers:
- OpenAI ``gpt-4o`` returns an exact ``tiktoken`` count with
  ``estimated=False``.
- Anthropic falls back to ``tiktoken`` (``o200k_base``) with
  ``estimated=True`` when ``anthropic.tokenize`` isn't importable.
- ``tokenise_tools`` JSON-serialises the array and tokenises the
  result; completes in under 10 ms for a 5-tool array.
- Edge cases: empty string, ``None`` text, ``None`` tools, model
  prefix dispatch, unknown provider falls through to flagged
  fallback.
"""

from __future__ import annotations

import sys
import time
import types

import pytest
import tiktoken

from inkfoot.tokenisers import (
    TokenCount,
    tokenise,
    tokenise_tools,
    tokenise_with_flags,
)


# ----------------------------------------------------------------------
# OpenAI exact counts
# ----------------------------------------------------------------------


def test_openai_gpt4o_returns_exact_tiktoken_count() -> None:
    expected = len(tiktoken.encoding_for_model("gpt-4o").encode("hello"))
    result = tokenise("hello", "gpt-4o")
    assert result == TokenCount(expected, False)


def test_openai_gpt4o_mini_uses_tiktoken_too() -> None:
    result = tokenise("a longer string for the tokeniser", "gpt-4o-mini")
    assert result.estimated is False
    assert result.value > 0


def test_openai_o1_dispatches_to_openai_path() -> None:
    result = tokenise("hi", "o1")
    assert result.estimated is False


def test_tokenise_with_flags_is_alias() -> None:
    a = tokenise("hello world", "gpt-4o")
    b = tokenise_with_flags("hello world", "gpt-4o")
    assert a == b


# ----------------------------------------------------------------------
# Anthropic fallback
# ----------------------------------------------------------------------


def test_anthropic_fallback_flags_estimated_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the ``anthropic`` import to fail so the tokeniser must
    fall back. Verifies the estimated flag surfaces."""

    # Insert a fake ``anthropic`` module that raises on import OR
    # remove anthropic from sys.modules + block its re-import.
    original = sys.modules.pop("anthropic", None)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    try:
        result = tokenise("hello there", "claude-sonnet-4-6")
        assert result.estimated is True
        assert result.value > 0
    finally:
        if original is not None:
            sys.modules["anthropic"] = original


def test_anthropic_uses_sdk_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``anthropic.tokenize`` exists and returns an int, the
    estimated flag is False."""
    fake = types.ModuleType("anthropic")
    fake.tokenize = lambda text: 42  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    result = tokenise("any text whatsoever", "claude-haiku-4-5")
    assert result == TokenCount(42, False)


def test_anthropic_falls_back_when_sdk_tokenizer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anthropic`` is importable but lacks a ``tokenize``
    function (older SDK versions). Tokeniser falls back, flagged."""
    fake = types.ModuleType("anthropic")
    # No 'tokenize' attribute.
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    result = tokenise("hello", "claude-opus-4-7")
    assert result.estimated is True


def test_anthropic_falls_back_when_sdk_tokenizer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK ``tokenize`` raising is treated as "not available"."""

    def boom(text):  # noqa: ANN001
        raise RuntimeError("upstream broke")

    fake = types.ModuleType("anthropic")
    fake.tokenize = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    result = tokenise("hello", "claude-sonnet-4-6")
    assert result.estimated is True


# ----------------------------------------------------------------------
# Unknown provider
# ----------------------------------------------------------------------


def test_unknown_provider_falls_back_with_estimation_flag() -> None:
    result = tokenise("hello", "mistral-large")
    assert result.estimated is True
    assert result.value > 0


# ----------------------------------------------------------------------
# Empty / None edges
# ----------------------------------------------------------------------


def test_empty_string_returns_zero_unflagged() -> None:
    assert tokenise("", "gpt-4o") == TokenCount(0, False)
    assert tokenise("", "claude-haiku-4-5") == TokenCount(0, False)


def test_none_text_raises_type_error() -> None:
    with pytest.raises(TypeError):
        tokenise(None, "gpt-4o")  # type: ignore[arg-type]


def test_non_string_text_raises_type_error() -> None:
    with pytest.raises(TypeError):
        tokenise(123, "gpt-4o")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# tokenise_tools
# ----------------------------------------------------------------------


def test_tokenise_tools_empty_returns_zero() -> None:
    assert tokenise_tools([], "gpt-4o") == TokenCount(0, False)


def test_tokenise_tools_none_raises() -> None:
    with pytest.raises(TypeError):
        tokenise_tools(None, "gpt-4o")  # type: ignore[arg-type]


def test_tokenise_tools_non_list_raises() -> None:
    with pytest.raises(TypeError):
        tokenise_tools({"not": "a list"}, "gpt-4o")  # type: ignore[arg-type]


def test_tokenise_tools_returns_positive_count_for_simple_array() -> None:
    tools = [
        {
            "name": "get_weather",
            "description": "Look up weather",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }
    ]
    result = tokenise_tools(tools, "gpt-4o")
    assert result.value > 5
    assert result.estimated is False


def test_tokenise_tools_completes_under_ten_ms_for_five_tools() -> None:
    tools = [
        {
            "name": f"tool_{i}",
            "description": f"Tool number {i} of five",
            "input_schema": {
                "type": "object",
                "properties": {
                    "arg1": {"type": "string"},
                    "arg2": {"type": "integer"},
                    "arg3": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["arg1"],
            },
        }
        for i in range(5)
    ]
    # Warm-up so we measure steady-state, not first-encoding load.
    tokenise_tools(tools, "gpt-4o")
    start = time.perf_counter()
    tokenise_tools(tools, "gpt-4o")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.010, (
        f"tokenise_tools on 5-tool array took {elapsed * 1000:.2f} ms "
        f"— exceeds 10 ms budget"
    )


def test_tokenise_tools_propagates_estimation_flag_on_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    tools = [{"name": "any_tool"}]
    result = tokenise_tools(tools, "claude-sonnet-4-6")
    assert result.estimated is True
