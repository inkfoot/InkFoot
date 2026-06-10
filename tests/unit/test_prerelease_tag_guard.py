"""Unit tests for the early-access pre-release tag guard (E6-S0 / T1).

``scripts/check_prerelease_tag.py`` is the authoritative gate the
publish workflow runs before it builds and uploads to PyPI. These
tests pin its parsing behaviour so a future edit can't silently let a
final release — or a tag that disagrees with ``_version.py`` — reach
PyPI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_prerelease_tag.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_prerelease_tag", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


guard = _load_module()


# --- normalise_tag -------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("v1.0.0a1", "1.0.0a1"),
        ("1.0.0a1", "1.0.0a1"),
        ("  v1.0.0a1  ", "1.0.0a1"),
        ("V1.0.0a1", "1.0.0a1"),  # capital V tolerated
    ],
)
def test_normalise_strips_single_leading_v(raw, expected):
    assert guard.normalise_tag(raw) == expected


def test_normalise_strips_only_one_v():
    # A double-v is a typo, not a valid tag: only one 'v' comes off so
    # the residual mismatch trips the version check downstream.
    assert guard.normalise_tag("vv1.0.0a1") == "v1.0.0a1"


# --- is_prerelease -------------------------------------------------------

@pytest.mark.parametrize(
    "version",
    [
        "1.0.0a1",
        "1.0.0b2",
        "1.0.0rc1",
        "1.0.0a0",
        "0.1.0a0",
        "1.0.0alpha1",
        "1.0.0.beta.3",
        "1.0a1",  # two-segment release is still valid PEP 440
        "2.0.0rc10",
    ],
)
def test_prerelease_versions_accepted(version):
    assert guard.is_prerelease(version) is True


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0",          # final release — public launch is Phase 3 IN17
        "1.0.0.post1",    # post-release is not early-access
        "1.0.0.dev1",     # dev-only carries no a/b/rc marker
        "1.0",            # final, short form
        "not-a-version",
        "",
        "abc",
    ],
)
def test_non_prerelease_versions_rejected(version):
    assert guard.is_prerelease(version) is False


# --- check ---------------------------------------------------------------

def test_check_accepts_matching_prerelease():
    assert guard.check("v1.0.0a1", "1.0.0a1") == "1.0.0a1"


def test_check_accepts_tag_without_v_prefix():
    assert guard.check("1.0.0a1", "1.0.0a1") == "1.0.0a1"


def test_check_rejects_empty_tag():
    with pytest.raises(guard.TagGuardError, match="empty tag"):
        guard.check("", "1.0.0a1")


def test_check_rejects_whitespace_only_tag():
    with pytest.raises(guard.TagGuardError, match="empty tag"):
        guard.check("   ", "1.0.0a1")


def test_check_rejects_tag_version_mismatch():
    with pytest.raises(guard.TagGuardError, match="mismatch"):
        guard.check("v1.0.0a2", "1.0.0a1")


def test_check_rejects_final_release_even_when_tag_matches():
    # Tag and version agree, but 1.0.0 is not a pre-release: the public
    # release path is owned by Phase 3 IN17, not this pipeline.
    with pytest.raises(guard.TagGuardError, match="not a PEP 440 pre-release"):
        guard.check("v1.0.0", "1.0.0")


# --- read_package_version ------------------------------------------------

def test_read_package_version_matches_shipped_value(tmp_path):
    version_file = REPO_ROOT / "inkfoot" / "_version.py"
    from inkfoot._version import __version__

    assert guard.read_package_version(version_file) == __version__


def test_read_package_version_raises_when_missing(tmp_path):
    bad = tmp_path / "_version.py"
    bad.write_text("# no version here\n", encoding="utf-8")
    with pytest.raises(guard.TagGuardError, match="could not find __version__"):
        guard.read_package_version(bad)


# --- main (exit codes) ---------------------------------------------------

def test_main_wrong_arg_count_returns_2():
    assert guard.main([]) == 2
    assert guard.main(["a", "b"]) == 2


def test_main_mismatched_tag_returns_1(capsys):
    # The shipped _version.py is a pre-release, so a deliberately wrong
    # tag exercises the mismatch path through real file reads.
    rc = guard.main(["v9.9.9a9"])
    assert rc == 1
    assert "mismatch" in capsys.readouterr().err


def test_main_happy_path_prints_version(capsys):
    from inkfoot._version import __version__

    rc = guard.main([__version__])
    assert rc == 0
    assert capsys.readouterr().out.strip() == __version__


def test_main_from_package_validates_shipped_version(capsys):
    # The workflow_dispatch path: no tag, so the guard validates
    # _version.py's own version. It must print exactly that version.
    from inkfoot._version import __version__

    rc = guard.main(["--from-package"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == __version__


def test_main_from_package_does_not_import_inkfoot():
    # Regression for the guard job crash: --from-package must read the
    # version by text-parsing the file, never importing the package
    # (which would require tiktoken, absent in the guard job). We prove
    # it by running the script in a subprocess with import of `inkfoot`
    # sabotaged — if it tried to import the package, it would fail.
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['inkfoot'] = None; "
        "import runpy; sys.argv = ['x', '--from-package']; "
        "runpy.run_path(%r, run_name='__main__')" % str(_SCRIPT)
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # Sabotaging `inkfoot` would surface as an ImportError if the script
    # touched it; a clean exit 0 proves the text-parse path.
    assert result.returncode == 0, result.stderr
    from inkfoot._version import __version__

    assert result.stdout.strip() == __version__
