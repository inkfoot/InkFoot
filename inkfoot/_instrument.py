"""``inkfoot.instrument()`` — Phase 0 entry point.

Contract per ``phase-0-classify.md`` §5.1:

1. Detect installed SDKs (or use the explicit list).
2. For each detected SDK, install its shim (monkey-patch).
   Already-installed shims are a no-op.
3. Resolve storage (default: ``SQLiteStorage`` at ``~/.inkfoot/runs.db``).
4. Validate every policy against the active integration pattern's
   capability matrix; raise ``PolicyNotSupported`` on mismatch.
5. Register policies in the global ``PolicyRegistry``.
6. Start the ``AggregatorWorker`` thread.
7. Install ``atexit`` hook to flush the aggregator + close the DB.

**Idempotent**: calling ``instrument()`` twice in the same process
is a no-op on the second call — the module-level
``_INSTRUMENTED`` flag guards every step.

**Fail-loud-at-registration**: registering a Pattern-C-only policy
on Pattern A raises :class:`~inkfoot.errors.PolicyNotSupported`
with a remediation hint pointing at the docs URL (ADR-0-2).
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from inkfoot.policy import Policy
    from inkfoot.storage import Storage
    from inkfoot.storage.aggregator import AggregatorWorker


_LOG = logging.getLogger("inkfoot.instrument")


# Idempotence guard. Touched only inside the module under
# ``_INSTRUMENT_LOCK``.
_INSTRUMENTED = False
_INSTRUMENT_LOCK = threading.Lock()

# Replay/metadata flag, read by the shim emit path on every call.
_CAPTURE_MODE = "metadata"

# References to objects we need to tear down at exit.
_STORAGE: Optional["Storage"] = None
_WORKER: Optional["AggregatorWorker"] = None
_ATEXIT_REGISTERED = False


def _capture_mode_getter() -> str:
    """Module-private accessor the shims read on every call. Lets
    a test flip the mode without going through :func:`instrument`
    twice (which would no-op)."""
    return _CAPTURE_MODE


def instrument(
    sdks: Optional[list[str]] = None,
    policies: Optional[list["Policy"]] = None,
    storage: Optional["Storage"] = None,
    log_level: str = "WARNING",
    capture_mode: str = "metadata",
) -> None:
    """Install Pattern A monkey-patches for the detected SDKs, start
    the aggregator, and register the supplied policies.

    Calling this function twice in the same process is a no-op on
    the second call (idempotent). Subsequent calls do **not** add
    new policies; clear via :func:`shutdown` first if you need to
    re-instrument with a different policy set.
    """
    global _INSTRUMENTED, _CAPTURE_MODE, _STORAGE, _WORKER

    if capture_mode not in {"metadata", "replay"}:
        raise ValueError(
            f"capture_mode must be 'metadata' or 'replay', not {capture_mode!r}"
        )

    with _INSTRUMENT_LOCK:
        if _INSTRUMENTED:
            _LOG.debug("inkfoot.instrument() already active; second call is a no-op")
            return

        # Set the capture mode *before* installing shims so any call
        # that races through right at install time sees the right flag.
        _CAPTURE_MODE = capture_mode

        # Resolve storage.
        if storage is None:
            from inkfoot.storage.sqlite import SQLiteStorage  # noqa: PLC0415

            storage = SQLiteStorage()
        storage.connect()
        _STORAGE = storage

        # Configure logger level for the inkfoot tree.
        logging.getLogger("inkfoot").setLevel(log_level.upper())

        # Register policies — validate the capability matrix first.
        # We do this BEFORE installing the shims so a bad policy
        # registration leaves the user's SDK calls untouched (no
        # half-shimmed state).
        from inkfoot.policy import (  # noqa: PLC0415
            IntegrationPattern,
            register_policies,
        )

        if policies:
            register_policies(policies, active_pattern=IntegrationPattern.A)

        # Install the SDK shims.
        from inkfoot._shim_install import install_shims  # noqa: PLC0415

        install_shims(
            storage=storage,
            capture_mode_getter=_capture_mode_getter,
            sdks=sdks,
        )

        # Start the aggregator.
        from inkfoot.storage.aggregator import AggregatorWorker  # noqa: PLC0415

        worker = AggregatorWorker(storage)
        worker.start()
        _WORKER = worker

        _register_atexit_hook_once()
        _INSTRUMENTED = True


def shutdown() -> None:
    """Reverse of :func:`instrument`. Stops the aggregator, removes
    the shims, closes storage, and clears the policy registry.

    Test-friendly: idempotent, safe to call before or after
    :func:`instrument`. Production code never has to call this —
    the atexit hook does it on process exit.
    """
    global _INSTRUMENTED, _STORAGE, _WORKER, _CAPTURE_MODE
    with _INSTRUMENT_LOCK:
        if _WORKER is not None:
            try:
                _WORKER.stop()
            except Exception:  # pylint: disable=broad-except
                _LOG.warning("aggregator stop raised", exc_info=True)
            _WORKER = None

        try:
            from inkfoot._shim_install import uninstall_shims  # noqa: PLC0415

            uninstall_shims()
        except Exception:  # pylint: disable=broad-except
            _LOG.warning("shim uninstall raised", exc_info=True)

        try:
            from inkfoot.policy.registry import PolicyRegistry  # noqa: PLC0415

            PolicyRegistry.clear()
        except Exception:  # pylint: disable=broad-except
            _LOG.warning("policy registry clear raised", exc_info=True)

        if _STORAGE is not None:
            try:
                _STORAGE.close()
            except Exception:  # pylint: disable=broad-except
                _LOG.warning("storage close raised", exc_info=True)
            _STORAGE = None

        _CAPTURE_MODE = "metadata"
        _INSTRUMENTED = False


def is_instrumented() -> bool:
    """Public accessor for tests + diagnostics."""
    return _INSTRUMENTED


def current_capture_mode() -> str:
    """Public accessor for tests + diagnostics."""
    return _CAPTURE_MODE


def _register_atexit_hook_once() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(_atexit_shutdown)
    _ATEXIT_REGISTERED = True


def _atexit_shutdown() -> None:
    """Called by the interpreter at process exit; safely-isolated."""
    try:
        shutdown()
    except Exception:  # pylint: disable=broad-except  # pragma: no cover
        _LOG.warning("atexit shutdown raised", exc_info=True)
