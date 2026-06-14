"""Storage-boundary redaction for replay-mode content.

Replay capture (``capture_mode='replay'``) persists full request and
response bodies so a run can be replayed offline. Those bodies routinely
carry secrets — API keys in headers, bearer tokens, personal data in
prompts. A *redaction hook* runs at the single storage boundary that
every shim and the streaming recorder funnel through, so un-redacted
content never reaches disk regardless of which capture surface produced
it.

The built-in :func:`default_redactor` is a regex *floor*: it masks the
shapes that must never be persisted — email addresses, the common
LLM-provider API-key prefixes, and JWTs. It is a floor, not a ceiling.
A deployment layers its own :class:`RedactionHook` on top via
``instrument(redaction_hook=...)`` and both run — the custom hook first,
the floor last — so a custom hook can extend, but never widen past, the
guaranteed minimum.

The floor scans string *values* recursively (inside lists and nested
objects); structural dictionary *keys* are left as-is, since in an LLM
body they are field names (``role``, ``content``, …) rather than
content. A deployment that puts secrets in keys must add a custom hook
to cover them.

Auditing is counts-only. The floor logs how many matches each pattern
made on the ``inkfoot.redaction`` logger and never the matched text;
logging the secret would defeat the redaction.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import (
    Any,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

__all__ = [
    "RedactionContext",
    "RedactionHook",
    "RedactionPayload",
    "RegexRedactor",
    "DEFAULT_PATTERNS",
    "REDACTION_PLACEHOLDER",
    "default_redactor",
    "compose",
    "resolve_redaction_hook",
    "apply_to_content",
]


# The audit log records *counts* per pattern, never the matched text.
_AUDIT_LOG = logging.getLogger("inkfoot.redaction")

# Replacement template; ``{name}`` is the pattern that fired. Chosen so
# the masked output never contains the original bytes.
REDACTION_PLACEHOLDER = "[REDACTED:{name}]"

# The three content fields a replay row carries. Hooks receive a payload
# keyed by these names; absent fields are ``None``.
_CONTENT_FIELDS = ("request", "response", "tool_result")


@dataclass(frozen=True)
class RedactionContext:
    """Read-only metadata about the event whose content is being
    redacted.

    Passed to every :class:`RedactionHook` so a custom hook can vary its
    behaviour by run, event kind, or position in the run. It carries no
    content — only the surrounding identifiers — so it is safe to log.
    """

    run_id: str
    event_id: str
    kind: str
    capture_mode: str
    sequence: int


# A hook is handed the deserialised content trio: a mapping with the
# keys in ``_CONTENT_FIELDS``, each holding a JSON-decoded body (dict,
# list, or scalar) or ``None`` when that body is absent.
RedactionPayload = dict[str, Any]


@runtime_checkable
class RedactionHook(Protocol):
    """A callable that redacts one event's replay content.

    The hook receives a :data:`RedactionPayload` — a mapping with the
    keys ``"request"``, ``"response"``, and ``"tool_result"``, each
    holding the deserialised JSON body or ``None`` — and a
    :class:`RedactionContext`. It returns a mapping of the same shape
    with sensitive values masked.

    Contract:

    * Return a *new* mapping; do not mutate the argument in place. The
      storage layer diffs the input against the output to decide
      whether anything was redacted.
    * Preserve the three content keys. A key that is dropped is treated
      as "redact this body away" — the stored field becomes null rather
      than the original, so a buggy hook fails closed, never open.
    """

    def __call__(
        self, payload: RedactionPayload, ctx: RedactionContext
    ) -> RedactionPayload: ...


# Default regex floor. Each named pattern is applied to every string
# leaf of the payload. ``anthropic_key`` precedes ``openai_key`` because
# ``sk-ant-`` is a prefix-superset of ``sk-``; matching the more
# specific shape first keeps the audit counts honest.
DEFAULT_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    ),
    (
        "anthropic_key",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    ),
    (
        "openai_key",
        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    ),
    (
        # A JWT is three base64url segments joined by dots; the header
        # segment always starts with ``eyJ`` (base64url of ``{"``),
        # which keeps this from matching arbitrary dotted text.
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    ),
)


class RegexRedactor:
    """The default :class:`RedactionHook` — a regex floor.

    Walks the payload, replacing every match of each configured pattern
    in every string leaf with ``[REDACTED:<name>]``. Dictionary keys are
    left untouched (they are structural field names, not content);
    values, list items, and nested structures are scrubbed recursively.

    After one pass it emits a single counts-only ``redaction_audit`` log
    record when anything matched — per-pattern hit counts, never the
    matched text.
    """

    def __init__(
        self,
        patterns: Optional[Sequence[tuple[str, "re.Pattern[str]"]]] = None,
        *,
        placeholder: str = REDACTION_PLACEHOLDER,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._patterns = (
            tuple(patterns) if patterns is not None else DEFAULT_PATTERNS
        )
        self._placeholder = placeholder
        self._log = logger if logger is not None else _AUDIT_LOG

    def __call__(
        self, payload: RedactionPayload, ctx: RedactionContext
    ) -> RedactionPayload:
        counts: dict[str, int] = {}
        redacted = {
            key: self._scrub(value, counts) for key, value in payload.items()
        }
        if counts:
            self._audit(counts, ctx)
        return redacted

    def _scrub(self, value: Any, counts: dict[str, int]) -> Any:
        if isinstance(value, str):
            return self._scrub_str(value, counts)
        if isinstance(value, Mapping):
            return {k: self._scrub(v, counts) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(item, counts) for item in value]
        return value

    def _scrub_str(self, text: str, counts: dict[str, int]) -> str:
        for name, pattern in self._patterns:
            replacement = self._placeholder.format(name=name)
            # A function replacement sidesteps backreference handling in
            # the placeholder so an exotic template can't corrupt output.
            text, hits = pattern.subn(lambda _m, _r=replacement: _r, text)
            if hits:
                counts[name] = counts.get(name, 0) + hits
        return text

    def _audit(self, counts: dict[str, int], ctx: RedactionContext) -> None:
        summary = " ".join(f"{name}={counts[name]}" for name in sorted(counts))
        self._log.info(
            "redaction_audit run=%s event=%s kind=%s %s",
            ctx.run_id,
            ctx.event_id,
            ctx.kind,
            summary,
            extra={"redaction_counts": dict(counts)},
        )


def _identity(payload: RedactionPayload, ctx: RedactionContext) -> RedactionPayload:
    return payload


def compose(*hooks: Optional[RedactionHook]) -> RedactionHook:
    """Chain hooks left-to-right into one hook.

    The output payload of each hook becomes the input of the next.
    ``None`` entries are skipped; composing nothing yields an identity
    hook.
    """
    chain = tuple(hook for hook in hooks if hook is not None)
    if not chain:
        return _identity
    if len(chain) == 1:
        return chain[0]

    def _composed(
        payload: RedactionPayload, ctx: RedactionContext
    ) -> RedactionPayload:
        for hook in chain:
            payload = hook(payload, ctx)
        return payload

    return _composed


def default_redactor() -> RegexRedactor:
    """The regex floor applied by default whenever replay capture is on."""
    return RegexRedactor()


def resolve_redaction_hook(
    capture_mode: str, custom: Optional[RedactionHook]
) -> Optional[RedactionHook]:
    """Pick the effective hook for a capture mode.

    * ``metadata`` — no content is persisted, so redaction is moot:
      returns ``None`` even when a custom hook was supplied.
    * ``replay`` — the floor always runs. A supplied custom hook runs
      *first*; the floor runs *last*, so a custom hook can extend the
      redaction but never widen what reaches disk past the floor.
    """
    if capture_mode != "replay":
        return None
    floor = default_redactor()
    if custom is None:
        return floor
    return compose(custom, floor)


def _maybe_load(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Unparseable content is handed to the hook as a raw string leaf
        # so the floor still scrubs it; it is re-serialised on the way
        # out. The real shim only ever writes valid JSON, so this is a
        # defensive fallback that preserves the no-leak guarantee.
        return raw


def _dump_field(value: Any, original: Optional[str]) -> Optional[str]:
    # An absent field stays absent; a hook that dropped a body to
    # ``None`` is stored as a true SQL NULL rather than the JSON literal
    # ``"null"`` (the content fields are never legitimately JSON null —
    # the shim only ever serialises dicts here).
    if original is None or value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover — default=str rarely fails
        return json.dumps(str(value))


def _content_changed(
    before: RedactionPayload, after: Mapping[str, Any]
) -> bool:
    return any(after.get(key) != before.get(key) for key in _CONTENT_FIELDS)


def apply_to_content(
    hook: Optional[RedactionHook],
    *,
    ctx: RedactionContext,
    request_json: Optional[str],
    response_json: Optional[str],
    tool_result_json: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Run ``hook`` over the three serialised content fields.

    Returns the ``(request_json, response_json, tool_result_json)`` trio
    — rewritten when the hook changed anything, otherwise the inputs
    untouched — plus a ``content_redacted`` flag.

    With ``hook is None`` the inputs pass straight through and the flag
    is ``False``. Each field is deserialised, redacted as part of one
    payload, then re-serialised, so a hook sees the whole event's
    content at once. A hook that raises or returns a non-mapping fails
    closed: the content is dropped (stored as null) rather than written
    unredacted.
    """
    if hook is None:
        return request_json, response_json, tool_result_json, False

    payload: RedactionPayload = {
        "request": _maybe_load(request_json),
        "response": _maybe_load(response_json),
        "tool_result": _maybe_load(tool_result_json),
    }
    before = copy.deepcopy(payload)

    try:
        redacted = hook(payload, ctx)
    except Exception:  # pylint: disable=broad-except
        _AUDIT_LOG.warning(
            "redaction hook raised for event %s; dropping replay content "
            "to avoid persisting unredacted data",
            ctx.event_id,
            exc_info=True,
        )
        return None, None, None, True

    if not isinstance(redacted, Mapping):
        _AUDIT_LOG.warning(
            "redaction hook for event %s returned %s, expected a mapping; "
            "dropping replay content",
            ctx.event_id,
            type(redacted).__name__,
        )
        return None, None, None, True

    if not _content_changed(before, redacted):
        return request_json, response_json, tool_result_json, False

    return (
        _dump_field(redacted.get("request"), request_json),
        _dump_field(redacted.get("response"), response_json),
        _dump_field(redacted.get("tool_result"), tool_result_json),
        True,
    )
