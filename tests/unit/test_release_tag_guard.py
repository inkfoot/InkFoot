"""Unit tests for the release tag guard.

``scripts/check_release_tag.py`` is the authoritative gate the publish
workflow runs before it builds and uploads to PyPI. These tests pin its
parsing behaviour so a future edit can't silently let a pre-release —
or a tag that disagrees with ``_version.py`` — reach PyPI as if it were
a final release.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_release_tag.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_release_tag", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


guard = _load_module()


# --- normalise_tag -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("v1.0.0", "1.0.0"),
        ("1.0.0", "1.0.0"),
        ("  v1.0.0  ", "1.0.0"),
        ("V1.0.0", "1.0.0"),  # capital V tolerated
    ],
)
def test_normalise_strips_single_leading_v(raw, expected):
    assert guard.normalise_tag(raw) == expected


def test_normalise_strips_only_one_v():
    # A double-v is a typo, not a valid tag: only one 'v' comes off so
    # the residual mismatch trips the version check downstream.
    assert guard.normalise_tag("vv1.0.0") == "v1.0.0"


# --- is_final_release ----------------------------------------------------


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0",
        "0.1.0",
        "2.0.0",
        "1.0",  # two-segment release is still valid
        "10.20.30",
        "v1.0.0",  # a leading v is tolerated by the regex
    ],
)
def test_final_versions_accepted(version):
    assert guard.is_final_release(version) is True


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0a1",  # pre-release
        "1.0.0b2",
        "1.0.0rc1",
        "1.0.0.post1",  # post-release
        "1.0.0.dev1",  # dev build
        "1.0.0alpha1",
        "not-a-version",
        "",
        "abc",
    ],
)
def test_non_final_versions_rejected(version):
    assert guard.is_final_release(version) is False


# --- check ---------------------------------------------------------------


def test_check_accepts_matching_final_tag():
    assert guard.check("v1.0.0", "1.0.0") == "1.0.0"


def test_check_rejects_empty_tag():
    with pytest.raises(guard.TagGuardError):
        guard.check("", "1.0.0")


def test_check_rejects_tag_version_mismatch():
    with pytest.raises(guard.TagGuardError) as excinfo:
        guard.check("v1.0.1", "1.0.0")
    assert "mismatch" in str(excinfo.value)


def test_check_rejects_prerelease_even_when_tag_matches():
    # The tag and _version.py agree, but the version is a pre-release —
    # the public pipeline must refuse it.
    with pytest.raises(guard.TagGuardError) as excinfo:
        guard.check("v1.0.0a1", "1.0.0a1")
    assert "final release" in str(excinfo.value)


# --- read_package_version + main -----------------------------------------


def test_read_package_version_parses_version_file(tmp_path):
    vf = tmp_path / "_version.py"
    vf.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    assert guard.read_package_version(vf) == "1.2.3"


def test_main_from_package_succeeds_on_repo_version(capsys):
    # The shipped _version.py must itself be a publishable final
    # release, so --from-package exits 0 and echoes the version.
    rc = guard.main(["--from-package"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert guard.is_final_release(out)


def test_main_rejects_bad_arity():
    assert guard.main([]) == 2
    assert guard.main(["a", "b"]) == 2
