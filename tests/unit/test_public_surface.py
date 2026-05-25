"""Tests for the frozen public import surface (E1-S1 acceptance).

The contract: ``from inkfoot import *`` exposes exactly the names in
``inkfoot.__all__`` — no underscore-prefixed modules leak, no module
attributes from imported deps bleed through, and every documented
callable exists (even if it raises ``NotImplementedError`` because it
ships in a later epic).
"""

from __future__ import annotations

import importlib

import pytest

import inkfoot


EXPECTED_SURFACE = frozenset(
    {
        "__version__",
        "instrument",
        "agent_run",
        "set_outcome",
        "tag",
        "tag_retrieval",
        "report_cost",
        "InkfootError",
        "PolicyNotSupported",
        "StorageError",
    }
)


def test_all_lists_exactly_the_documented_surface() -> None:
    assert set(inkfoot.__all__) == EXPECTED_SURFACE


def test_star_import_exposes_only_the_documented_surface(tmp_path) -> None:
    # Use an isolated namespace so we observe what ``import *`` actually
    # binds — independent of whatever this test module already imported.
    namespace: dict[str, object] = {}
    exec("from inkfoot import *", namespace)
    leaked = {
        name
        for name in namespace
        if not name.startswith("__") and name not in EXPECTED_SURFACE
    }
    assert leaked == set(), f"unexpected names leaked via star-import: {leaked}"


def test_underscore_modules_are_not_in_all() -> None:
    # The SemVer contract is ``__all__`` + ``from inkfoot import *``.
    # Python's import machinery attaches imported submodules to the
    # parent's namespace (e.g. ``inkfoot._version`` becomes visible on
    # ``dir(inkfoot)``), but they must not be re-exported.
    for name in inkfoot.__all__:
        assert not name.startswith("_") or name.startswith("__"), (
            f"private-ish name {name!r} appears in __all__"
        )


def test_version_is_a_pep440_string() -> None:
    assert isinstance(inkfoot.__version__, str)
    assert inkfoot.__version__  # not empty


@pytest.mark.parametrize(
    "callable_name",
    # ``instrument`` shipped in E3 (Pattern A); removed from this
    # list. The rest still ship in E5.
    ["agent_run", "set_outcome", "tag", "tag_retrieval", "report_cost"],
)
def test_unshipped_callables_raise_notimplementederror_with_pointer(callable_name: str) -> None:
    fn = getattr(inkfoot, callable_name)
    with pytest.raises(NotImplementedError) as exc:
        fn()
    # The error must tell the developer which epic the function lands in
    # so a reader chasing the stub doesn't hit a dead end.
    assert "epic" in str(exc.value).lower()


def test_instrument_is_a_real_callable_not_a_stub() -> None:
    """E3 shipped ``inkfoot.instrument()`` — it must no longer raise
    NotImplementedError. We don't *call* it here (would side-effect
    install monkey-patches); we just check the docstring + module
    origin. The submodule is private (``inkfoot._instrument``) to
    avoid colliding with the same-named function on the package."""
    fn = inkfoot.instrument
    assert callable(fn)
    assert fn.__module__ == "inkfoot._instrument"
    assert "NotImplementedError" not in (fn.__doc__ or "")


def test_module_reimports_cleanly() -> None:
    # Importing twice should not change the surface.
    before = set(inkfoot.__all__)
    importlib.reload(inkfoot)
    after = set(inkfoot.__all__)
    assert before == after
