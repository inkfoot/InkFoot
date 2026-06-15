#!/usr/bin/env python3
"""Guard for the public release pipeline.

The release publish workflow is tag-triggered. Before it builds and
uploads to PyPI we want one cheap, deterministic check that the git tag
and the package's own ``__version__`` agree, and that the version is an
*actual* final release — not a pre-release (``aN`` / ``bN`` / ``rcN``),
a dev build (``.devN``), or a post-release (``.postN``) that slipped
through the tag filter.

Why a standalone script instead of inline shell in the workflow:

* It is unit-testable (``tests/unit/test_release_tag_guard.py``) so the
  parsing edge cases are covered without spinning up Actions.
* It runs *before* ``pip install build`` in the workflow, so it must
  not import third-party packages — the version parse below is a
  deliberately minimal, dependency-free regex rather than
  ``packaging.version``.

Usage::

    python scripts/check_release_tag.py <git-tag>
    python scripts/check_release_tag.py --from-package

``<git-tag>`` is normally ``${{ github.ref_name }}`` (e.g. ``v1.0.0``
or ``1.0.0``; a single leading ``v`` is tolerated). The
``--from-package`` form validates ``_version.py``'s own version (used
by the ``workflow_dispatch`` path, which has no tag): it text-parses
the version rather than importing ``inkfoot`` — importing the package
would drag in ``tiktoken`` et al, which aren't installed in the guard
job. Exit code 0 means "safe to publish"; any non-zero exit carries a
human-readable reason on stderr and must stop the pipeline.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A final release is a bare release segment and nothing else: one or
# more dot-separated integers, with no pre-release (a/b/rc), dev, or
# post suffix. We anchor both ends so ``1.0.0a1``, ``1.0.0.post1``, and
# ``1.0.0.dev1`` all fail.
_FINAL_RELEASE_RE = re.compile(
    r"""
    ^\s*
    v?                                  # tolerate a single leading 'v'
    [0-9]+(?:\.[0-9]+)*                 # 1 or 1.0 or 1.0.0 ...
    \s*$
    """,
    re.VERBOSE,
)


class TagGuardError(ValueError):
    """Raised when a tag is not a publishable final release."""


def normalise_tag(tag: str) -> str:
    """Strip a single leading ``v`` and surrounding whitespace.

    ``v1.0.0`` and ``1.0.0`` are treated as the same version. We only
    strip *one* leading ``v`` — ``vv1.0.0`` is a typo, not a valid tag,
    and should fail loudly downstream rather than be silently coerced.
    """
    stripped = tag.strip()
    if stripped[:1].lower() == "v":
        return stripped[1:]
    return stripped


def is_final_release(version: str) -> bool:
    """True iff ``version`` is a bare final release.

    A pre-release (``1.0.0a1``), a dev build (``1.0.0.dev1``), and a
    post-release (``1.0.0.post1``) all return False: none of them is
    the shape the public release pipeline is allowed to publish.
    """
    return _FINAL_RELEASE_RE.match(version) is not None


def read_package_version(version_file: Path) -> str:
    """Extract ``__version__`` from ``inkfoot/_version.py`` by parsing.

    We read the literal rather than importing the package: the guard
    runs in a bare checkout before ``pip install`` and importing
    ``inkfoot`` would drag in the full dependency tree (tiktoken et al)
    for no benefit.
    """
    text = version_file.read_text(encoding="utf-8")
    match = re.search(
        r"""^__version__\s*=\s*["'](?P<v>[^"']+)["']""",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise TagGuardError(f"could not find __version__ in {version_file}")
    return match.group("v")


def check(tag: str, package_version: str) -> str:
    """Validate ``tag`` against ``package_version``; return the version.

    Raises :class:`TagGuardError` with an actionable message on any of:
    empty tag, tag/version mismatch, or a non-final version.
    """
    if not tag or not tag.strip():
        raise TagGuardError("empty tag: expected something like 'v1.0.0'")

    tag_version = normalise_tag(tag)

    if tag_version != package_version:
        raise TagGuardError(
            f"tag/version mismatch: git tag resolves to '{tag_version}' "
            f"but inkfoot/_version.py declares '{package_version}'. "
            f"Bump _version.py to match the tag (or retag) before publishing."
        )

    if not is_final_release(package_version):
        raise TagGuardError(
            f"'{package_version}' is not a final release. The release "
            f"pipeline only publishes final versions (e.g. 1.0.0); "
            f"pre-release, dev, and post tags are rejected."
        )

    return package_version


def _default_version_file() -> Path:
    return Path(__file__).resolve().parents[1] / "inkfoot" / "_version.py"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_release_tag.py <git-tag>", file=sys.stderr)
        return 2

    try:
        package_version = read_package_version(_default_version_file())
        # ``--from-package`` (the workflow_dispatch path) has no tag to
        # check against, so validate the file's own version: the
        # tag==version step is trivially true and the final-release
        # assertion still applies.
        tag = package_version if args[0] == "--from-package" else args[0]
        version = check(tag, package_version)
    except TagGuardError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    print(version)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
