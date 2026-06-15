"""Opt-in, anonymous install telemetry.

Inkfoot can record a single anonymous "install" ping the first time
:func:`inkfoot.instrument` runs in an environment that has consented.
The ping exists for exactly one reason: to let the maintainers size
adoption without guessing. It is **off by default** and never runs
until the user has explicitly opted in.

What leaves the machine is deliberately tiny and is enumerated in full
in the privacy documentation. The payload is::

    {
      "event": "install",
      "installation": "<sha256 hex>",   # a random local id, hashed
      "inkfoot_version": "1.0.0",
      "python": "3.12",                 # major.minor only
      "os": "Linux"                     # platform.system()
    }

The installation id is generated locally and **hashed** before it is
sent — the raw id never goes over the wire, so two pings can be
recognised as "the same install" without that value being reversible
to anything on the machine. The payload carries no prompts, no tokens,
no costs, no file paths, no hostname, no username. None of the user's
data is sent.

Design rules, all load-bearing:

* **Default-off.** With no recorded decision and no environment
  override, consent defaults to *denied*. A prompt is shown only when
  the process is attached to an interactive terminal; in any
  non-interactive context (CI, a long-running service, a notebook
  kernel) the module records "denied" and returns without printing or
  blocking.
* **Never block, never raise.** Every entry point is wrapped so a
  failure — unwritable home directory, no network, a corrupt state
  file — degrades to "no ping" and is logged at debug level, never
  surfaced to the caller. Telemetry must never be able to break
  ``instrument()``.
* **At most one ping per (installation, version).** The recorded state
  remembers the last version pinged, so re-running ``instrument()`` —
  or upgrading and running again — never produces a duplicate.
* **Honours opt-out conventions.** ``DO_NOT_TRACK`` /
  ``INKFOOT_DO_NOT_TRACK`` set to a recognised truthy value
  (``1``/``true``/``yes``/``on`` — ``=1`` is the cross-tool
  convention) force consent off and suppress the prompt.
  ``INKFOOT_TELEMETRY=0|1`` is an explicit non-interactive opt-out /
  opt-in for scripted environments where no human is present to answer
  the prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

_LOG = logging.getLogger("inkfoot.telemetry")

# The collector endpoint. Overridable so tests never touch the network
# and self-hosters can point it at their own sink (or a black hole).
_DEFAULT_ENDPOINT = "https://telemetry.inkfoot.dev/v1/install"

# Hard ceiling on the network call. The send runs in a daemon thread so
# this never delays the caller, but a bounded timeout keeps a wedged
# socket from lingering for the life of the process.
_PING_TIMEOUT_S = 2.0

# Exact consent text. Kept as a module constant so the privacy docs and
# the unit test assert against one source of truth.
CONSENT_PROMPT = (
    "Inkfoot can record an anonymous install ping to help us understand "
    "usage. None of your data is sent. Enable? (y/N) "
)

# A callable that takes the rendered payload and the endpoint and is
# responsible for delivery. Injectable for tests; the default fires a
# background thread and returns immediately.
Sender = Callable[[dict, str], None]
# A callable that renders the prompt and returns the user's raw answer.
PromptFn = Callable[[str], str]


def _home() -> Path:
    """Inkfoot's per-user state directory (``~/.inkfoot`` by default).

    Mirrors the storage backend's resolution so telemetry state lives
    alongside the runs database and honours the same ``INKFOOT_HOME``
    override used throughout the test-suite and the docs.
    """
    return Path(os.environ.get("INKFOOT_HOME", Path.home() / ".inkfoot"))


def _state_path() -> Path:
    return _home() / "telemetry.json"


def _env_flag(name: str) -> Optional[bool]:
    """Three-valued read of a boolean-ish environment variable.

    Returns ``True`` / ``False`` for a recognised truthy / falsy value,
    and ``None`` when the variable is unset (so the caller can fall
    through to the persisted decision or the prompt).
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    # An unrecognised value reads as falsy. For the opt-out flags that is
    # conservative (telemetry stays off); for the opt-in flag a typo that
    # errs toward *not* sending is the safe default.
    return False


def _load_state() -> Optional[dict]:
    """Read the persisted consent record, or ``None`` if absent/corrupt."""
    path = _state_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError):
        # A corrupt or unreadable state file is treated as "no decision
        # recorded" rather than an error — the worst case is one extra
        # prompt, which is harmless.
        _LOG.debug("telemetry state unreadable at %s", path, exc_info=True)
        return None


def _save_state(state: dict) -> None:
    """Persist the consent record, swallowing any I/O failure."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write can't leave a truncated
        # JSON file that would re-trigger the prompt.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOG.debug("could not persist telemetry state to %s", path, exc_info=True)


def _is_interactive() -> bool:
    """True only when both stdin and stdout are attached to a TTY.

    The consent prompt must never be shown — and ``input()`` must never
    be called — in a non-interactive context, where it would either
    print noise into logs or block a service waiting on a human who
    will never type.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _build_payload(installation_id: str, version: str) -> dict:
    """Assemble the anonymous install payload.

    The installation id is hashed here — the raw value never appears in
    the returned dict, so it can never leave the machine.
    """
    return {
        "event": "install",
        "installation": hashlib.sha256(installation_id.encode("utf-8")).hexdigest(),
        "inkfoot_version": version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system(),
    }


def _default_sender(payload: dict, endpoint: str) -> None:
    """Fire the ping from a daemon thread and return immediately.

    The caller is never blocked on the network. Delivery failures are
    swallowed: a missing collector, an offline machine, or a proxy that
    rejects the POST must all degrade silently to "no ping".
    """

    def _post() -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": f"inkfoot/{payload.get('inkfoot_version', '')}",
                },
            )
            with urllib.request.urlopen(request, timeout=_PING_TIMEOUT_S) as resp:
                resp.read()
        except Exception:  # noqa: BLE001 - telemetry must never raise
            _LOG.debug("telemetry ping did not deliver", exc_info=True)

    thread = threading.Thread(target=_post, name="inkfoot-telemetry", daemon=True)
    thread.start()


def _endpoint() -> str:
    return os.environ.get("INKFOOT_TELEMETRY_ENDPOINT", _DEFAULT_ENDPOINT).strip() or (
        _DEFAULT_ENDPOINT
    )


def _installation_id(state: Optional[dict]) -> str:
    """Return the local installation id, minting one if needed."""
    if state and isinstance(state.get("installation_id"), str):
        return state["installation_id"]
    return uuid.uuid4().hex


def _ping(version: str, state: Optional[dict], sender: Sender) -> None:
    """Send one ping and record that this version has been pinged.

    The "pinged" marker is written *whether or not* delivery succeeds:
    the contract is one ping attempt per version, and retrying on every
    subsequent ``instrument()`` when the network is down would be worse
    than missing a single data point.
    """
    installation_id = _installation_id(state)
    payload = _build_payload(installation_id, version)
    try:
        sender(payload, _endpoint())
    except Exception:  # noqa: BLE001 - a broken sender must not propagate
        _LOG.debug("telemetry sender raised", exc_info=True)
    _save_state(
        {
            "consent": True,
            "installation_id": installation_id,
            "decided_at": (state or {}).get("decided_at", int(time.time())),
            "last_pinged_version": version,
        }
    )


def _record_denied(state: Optional[dict]) -> None:
    """Persist a 'no' decision so the prompt is never shown again."""
    _save_state(
        {
            "consent": False,
            "installation_id": _installation_id(state),
            "decided_at": (state or {}).get("decided_at", int(time.time())),
            "last_pinged_version": (state or {}).get("last_pinged_version"),
        }
    )


def record_install_and_maybe_ping(
    version: str,
    *,
    prompt_fn: Optional[PromptFn] = None,
    sender: Optional[Sender] = None,
) -> None:
    """Resolve consent and, if granted, send at most one anonymous ping.

    Safe to call on every :func:`inkfoot.instrument`; it is wrapped so
    it can never raise into, or block, the caller. ``prompt_fn`` and
    ``sender`` are injection seams for tests — production uses
    :func:`input` and a background-thread HTTP POST respectively.
    """
    try:
        _resolve_and_maybe_ping(
            version,
            prompt_fn=prompt_fn or input,
            sender=sender or _default_sender,
        )
    except Exception:  # noqa: BLE001 - the whole subsystem is best-effort
        _LOG.debug("telemetry step failed; continuing without it", exc_info=True)


def _resolve_and_maybe_ping(version: str, *, prompt_fn: PromptFn, sender: Sender) -> None:
    # 1. Opt-out conventions win over everything and never prompt.
    if _env_flag("DO_NOT_TRACK") or _env_flag("INKFOOT_DO_NOT_TRACK"):
        return

    state = _load_state()

    # 2. Explicit env override (scripted environments, CI opt-in).
    env_choice = _env_flag("INKFOOT_TELEMETRY")
    if env_choice is False:
        return
    if env_choice is True:
        # Forced on without a prompt; still one ping per version.
        if not state or state.get("last_pinged_version") != version:
            _ping(version, state, sender)
        return

    # 3. A decision already on disk is authoritative.
    if state is not None and "consent" in state:
        if not state.get("consent"):
            return
        if state.get("last_pinged_version") != version:
            _ping(version, state, sender)
        return

    # 4. No decision yet. Only a human at a terminal may be asked;
    #    everywhere else defaults to off and records it so we never
    #    re-evaluate noisily.
    if not _is_interactive():
        _record_denied(state)
        return

    try:
        answer = prompt_fn(CONSENT_PROMPT)
    except (EOFError, KeyboardInterrupt):
        # Treat an aborted prompt as "no" — the conservative default.
        _record_denied(state)
        return

    if str(answer).strip().lower() in {"y", "yes"}:
        _ping(version, state, sender)
    else:
        _record_denied(state)
