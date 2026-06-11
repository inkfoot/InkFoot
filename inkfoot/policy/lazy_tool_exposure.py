"""``LazyToolExposure`` — narrow the per-turn tools list.

Agent loops typically expose every enabled tool on every turn; for a
five-tool agent that's thousands of tokens of tool definitions resent
each call. This policy applies a small heuristic classifier per turn
and drops the tools the turn doesn't plausibly need:

* Tools called within the most recent ``stale_after_turns`` turns are
  kept.
* Tools whose names appear in the user's question are kept.
* Tools tagged as core — via the constructor's ``core_tools`` set or
  an ``"inkfoot_core": true`` key on the tool dict — are always kept.
* Everything else is dropped from the current turn's request.

**Restoration.** When the model needs a dropped tool it says so in
text ("I'd need to look up X but don't see that tool available").
The next turn's classifier scans the latest assistant message for
dropped-tool names and restores any it finds, refreshing the tool's
staleness window so it isn't immediately re-dropped. A mention in the
user's question restores a dropped tool the same way.

**Why Pattern C only.** Under the bare SDK shim the agent's loop may
have cached the tools list before the shim ever sees it, so a rewrite
isn't reliable. A framework adapter has the framework's tool registry
in hand and re-supplies the full list every turn, which is exactly
the shape this policy needs.

The policy never mutates the caller's list object — it replaces the
``tools`` entry in the per-call kwargs dict with a fresh, narrowed
list, so the agent's own tool registry is untouched.

Events: ``lazy_tool_dropped`` (once per tool per drop episode, with
the dropped names) and ``lazy_tool_restored`` (when a previously
dropped tool comes back).
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any, Iterable, Optional

from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy._events import emit_policy_event

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext


# Tool-dict key that marks a tool as always-kept. Both the flat
# Anthropic shape and the nested OpenAI ``function`` dict are checked.
CORE_TOOL_MARKER = "inkfoot_core"


def _tool_name(tool: Any) -> str:
    """Tool name for both provider shapes: Anthropic's flat
    ``{"name": ...}`` and OpenAI's ``{"type": "function",
    "function": {"name": ...}}``. Unknown shapes yield ``""``."""
    if not isinstance(tool, dict):
        return ""
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    else:
        name = tool.get("name")
    return name if isinstance(name, str) else ""


def _is_core_tagged(tool: Any) -> bool:
    if not isinstance(tool, dict):
        return False
    if tool.get(CORE_TOOL_MARKER):
        return True
    fn = tool.get("function")
    return isinstance(fn, dict) and bool(fn.get(CORE_TOOL_MARKER))


def _message_text(msg: dict[str, Any]) -> str:
    """Text content of one message: plain string content, or the
    concatenated ``text`` blocks of a content list. Tool-result blocks
    are skipped — a tool name inside its own result must not count as
    a mention."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts)
    return ""


def _last_text_for_role(messages: Any, role: str) -> str:
    """Most recent non-empty text for ``role`` in the messages array."""
    last = ""
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == role:
            text = _message_text(msg)
            if text:
                last = text
    return last


def _mentions(text: str, name: str) -> bool:
    """Case-insensitive whole-word match of ``name`` inside ``text``.
    Underscores count as word characters, so ``datadog_metrics``
    matches only as the full identifier."""
    if not text or not name:
        return False
    return re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) is not None


def _called_tool_names(response: Any) -> tuple[str, ...]:
    """Names of tools invoked in a provider response. Handles the
    Anthropic shape (``tool_use`` content blocks) and the OpenAI shape
    (``choices[0].message.tool_calls``); dicts and SDK objects both."""
    names: list[str] = []

    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name:
                    names.append(name)

    choices = (
        response.get("choices")
        if isinstance(response, dict)
        else getattr(response, "choices", None)
    )
    if isinstance(choices, list) and choices:
        first = choices[0]
        msg = (
            first.get("message")
            if isinstance(first, dict)
            else getattr(first, "message", None)
        )
        tool_calls = (
            msg.get("tool_calls")
            if isinstance(msg, dict)
            else getattr(msg, "tool_calls", None)
        )
        for tc in tool_calls or []:
            fn = (
                tc.get("function")
                if isinstance(tc, dict)
                else getattr(tc, "function", None)
            )
            name = (
                fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            )
            if isinstance(name, str) and name:
                names.append(name)

    return tuple(names)


class _RunToolState:
    """Per-run sliding-window bookkeeping."""

    __slots__ = ("turn", "last_relevant", "dropped")

    def __init__(self) -> None:
        # Monotonic per-run turn counter; one increment per LLM call
        # that carries a tools list.
        self.turn = 0
        # tool name -> the turn at which it was last relevant (called,
        # mentioned, referenced, or first offered).
        self.last_relevant: dict[str, int] = {}
        # Names currently withheld from the request — tracked so a
        # comeback emits ``lazy_tool_restored`` rather than nothing.
        self.dropped: set[str] = set()


class LazyToolExposure(Policy):
    """Heuristic per-turn tool-set narrowing (framework adapters only).

    ``stale_after_turns`` is the relevance window: a tool last
    called/mentioned at turn *T* stays exposed through turn
    ``T + stale_after_turns`` and is dropped from the call after that.
    ``core_tools`` names are exempt, as is any tool dict carrying a
    truthy ``"inkfoot_core"`` key.
    """

    NAME = "LazyToolExposure"
    SUPPORTED_PATTERNS = {IntegrationPattern.C}

    def __init__(
        self,
        *,
        stale_after_turns: int = 3,
        core_tools: Iterable[str] = (),
    ) -> None:
        if not isinstance(stale_after_turns, int) or stale_after_turns < 1:
            raise ValueError(
                f"LazyToolExposure: stale_after_turns must be a positive "
                f"integer, got {stale_after_turns!r}"
            )
        names = tuple(core_tools)
        if not all(isinstance(n, str) and n for n in names):
            raise ValueError(
                "LazyToolExposure: core_tools must be non-empty strings"
            )
        self._stale_after_turns = stale_after_turns
        self._core_tools = frozenset(names)
        self._runs: dict[str, _RunToolState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        tools = ctx.request_kwargs.get("tools")
        if not isinstance(tools, list) or not tools:
            return PolicyDecision(action="allow")

        messages = ctx.request_kwargs.get("messages")
        newly_dropped: list[str] = []
        restored: list[str] = []

        with self._lock:
            state = self._runs.setdefault(ctx.run_id, _RunToolState())
            state.turn += 1
            turn = state.turn
            question = _last_text_for_role(messages, "user")
            assistant = _last_text_for_role(messages, "assistant")

            kept: list[Any] = []
            for tool in tools:
                name = _tool_name(tool)
                if not name:
                    # Unnameable tools can't be classified — keep them.
                    kept.append(tool)
                    continue
                if name not in state.last_relevant:
                    # Grace window: a tool offered for the first time
                    # is fresh as of this turn.
                    state.last_relevant[name] = turn
                if _mentions(question, name) or _mentions(assistant, name):
                    state.last_relevant[name] = turn

                core = name in self._core_tools or _is_core_tagged(tool)
                fresh = turn - state.last_relevant[name] <= self._stale_after_turns
                if core or fresh:
                    kept.append(tool)
                    if name in state.dropped:
                        state.dropped.discard(name)
                        restored.append(name)
                else:
                    if name not in state.dropped:
                        newly_dropped.append(name)
                    state.dropped.add(name)

            # Never narrow to an empty tools list — some provider APIs
            # reject an empty array, and an all-stale turn shouldn't
            # turn into a request error. Leave the request unchanged.
            if kept and len(kept) != len(tools):
                # Replace the kwargs entry with a fresh list; the
                # caller's own list object is never mutated.
                ctx.request_kwargs["tools"] = kept
            elif not kept:
                # The full list goes out unchanged, so any
                # previously-dropped tools return silently — no
                # lazy_tool_restored event, because nothing was
                # narrowed this turn for them to be restored against.
                newly_dropped = []
                state.dropped.clear()

        if newly_dropped:
            emit_policy_event(
                ctx.run_id,
                "lazy_tool_dropped",
                {"dropped": newly_dropped, "turn": turn},
            )
        if restored:
            emit_policy_event(
                ctx.run_id,
                "lazy_tool_restored",
                {"restored": restored, "turn": turn},
            )
        return PolicyDecision(action="allow")

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        """Refresh the relevance window for every tool the response
        actually called."""
        names = _called_tool_names(response)
        if not names:
            return
        with self._lock:
            state = self._runs.get(ctx.run_id)
            if state is None:
                return
            for name in names:
                state.last_relevant[name] = state.turn

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()
