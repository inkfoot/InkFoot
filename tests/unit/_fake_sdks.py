"""Fake Anthropic + OpenAI + Gemini SDK modules for shim tests.

We don't want to depend on the real SDKs in unit tests — they're
heavy, network-coupled, and version-sensitive. The shims monkey-patch
at the *attribute* level, so we stand up just enough of the module
hierarchy:

* ``anthropic.resources.messages.Messages.create`` (sync)
* ``anthropic.resources.messages.AsyncMessages.create`` (async)
* ``openai.resources.chat.completions.Completions.create``
* ``openai.resources.chat.completions.AsyncCompletions.create``
* ``openai.resources.responses.Responses.create``
* ``openai.resources.responses.AsyncResponses.create``
* ``google.generativeai.generative_models.GenerativeModel
  .generate_content`` (+ ``generate_content_async``)

Each fake entry point records its invocation in a call log and
returns a dict that mimics the real provider's usage shape so the
translator can build a ledger. The fake Gemini model also implements
``from_cached_content`` and a ``caching.CachedContent.create``
factory so cache-resource flows can run end to end offline; a
cache-bound model reports 256 cached tokens in its usage.
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
    # Exceptions queued here are raised (FIFO) by the next ``create``
    # call — after the attempt is recorded in ``calls`` — so tests can
    # drive the shim's error path offline.
    errors: list[BaseException] = []

    # Response ids mimic the real SDK's ``msg_...`` ids and are unique
    # per call (the emit-path dedup keys on them; a repeated id would
    # wrongly collapse two distinct calls).

    class Messages:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            calls.append({"variant": "sync", "args": args, "kwargs": kwargs})
            if errors:
                raise errors.pop(0)
            return {
                "id": f"msg_fake_{len(calls)}",
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
            if errors:
                raise errors.pop(0)
            return {
                "id": f"msg_fake_{len(calls)}",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 6,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "ack-async"}],
            }

    class Anthropic:
        """Client facade like the real SDK's — ``client.messages``
        is an instance of the same ``Messages`` class the shim
        patches, so client calls flow through the patched method."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.messages = Messages()

    messages_mod.Messages = Messages
    messages_mod.AsyncMessages = AsyncMessages
    resources_mod.messages = messages_mod
    anthropic_mod.resources = resources_mod
    anthropic_mod.Anthropic = Anthropic

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod

    return {
        "calls": calls,
        "errors": errors,
        "Messages": Messages,
        "AsyncMessages": AsyncMessages,
        "Anthropic": Anthropic,
        "module": anthropic_mod,
    }


def install_fake_openai(*, with_responses: bool = True) -> dict:
    """Install a fake ``openai`` module hierarchy and return its
    call log.

    ``with_responses=False`` mimics an SDK version that predates the
    Responses API — ``openai.resources.responses`` is absent, which
    is exactly the shape the Responses shim must skip gracefully.
    """
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            del sys.modules[key]

    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")
    responses_mod = types.ModuleType("openai.resources.responses")

    calls: list[dict[str, Any]] = []
    responses_calls: list[dict[str, Any]] = []
    # Exceptions queued here are raised (FIFO) by the next Responses
    # ``create`` call — after the attempt is recorded — so tests can
    # drive the shim's error path offline.
    responses_errors: list[BaseException] = []

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

    # Response ids mimic the real API's ``resp_...`` ids and are
    # unique per call (the emit-path dedup keys on them; a repeated
    # id would wrongly collapse two distinct calls).

    def _responses_payload(variant: str, kwargs: dict) -> dict:
        return {
            "id": f"resp_fake_{len(responses_calls)}",
            "object": "response",
            "status": "completed",
            "model": kwargs.get("model", ""),
            "output": [
                {
                    "type": "message",
                    "id": "msg_fake",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ack" if variant == "sync" else "ack-async",
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 10 if variant == "sync" else 11,
                "output_tokens": 5 if variant == "sync" else 6,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15 if variant == "sync" else 17,
            },
        }

    class Responses:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            responses_calls.append(
                {"variant": "sync", "args": args, "kwargs": kwargs}
            )
            if responses_errors:
                raise responses_errors.pop(0)
            return _responses_payload("sync", kwargs)

    class AsyncResponses:
        async def create(self, *args: Any, **kwargs: Any) -> Any:
            responses_calls.append(
                {"variant": "async", "args": args, "kwargs": kwargs}
            )
            if responses_errors:
                raise responses_errors.pop(0)
            return _responses_payload("async", kwargs)

    class _Chat:
        def __init__(self) -> None:
            self.completions = Completions()

    class OpenAI:
        """Client facade like the real SDK's — ``client.chat.completions``
        and ``client.responses`` are instances of the same classes
        the shims patch, so client calls flow through the patched
        methods."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.chat = _Chat()
            if with_responses:
                self.responses = Responses()

    completions_mod.Completions = Completions
    completions_mod.AsyncCompletions = AsyncCompletions
    chat_mod.completions = completions_mod
    resources_mod.chat = chat_mod
    openai_mod.resources = resources_mod
    openai_mod.OpenAI = OpenAI

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod

    if with_responses:
        responses_mod.Responses = Responses
        responses_mod.AsyncResponses = AsyncResponses
        resources_mod.responses = responses_mod
        sys.modules["openai.resources.responses"] = responses_mod

    return {
        "calls": calls,
        "responses_calls": responses_calls,
        "responses_errors": responses_errors,
        "Completions": Completions,
        "AsyncCompletions": AsyncCompletions,
        "Responses": Responses,
        "AsyncResponses": AsyncResponses,
        "OpenAI": OpenAI,
        "module": openai_mod,
    }


def install_fake_gemini() -> dict:
    """Install a fake ``google.generativeai`` module hierarchy and
    return its call log.

    The fake mirrors the SDK surfaces the shim and policies touch:

    * ``GenerativeModel`` with the private construction-time attrs
      the shim reads (``_system_instruction``, ``_tools``,
      ``_generation_config``, ``_safety_settings``), the
      ``model_name`` property (``models/``-prefixed, like the real
      SDK), and both ``generate_content`` variants.
    * ``GenerativeModel.from_cached_content`` — returns a new model
      bound to the cache resource; bound models report 256 cached
      tokens so cache-read/creation attribution is observable.
    * ``caching.CachedContent.create`` — records each creation in
      ``cache_creations`` and returns a uniquely named resource.
    """
    for key in list(sys.modules):
        if key == "google" or key.startswith("google."):
            del sys.modules[key]

    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.generativeai")
    generative_models_mod = types.ModuleType(
        "google.generativeai.generative_models"
    )
    caching_mod = types.ModuleType("google.generativeai.caching")

    calls: list[dict[str, Any]] = []
    cache_creations: list[dict[str, Any]] = []

    class CachedContent:
        _counter = 0

        def __init__(self, name: str, model: Any) -> None:
            self.name = name
            self.model = model

        @classmethod
        def create(cls, model: Any = None, **kwargs: Any) -> "CachedContent":
            cls._counter += 1
            cache_creations.append({"model": model, "kwargs": kwargs})
            return cls(f"cachedContents/fake-{cls._counter}", model)

    class GenerativeModel:
        def __init__(
            self,
            model_name: str = "gemini-1.5-pro",
            *,
            system_instruction: Any = None,
            tools: Any = None,
            generation_config: Any = None,
            safety_settings: Any = None,
            **kwargs: Any,
        ) -> None:
            name = str(model_name)
            if not name.startswith("models/"):
                name = f"models/{name}"
            self._model_name = name
            self._system_instruction = system_instruction
            self._tools = tools
            self._generation_config = generation_config
            self._safety_settings = safety_settings
            self.cached_content: Any = None

        @property
        def model_name(self) -> str:
            return self._model_name

        @classmethod
        def from_cached_content(
            cls, cached_content: Any, **kwargs: Any
        ) -> "GenerativeModel":
            model = (
                getattr(cached_content, "model", None) or "gemini-1.5-pro"
            )
            instance = cls(str(model), **kwargs)
            instance.cached_content = cached_content
            return instance

        def _usage(self, base_input: int, base_output: int) -> dict:
            cached = 256 if self.cached_content is not None else 0
            return {
                "prompt_token_count": base_input + cached,
                "candidates_token_count": base_output,
                "cached_content_token_count": cached,
            }

        def generate_content(
            self, contents: Any = None, **kwargs: Any
        ) -> Any:
            calls.append(
                {
                    "variant": "sync",
                    "model": self,
                    "contents": contents,
                    "kwargs": kwargs,
                    "cached": self.cached_content is not None,
                }
            )
            return {
                "usage_metadata": self._usage(10, 5),
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ack"}],
                        }
                    }
                ],
            }

        async def generate_content_async(
            self, contents: Any = None, **kwargs: Any
        ) -> Any:
            calls.append(
                {
                    "variant": "async",
                    "model": self,
                    "contents": contents,
                    "kwargs": kwargs,
                    "cached": self.cached_content is not None,
                }
            )
            return {
                "usage_metadata": self._usage(11, 6),
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ack-async"}],
                        }
                    }
                ],
            }

    generative_models_mod.GenerativeModel = GenerativeModel
    caching_mod.CachedContent = CachedContent
    genai_mod.GenerativeModel = GenerativeModel
    genai_mod.generative_models = generative_models_mod
    genai_mod.caching = caching_mod
    google_mod.generativeai = genai_mod

    sys.modules["google"] = google_mod
    sys.modules["google.generativeai"] = genai_mod
    sys.modules["google.generativeai.generative_models"] = (
        generative_models_mod
    )
    sys.modules["google.generativeai.caching"] = caching_mod

    return {
        "calls": calls,
        "cache_creations": cache_creations,
        "GenerativeModel": GenerativeModel,
        "CachedContent": CachedContent,
        "module": genai_mod,
    }


def uninstall_fake_sdks() -> None:
    """Drop all fakes from ``sys.modules``."""
    for prefix in ("anthropic", "openai", "google"):
        for key in list(sys.modules):
            if key == prefix or key.startswith(f"{prefix}."):
                del sys.modules[key]
