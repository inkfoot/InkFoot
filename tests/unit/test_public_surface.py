"""Tests for the frozen public import surface.

The contract: ``from inkfoot import *`` exposes exactly the names in
``inkfoot.__all__`` — no underscore-prefixed modules leak, no module
attributes from imported deps bleed through, and every documented
callable exists (even if it raises ``NotImplementedError`` because it
ships in a later release).
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
        "checkpoint",  # public helper
        "set_outcome",
        "tag",
        "tag_node",  # public helper
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
    "callable_name,expected_module",
    [
        ("instrument", "inkfoot._instrument"),
        ("agent_run", "inkfoot._run_lifecycle"),
        ("set_outcome", "inkfoot._run_lifecycle"),
        ("tag", "inkfoot._run_lifecycle"),
        ("tag_retrieval", "inkfoot._run_lifecycle"),
        ("tag_node", "inkfoot._run_lifecycle"),
        ("checkpoint", "inkfoot._run_lifecycle"),
        ("report_cost", "inkfoot._run_lifecycle"),
    ],
)
def test_all_public_callables_resolve_to_real_implementations(
    callable_name: str, expected_module: str
) -> None:
    """Every name in ``__all__`` that's a callable now points at a
    real implementation (no more NotImplementedError stubs after
    )."""
    fn = getattr(inkfoot, callable_name)
    assert callable(fn)
    assert fn.__module__ == expected_module
    assert "NotImplementedError" not in (fn.__doc__ or "")


def test_module_reimports_cleanly() -> None:
    # Importing twice should not change the surface.
    before = set(inkfoot.__all__)
    importlib.reload(inkfoot)
    after = set(inkfoot.__all__)
    assert before == after
