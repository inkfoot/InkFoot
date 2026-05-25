"""The ``normalise`` package — provider-neutral event payloads and
the recipes that translate raw provider responses into them.

This module exposes the *neutral* shape:

* :class:`NeutralCall` — frozen event payload for one LLM call, what
  the storage event log records (when ``capture_mode="metadata"``).
* :class:`NeutralError` — neutral wrapper around a provider error so
  reports don't have to know each SDK's exception shape.
* :func:`dict_to_neutral_call` — round-trip-safe deserialiser used
  by tests + the report CLI.
* :func:`update_stable_prefix` — the shortening-only longest-common
  prefix algorithm used by ``system_static`` / ``system_dynamic``
  attribution. Lives here because both translators need it.

Per-provider translators are in :mod:`inkfoot.normalise.anthropic`
and :mod:`inkfoot.normalise.openai`. Import them lazily so
``inkfoot.normalise`` stays cheap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Optional

from inkfoot.ledger import CausalTokenLedger

__all__ = [
    "NeutralCall",
    "NeutralError",
    "dict_to_neutral_call",
    "update_stable_prefix",
]


@dataclass(frozen=True, slots=True)
class NeutralError:
    """Provider-neutral error wrapper carried by :class:`NeutralCall`
    when the underlying SDK call failed. Truncated to keep the event
    payload small (privacy §9.3 caps user-facing error text at 1 KB).
    """

    type: str
    message: str = ""
    retryable: bool = False


_VALID_CACHE_STATUSES = frozenset({"hit", "partial", "miss", "n/a"})


@dataclass(frozen=True, slots=True)
class NeutralCall:
    """Provider-neutral payload for one LLM call.

    Field order follows the §5.4 class diagram. The ledger is
    **always** populated; estimation flags list which ledger fields
    were tokeniser-estimated rather than provider-reported.

    ``cache_status`` is validated in ``__post_init__`` so a
    translator that constructs :class:`NeutralCall` directly hits
    the same contract as one going through
    :func:`dict_to_neutral_call`.
    """

    provider: str
    model: str
    started_at: int  # Unix ms
    ended_at: int  # Unix ms
    ledger: CausalTokenLedger
    estimated_nanodollars: Optional[int] = None
    tools_offered: tuple[str, ...] = ()
    tools_called: tuple[str, ...] = ()
    error: Optional[NeutralError] = None
    cache_status: str = "n/a"  # "hit" | "partial" | "miss" | "n/a"
    parent_run_id: Optional[str] = None
    sequence: int = 0
    estimation_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cache_status not in _VALID_CACHE_STATUSES:
            raise ValueError(
                f"NeutralCall: invalid cache_status {self.cache_status!r}; "
                f"expected one of {sorted(_VALID_CACHE_STATUSES)}"
            )


def dict_to_neutral_call(payload: dict[str, Any]) -> NeutralCall:
    """Inverse of ``dataclasses.asdict(neutral_call)``.

    Used by the report CLI to deserialise events read back from the
    storage event log, and by the test that asserts round-trip
    losslessness. Accepts the dict shape produced by ``asdict`` —
    nested ``ledger`` as a dict, lists in place of tuples for
    ``tools_offered`` / ``tools_called`` / ``estimation_flags``,
    nested ``error`` as a dict.

    Rejects unknown top-level keys (defence against silently dropped
    fields after a schema bump).
    """
    expected = {f.name for f in fields(NeutralCall)}
    unknown = set(payload) - expected
    if unknown:
        raise ValueError(
            f"dict_to_neutral_call: unknown keys {sorted(unknown)} "
            f"in payload"
        )

    ledger_payload = payload.get("ledger")
    if ledger_payload is None:
        raise ValueError("dict_to_neutral_call: missing required 'ledger'")
    if isinstance(ledger_payload, CausalTokenLedger):
        ledger = ledger_payload
    elif isinstance(ledger_payload, dict):
        ledger_fields = {f.name for f in fields(CausalTokenLedger)}
        extra = set(ledger_payload) - ledger_fields
        if extra:
            raise ValueError(
                f"dict_to_neutral_call: unknown ledger keys {sorted(extra)}"
            )
        ledger = CausalTokenLedger(**ledger_payload)
    else:
        raise TypeError(
            f"dict_to_neutral_call: 'ledger' must be dict or "
            f"CausalTokenLedger, got {type(ledger_payload).__name__}"
        )

    error_payload = payload.get("error")
    error: Optional[NeutralError]
    if error_payload is None:
        error = None
    elif isinstance(error_payload, NeutralError):
        error = error_payload
    elif isinstance(error_payload, dict):
        error_fields = {f.name for f in fields(NeutralError)}
        extra = set(error_payload) - error_fields
        if extra:
            raise ValueError(
                f"dict_to_neutral_call: unknown error keys {sorted(extra)}"
            )
        error = NeutralError(**error_payload)
    else:
        raise TypeError(
            "dict_to_neutral_call: 'error' must be dict, NeutralError, or None"
        )

    # cache_status validation lives on NeutralCall.__post_init__ so
    # both the deserialiser and any translator constructing a
    # NeutralCall directly hit the same contract. We don't pre-check
    # here.
    return NeutralCall(
        provider=payload["provider"],
        model=payload["model"],
        started_at=payload["started_at"],
        ended_at=payload["ended_at"],
        ledger=ledger,
        estimated_nanodollars=payload.get("estimated_nanodollars"),
        tools_offered=tuple(payload.get("tools_offered") or ()),
        tools_called=tuple(payload.get("tools_called") or ()),
        error=error,
        cache_status=payload.get("cache_status", "n/a"),
        parent_run_id=payload.get("parent_run_id"),
        sequence=payload.get("sequence", 0),
        estimation_flags=tuple(payload.get("estimation_flags") or ()),
    )


def update_stable_prefix(current_prefix: str, new_system_block: str) -> str:
    """Return the new ``stable_system_prefix`` after observing
    ``new_system_block``.

    Algorithm (§5.3 stable-prefix detection): the longest character-
    level common prefix of the previous prefix and the incoming
    system block. **Monotonically shortening** — never grows. The
    first observation seeds the prefix with the entire system block.

    Edge cases:

    * If ``current_prefix`` is empty (first observation),
      ``new_system_block`` becomes the prefix.
    * If ``new_system_block`` is empty, the prefix collapses to the
      empty string. That matches the "system block changed
      completely" reading — it has no information to share with what
      came before.
    * Non-string inputs raise ``TypeError`` rather than coercing,
      because silently coercing a ``None`` system block would be
      indistinguishable from a "no system block" call.
    """
    if not isinstance(current_prefix, str):
        raise TypeError(
            f"current_prefix must be str, got {type(current_prefix).__name__}"
        )
    if not isinstance(new_system_block, str):
        raise TypeError(
            f"new_system_block must be str, got {type(new_system_block).__name__}"
        )

    if not current_prefix:
        return new_system_block

    limit = min(len(current_prefix), len(new_system_block))
    i = 0
    while i < limit and current_prefix[i] == new_system_block[i]:
        i += 1
    return current_prefix[:i]
