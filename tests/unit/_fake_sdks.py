"""Fake Anthropic + OpenAI SDK modules for shim tests.

We don't want to depend on the real SDKs in unit tests — they're
heavy, network-coupled, and version-sensitive. The shims monkey-patch
at the *attribute* level, so we stand up just enough of the module
hierarchy:

* ``anthropic.resources.messages.Messages.create`` (sync)
* ``anthropic.resources.messages.AsyncMessages.create`` (async)
* ``openai.resources.chat.completions.Completions.create``
* ``openai.resources.chat.completions.AsyncCompletions.create``

Each fake ``.create`` records its invocation in ``shim_calls`` and
returns a dict that mimics the real provider's usage shape so the
translator can build a ledger.
"""

from __future__ import annotations

import sys
import types
from typing import Any


def install_fake_anthropic() -> dict:
    """Install a fake ``anthropic`` module hierarchy and return its
    call log. Repeated calls return the same log (idempotent)."""
    if "anthropic" in sys.modules:
        # Test isolation: tear down any leftover fake.
        for key in list(sys.modules):
            if key == "anthropic" or key.startswith("anthropic."):
                del sys.modules[key]

    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    calls: list[dict[str, Any]] = []

    class Messages:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "sync", "args": args, "kwargs": kwargs})
            return {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ack"}],
            }

    class AsyncMessages:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "async", "args": args, "kwargs": kwargs})
            return {
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 6,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ack-async"}],
            }

    messages_mod.Messages = Messages
    messages_mod.AsyncMessages = AsyncMessages
    resources_mod.messages = messages_mod
    anthropic_mod.resources = resources_mod

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod

    return {
        "calls": calls,
        "Messages": Messages,
        "AsyncMessages": AsyncMessages,
        "module": anthropic_mod,
    }


def install_fake_openai() -> dict:
    """Install a fake ``openai`` module hierarchy and return its
    call log."""
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            del sys.modules[key]

    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "sync", "args": args, "kwargs": kwargs})
            return {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "choices": [
                    {"message": {"role": "assistant", "content": "ack"}}
                ],
            }

    class AsyncCompletions:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "async", "args": args, "kwargs": kwargs})
            return {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 6,
                    "total_tokens": 17,
                },
                "choices": [
                    {"message": {"role": "assistant", "content": "ack-async"}}
                ],
            }

    completions_mod.Completions = Completions
    completions_mod.AsyncCompletions = AsyncCompletions
    chat_mod.completions = completions_mod
    resources_mod.chat = chat_mod
    openai_mod.resources = resources_mod

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod

    return {
        "calls": calls,
        "Completions": Completions,
        "AsyncCompletions": AsyncCompletions,
        "module": openai_mod,
    }


def uninstall_fake_sdks() -> None:
    """Drop both fakes from ``sys.modules``."""
    for prefix in ("anthropic", "openai"):
        for key in list(sys.modules):
            if key == prefix or key.startswith(f"{prefix}."):
                del sys.modules[key]
