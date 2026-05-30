"""Tests for ``inkfoot.instrument()``.

Covers:
- Double-call is a no-op on the second call (idempotent guard).
- SDK detection: with only the fake ``anthropic`` module loaded,
  only ``AnthropicShim`` is installed.
- ``capture_mode`` is propagated and readable via the public accessor.
- Atexit hook is registered exactly once.
- ``shutdown()`` reverses the patch (test convenience) and is itself
  idempotent.
- ``PolicyNotSupported`` is raised at registration before the
  shims install (so a bad policy can't half-instrument the runtime).
"""

from __future__ import annotations

import sys

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot._shim_install import installed_providers
from inkfoot.errors import PolicyNotSupported
from inkfoot.policy import IntegrationPattern, Policy, PolicyDecision
from inkfoot.policy.registry import PolicyRegistry
from inkfoot.storage.sqlite import SQLiteStorage
from tests.unit._fake_sdks import (
    install_fake_anthropic,
    install_fake_openai,
    uninstall_fake_sdks,
)


@pytest.fixture(autouse=True)
def clean_state() -> None:
    """Each test starts + ends with no shims installed, no policies
    registered, and the global instrument flag cleared."""
    inkfoot.instrument.__wrapped__ if hasattr(
        inkfoot.instrument, "__wrapped__"
    ) else None
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()
    yield
    instrument_mod.shutdown()
    PolicyRegistry.clear()
    uninstall_fake_sdks()


def test_double_call_is_idempotent(tmp_path) -> None:
    install_fake_anthropic()
    storage1 = SQLiteStorage(path=tmp_path / "one.db")
    storage2 = SQLiteStorage(path=tmp_path / "two.db")

    inkfoot.instrument(storage=storage1)
    assert instrument_mod.is_instrumented() is True
    providers_after_first = list(installed_providers())

    # Second call with different storage: must be a no-op.
    inkfoot.instrument(storage=storage2)
    providers_after_second = list(installed_providers())
    assert providers_after_first == providers_after_second


def test_only_anthropic_installed_means_only_anthropic_shim(tmp_path) -> None:
    install_fake_anthropic()
    # Deliberately NOT install_fake_openai().
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    inkfoot.instrument(storage=storage)
    assert installed_providers() == ["anthropic"]


def test_both_sdks_installed_installs_both_shims(tmp_path) -> None:
    install_fake_anthropic()
    install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    inkfoot.instrument(storage=storage)
    assert set(installed_providers()) == {"anthropic", "openai"}


def test_neither_sdk_installed_is_silent_no_shims(tmp_path) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    assert installed_providers() == []
    assert instrument_mod.is_instrumented() is True


def test_explicit_sdks_filters_auto_detect(tmp_path) -> None:
    install_fake_anthropic()
    install_fake_openai()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(sdks=["anthropic"], storage=storage)
    assert installed_providers() == ["anthropic"]


def test_capture_mode_propagates(tmp_path) -> None:
    install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage, capture_mode="replay")
    assert instrument_mod.current_capture_mode() == "replay"


def test_capture_mode_invalid_value_raises(tmp_path) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    with pytest.raises(ValueError, match="capture_mode"):
        inkfoot.instrument(storage=storage, capture_mode="bogus")


def test_shutdown_is_idempotent(tmp_path) -> None:
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    instrument_mod.shutdown()
    instrument_mod.shutdown()  # second call must not raise
    assert instrument_mod.is_instrumented() is False


def test_shutdown_without_prior_instrument_is_safe() -> None:
    instrument_mod.shutdown()
    assert instrument_mod.is_instrumented() is False


def test_unsupported_policy_raises_before_shim_install(tmp_path) -> None:
    """A Pattern-C-only policy must raise *before* the shim is
    installed — otherwise the user's SDK calls would be touched
    despite the registration error."""

    class PatternCOnly(Policy):
        NAME = "PatternCOnly"
        SUPPORTED_PATTERNS = {IntegrationPattern.C}

        def before_call(self, ctx):
            return PolicyDecision()

        def after_call(self, ctx, response):
            return None

    install_fake_anthropic()
    storage = SQLiteStorage(path=tmp_path / "runs.db")

    with pytest.raises(PolicyNotSupported, match="PatternCOnly"):
        inkfoot.instrument(
            storage=storage, policies=[PatternCOnly()]
        )

    # Verify the shim was NOT installed despite the storage being
    # already opened.
    assert installed_providers() == []
    assert instrument_mod.is_instrumented() is False


def test_atexit_hook_registered_once(tmp_path, monkeypatch) -> None:
    """The atexit hook should only be registered once across
    multiple instrument()/shutdown() cycles."""
    calls: list = []

    import atexit

    real_register = atexit.register

    def spy_register(fn, *a, **kw):
        calls.append(fn)
        return real_register(fn, *a, **kw)

    monkeypatch.setattr(atexit, "register", spy_register)
    # Force the module to re-evaluate its registration guard by
    # resetting it. (The module-level flag is the production guard.)
    instrument_mod._ATEXIT_REGISTERED = False  # type: ignore[attr-defined]

    storage = SQLiteStorage(path=tmp_path / "runs.db")
    inkfoot.instrument(storage=storage)
    instrument_mod.shutdown()
    inkfoot.instrument(storage=SQLiteStorage(path=tmp_path / "runs2.db"))

    # Despite two instrument() calls, the atexit hook is registered once.
    our_hook_calls = [f for f in calls if f is instrument_mod._atexit_shutdown]
    assert len(our_hook_calls) == 1
