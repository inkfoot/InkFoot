"""Unit tests for the opt-in install telemetry.

The telemetry module has one job and a long list of things it must
never do: never send without consent, never prompt outside a terminal,
never block, never raise, never duplicate a ping, and never put
anything reversible to the user on the wire. Each of those is pinned
below.

Isolation: ``INKFOOT_HOME`` is redirected to a per-test ``tmp_path`` and
every telemetry-related environment variable is cleared, so the suite
never reads or writes a real ``~/.inkfoot`` and never touches the
network (a recording sender is injected in place of the HTTP POST).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from inkfoot import _telemetry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFOOT_HOME", str(tmp_path))
    for var in (
        "DO_NOT_TRACK",
        "INKFOOT_DO_NOT_TRACK",
        "INKFOOT_TELEMETRY",
        "INKFOOT_TELEMETRY_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class RecordingSender:
    """A stand-in for the real HTTP sender that records calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, payload, endpoint):
        self.calls.append((payload, endpoint))


def _no_prompt(_text):
    raise AssertionError("prompt must not be shown in this scenario")


def _read_state(home: Path) -> dict:
    return json.loads((home / "telemetry.json").read_text(encoding="utf-8"))


def _force_interactive(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(_telemetry, "_is_interactive", lambda: value)


# --- default-off / non-interactive ---------------------------------------


def test_non_interactive_no_decision_defaults_to_denied(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, False)
    sender = RecordingSender()

    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=sender
    )

    assert sender.calls == []
    assert _read_state(isolated_home)["consent"] is False


def test_non_interactive_never_prompts(isolated_home, monkeypatch):
    # The prompt callable raises if touched; reaching here proves it
    # was not called in a non-interactive context.
    _force_interactive(monkeypatch, False)
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=RecordingSender()
    )


# --- interactive consent -------------------------------------------------


def test_interactive_yes_sends_one_ping(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()

    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )

    assert len(sender.calls) == 1
    payload, _endpoint = sender.calls[0]
    assert payload["event"] == "install"
    state = _read_state(isolated_home)
    assert state["consent"] is True
    assert state["last_pinged_version"] == "1.0.0"


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "  y  "])
def test_affirmative_answers_accepted(isolated_home, monkeypatch, answer):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: answer, sender=sender
    )
    assert len(sender.calls) == 1


@pytest.mark.parametrize("answer", ["", "n", "no", "nope", "0", "q"])
def test_negative_or_blank_answers_deny(isolated_home, monkeypatch, answer):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: answer, sender=sender
    )
    assert sender.calls == []
    assert _read_state(isolated_home)["consent"] is False


def test_aborted_prompt_is_treated_as_no(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()

    def _abort(_text):
        raise EOFError

    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_abort, sender=sender
    )
    assert sender.calls == []
    assert _read_state(isolated_home)["consent"] is False


# --- one ping per (installation, version) --------------------------------


def test_second_call_same_version_does_not_re_ping(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    # Second call: the decision is on disk now, so neither prompt nor a
    # duplicate ping should happen.
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=sender
    )
    assert len(sender.calls) == 1


def test_new_version_pings_again_without_reprompt(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    # A version bump re-pings, but consent is already recorded so the
    # user is not asked twice.
    _telemetry.record_install_and_maybe_ping(
        "1.1.0", prompt_fn=_no_prompt, sender=sender
    )
    assert len(sender.calls) == 2
    assert _read_state(isolated_home)["last_pinged_version"] == "1.1.0"


def test_recorded_no_is_honoured_forever(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "n", sender=sender
    )
    # Even a version bump must not re-prompt a user who already said no.
    _telemetry.record_install_and_maybe_ping(
        "2.0.0", prompt_fn=_no_prompt, sender=sender
    )
    assert sender.calls == []


# --- environment overrides -----------------------------------------------


@pytest.mark.parametrize("var", ["DO_NOT_TRACK", "INKFOOT_DO_NOT_TRACK"])
def test_do_not_track_forces_off_without_prompt(isolated_home, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    _force_interactive(monkeypatch, True)  # even at a terminal: no prompt
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=sender
    )
    assert sender.calls == []


def test_env_opt_in_pings_without_prompt(isolated_home, monkeypatch):
    monkeypatch.setenv("INKFOOT_TELEMETRY", "1")
    _force_interactive(monkeypatch, False)  # scripted / CI environment
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=sender
    )
    assert len(sender.calls) == 1


def test_env_opt_out_suppresses_prompt_and_ping(isolated_home, monkeypatch):
    monkeypatch.setenv("INKFOOT_TELEMETRY", "0")
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=_no_prompt, sender=sender
    )
    assert sender.calls == []


def test_env_opt_in_still_one_ping_per_version(isolated_home, monkeypatch):
    monkeypatch.setenv("INKFOOT_TELEMETRY", "1")
    _force_interactive(monkeypatch, False)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping("1.0.0", sender=sender)
    _telemetry.record_install_and_maybe_ping("1.0.0", sender=sender)
    assert len(sender.calls) == 1


# --- payload is anonymous ------------------------------------------------


def test_payload_carries_only_documented_fields(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.2.3", prompt_fn=lambda _t: "y", sender=sender
    )
    payload, _endpoint = sender.calls[0]
    assert set(payload) == {"event", "installation", "inkfoot_version", "python", "os"}
    assert payload["inkfoot_version"] == "1.2.3"


def test_installation_id_is_hashed_not_raw(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    payload, _endpoint = sender.calls[0]
    raw_id = _read_state(isolated_home)["installation_id"]

    # The raw id stays on disk; only its SHA-256 leaves the machine.
    assert payload["installation"] != raw_id
    assert payload["installation"] == hashlib.sha256(raw_id.encode()).hexdigest()
    assert len(payload["installation"]) == 64


def test_payload_has_no_host_or_user_identifiers(isolated_home, monkeypatch):
    import getpass
    import socket

    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    payload, _endpoint = sender.calls[0]
    serialised = json.dumps(payload)
    for leak in (socket.gethostname(), getpass.getuser(), str(isolated_home)):
        if leak:
            assert leak not in serialised


def test_installation_id_stable_across_versions(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    _telemetry.record_install_and_maybe_ping(
        "1.1.0", prompt_fn=_no_prompt, sender=sender
    )
    first, second = sender.calls[0][0], sender.calls[1][0]
    assert first["installation"] == second["installation"]


# --- robustness: never raise, never block --------------------------------


def test_sender_failure_is_swallowed(isolated_home, monkeypatch):
    _force_interactive(monkeypatch, True)

    def _boom(_payload, _endpoint):
        raise RuntimeError("collector down")

    # Must not propagate, and must still record the decision so we
    # don't re-prompt on the next run.
    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=_boom
    )
    assert _read_state(isolated_home)["last_pinged_version"] == "1.0.0"


def test_corrupt_state_is_treated_as_undecided(isolated_home, monkeypatch):
    (isolated_home / "telemetry.json").write_text("{not json", encoding="utf-8")
    _force_interactive(monkeypatch, False)
    sender = RecordingSender()
    # Should not raise; falls back to the default-off path.
    _telemetry.record_install_and_maybe_ping("1.0.0", sender=sender)
    assert sender.calls == []


def test_unwritable_home_does_not_raise(tmp_path, monkeypatch):
    # Point INKFOOT_HOME at a path whose parent is a regular file, so
    # mkdir fails. The decision can't be persisted, but instrument()
    # must not be able to crash because of it.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("INKFOOT_HOME", str(blocker / "nested"))
    for var in ("DO_NOT_TRACK", "INKFOOT_DO_NOT_TRACK", "INKFOOT_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    _force_interactive(monkeypatch, True)
    sender = RecordingSender()

    _telemetry.record_install_and_maybe_ping(
        "1.0.0", prompt_fn=lambda _t: "y", sender=sender
    )
    # Consent was given, so the ping is attempted even though state
    # could not be written.
    assert len(sender.calls) == 1


def test_default_sender_returns_immediately_and_never_raises(monkeypatch):
    # Point at a closed local port; the daemon thread will fail to
    # connect and swallow it. The call itself must return without
    # raising and without waiting on the socket.
    payload = {"event": "install", "inkfoot_version": "1.0.0"}
    _telemetry._default_sender(payload, "http://127.0.0.1:9/")


# --- consent text is the published text ----------------------------------


def test_consent_prompt_matches_published_wording():
    assert "None of your data is sent." in _telemetry.CONSENT_PROMPT
    assert "Enable? (y/N)" in _telemetry.CONSENT_PROMPT


# --- wiring: instrument() invokes telemetry exactly once -----------------


def test_instrument_invokes_telemetry_once(tmp_path, monkeypatch):
    import inkfoot
    import inkfoot._instrument as instrument_mod
    from inkfoot.storage.sqlite import SQLiteStorage

    calls = []
    monkeypatch.setattr(
        _telemetry,
        "record_install_and_maybe_ping",
        lambda version, **kw: calls.append(version),
    )

    instrument_mod.shutdown()
    try:
        inkfoot.instrument(storage=SQLiteStorage(path=tmp_path / "runs.db"))
        # Second call is the idempotent no-op and must not re-fire it.
        inkfoot.instrument(storage=SQLiteStorage(path=tmp_path / "other.db"))
    finally:
        instrument_mod.shutdown()

    assert calls == [inkfoot.__version__]
