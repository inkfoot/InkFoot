"""``CacheControlPlacer`` — capability-aware prompt-cache help.

The policy reads the provider's declared ``prompt_cache_style``
from the provider registry and branches on the *style*, never on
the provider's name, so a custom provider gets the same treatment
as the built-in that pioneered its style:

* **``explicit_marker``** (Anthropic; Claude on Bedrock) —
  observe-only. Real injection of ``cache_control`` markers into
  the user's request is a modification policy for a future
  framework-adapter release. The current implementation inspects
  the request and *emits an advice event* when the system block or
  tool list looks like it could benefit from a marker that isn't
  there. The proposed marker placement rides in the event's
  metadata.
* **``cache_resource``** (Gemini) — active. The prompt cache is an
  explicit ``CachedContent`` resource created up front, not a
  request marker, so there's a safe non-rewriting action to take:
  the policy creates (or reuses) a resource keyed on the
  ``(model, system_instruction, tools)`` fingerprint via
  :class:`inkfoot.providers.gemini.GeminiCacheManager` and hands it
  to the Gemini shim through ``ctx.metadata``; the shim binds the
  call to it. The user's request kwargs are never mutated. When the
  resource can't be created (SDK missing, content below the
  provider minimum, API error) the policy degrades to the same
  advice-only event as the explicit-marker arm.
* **``automatic``** (OpenAI) **and ``none``** — silently ignored
  (automatic caching needs no help; ``none`` has nothing to place).
  A provider missing from the registry is treated as ``none``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import CallContext, PolicyDecision

from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision  # noqa: E402


# Approximate size below which it's not worth advising a marker.
# Anthropic only caches blocks that are at least 1024 tokens; the
# tokeniser is ~4 chars/token on English so we use 4096 chars as a
# rough lower bound.
_MIN_BLOCK_CHARS_FOR_ADVICE = 4096

# Gemini's CachedContent API enforces a minimum cacheable size —
# 32,768 tokens on the 1.5 family (newer models accept less). At ~4
# chars/token that's ~131k chars; we gate creation on the
# conservative figure so we never fire doomed creation calls for
# prompts the provider would reject.
_MIN_GEMINI_CACHE_CHARS = 131_072


def _has_cache_control_on_system(request_kwargs: dict[str, Any]) -> bool:
    """Return True iff the system block (string or list-of-blocks)
    already carries a ``cache_control`` marker."""
    system = request_kwargs.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("cache_control"):
                return True
    return False


def _system_size_chars(request_kwargs: dict[str, Any]) -> int:
    system = request_kwargs.get("system")
    if isinstance(system, str):
        return len(system)
    if isinstance(system, list):
        return sum(
            len(b.get("text", "")) if isinstance(b, dict) else 0
            for b in system
        )
    return 0


def _has_cache_control_on_tools(request_kwargs: dict[str, Any]) -> bool:
    tools = request_kwargs.get("tools") or []
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(t, dict) and t.get("cache_control") for t in tools
    )


def _tools_size_chars(request_kwargs: dict[str, Any]) -> int:
    import json

    tools = request_kwargs.get("tools") or []
    if not isinstance(tools, list) or not tools:
        return 0
    try:
        return len(json.dumps(tools, default=str))
    except (TypeError, ValueError):
        return 0


def _prompt_cache_style(provider: str, model: str) -> str:
    """The provider's declared ``prompt_cache_style`` for ``model``,
    or ``"none"`` when the provider string isn't in the registry — a
    provider the policy knows nothing about is left alone."""
    # Function-level import keeps the policy package import-light.
    from inkfoot.providers import ProviderRegistry  # noqa: PLC0415

    declared = ProviderRegistry.get(provider)
    if declared is None:
        return "none"
    return declared.get_capabilities(model).prompt_cache_style


class CacheControlPlacer(Policy):
    """Capability-aware prompt-cache help. Dispatch is on the
    ``prompt_cache_style`` the provider declares in the registry.

    On explicit-marker providers (Anthropic): advice for missing
    ``cache_control`` markers, fired once per run for each unmarked
    block that exceeds the minimum-size threshold (system block,
    tools array). The event metadata lists which blocks need
    markers + the proposed placement.

    On cache-resource providers (Gemini): creates/reuses a
    ``CachedContent`` resource for the stable
    ``system_instruction`` + ``tools`` prefix and attaches it to
    the call context for the shim to bind. Emits a
    ``cache_resource_created`` event on the creation call; degrades
    to advice when creation isn't possible.
    """

    NAME = "CacheControlPlacer"
    SUPPORTED_PATTERNS = {
        IntegrationPattern.A,
        IntegrationPattern.B,
        IntegrationPattern.C,
    }

    def __init__(self) -> None:
        # run_id -> fired block labels ("system", "tools",
        # "cache_resource").
        self._fired: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def before_call(self, ctx: "CallContext") -> "PolicyDecision":
        style = _prompt_cache_style(ctx.provider, ctx.model)
        if style == "cache_resource":
            return self._cache_resource_before_call(ctx)
        if style != "explicit_marker":
            return PolicyDecision(action="allow")

        advice: list[str] = []
        proposed: dict[str, Any] = {}

        if (
            not _has_cache_control_on_system(ctx.request_kwargs)
            and _system_size_chars(ctx.request_kwargs)
            >= _MIN_BLOCK_CHARS_FOR_ADVICE
        ):
            advice.append("system")
            proposed["system"] = {"type": "ephemeral"}

        if (
            not _has_cache_control_on_tools(ctx.request_kwargs)
            and _tools_size_chars(ctx.request_kwargs)
            >= _MIN_BLOCK_CHARS_FOR_ADVICE
        ):
            advice.append("tools")
            proposed["tools"] = {"type": "ephemeral"}

        if not advice:
            return PolicyDecision(action="allow")

        # Once-per-block-per-run.
        with self._lock:
            fired_blocks = self._fired.setdefault(ctx.run_id, set())
            new_advice = [b for b in advice if b not in fired_blocks]
            if not new_advice:
                return PolicyDecision(action="allow")
            fired_blocks.update(new_advice)

        return PolicyDecision(
            action="warn",
            reason=(
                "request has uncached "
                + " + ".join(new_advice)
                + " block(s) large enough to benefit from an explicit "
                "cache marker"
            ),
            metadata={"blocks": new_advice, "proposed_markers": proposed},
            emit_event_kind="cache_control_advice",
        )

    def _cache_resource_before_call(
        self, ctx: "CallContext"
    ) -> "PolicyDecision":
        """Create/reuse a ``CachedContent`` resource for the call's
        stable prefix and hand it to the shim via ``ctx.metadata``.
        (Gemini is the only shipped ``cache_resource`` provider, so
        the implementation is its ``CachedContent`` flow; a custom
        provider declaring the style degrades to the advice event
        below when no resource can be created for it.)

        The resource is attached on *every* qualifying call (the
        shim needs it each time to bind the request); only the
        events are once-per: ``cache_resource_created`` fires on the
        one call that created the resource, the advice fallback at
        most once per run.
        """
        # Function-level imports keep the policy package import-light
        # for the (common) deployments that never touch Gemini.
        from inkfoot._run_context import get_or_create_run_state  # noqa: PLC0415
        from inkfoot.providers.gemini import GEMINI_CACHE_MANAGER  # noqa: PLC0415
        from inkfoot.shims.gemini import CACHED_CONTENT_METADATA_KEY  # noqa: PLC0415

        system_instruction = ctx.request_kwargs.get("system_instruction")
        sys_chars = (
            len(system_instruction)
            if isinstance(system_instruction, str)
            else 0
        )
        total_chars = sys_chars + _tools_size_chars(ctx.request_kwargs)
        if total_chars < _MIN_GEMINI_CACHE_CHARS:
            return PolicyDecision(action="allow")

        resource, created = GEMINI_CACHE_MANAGER.get_or_create(
            model=ctx.model,
            system_instruction=system_instruction,
            tools=ctx.request_kwargs.get("tools"),
        )
        if resource is None:
            # SDK missing or creation failed — advice-only, once per
            # run, so the user still learns the prefix is cacheable.
            with self._lock:
                fired_blocks = self._fired.setdefault(ctx.run_id, set())
                if "cache_resource" in fired_blocks:
                    return PolicyDecision(action="allow")
                fired_blocks.add("cache_resource")
            return PolicyDecision(
                action="warn",
                reason=(
                    "Gemini request has a system_instruction/tools "
                    "prefix large enough for a CachedContent resource, "
                    "but one could not be created"
                ),
                metadata={"blocks": ["cache_resource"]},
                emit_event_kind="cache_control_advice",
            )

        ctx.metadata[CACHED_CONTENT_METADATA_KEY] = resource
        if not created:
            return PolicyDecision(action="allow")

        # One-time write: tell the translator to attribute this
        # call's cached-content count to cache_creation_tokens.
        state = get_or_create_run_state(ctx.run_id)
        state.pending_cache_resource_creation = True
        return PolicyDecision(
            action="warn",
            reason=(
                "created a Gemini CachedContent resource for the "
                "stable system_instruction/tools prefix"
            ),
            metadata={
                "model": ctx.model,
                "resource_name": getattr(resource, "name", None),
            },
            emit_event_kind="cache_resource_created",
        )

    def after_call(self, ctx: "CallContext", response: Any) -> None:
        # No-op in the current implementation. Future code can compare cache hit ratios
        # turn-over-turn and re-emit if the advice didn't take.
        return None

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._fired.clear()
