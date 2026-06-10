#!/usr/bin/env python3
"""Guard for the early-access pre-release pipeline (E6-S0 / T1).

The pre-release publish workflow is tag-triggered. Before it builds
and uploads to PyPI we want one cheap, deterministic check that the
git tag and the package's own ``__version__`` agree, and that the
version is an *actual* PEP 440 pre-release (``aN`` / ``bN`` / ``rcN``)
rather than a final release that slipped through the tag filter.

Why a standalone script instead of inline shell in the workflow:

* It is unit-testable (``tests/unit/test_prerelease_tag_guard.py``)
  so the parsing edge cases are covered without spinning up Actions.
* It runs *before* ``pip install build`` in the workflow, so it must
  not import third-party packages — the PEP 440 parse below is a
  deliberately minimal, dependency-free regex rather than
  ``packaging.version``.

Usage::

    python scripts/check_prerelease_tag.py <git-tag>
    python scripts/check_prerelease_tag.py --from-package

``<git-tag>`` is normally ``${{ github.ref_name }}`` (e.g. ``v1.0.0a1``
or ``1.0.0a1``; a single leading ``v`` is tolerated). The
``--from-package`` form validates ``_version.py``'s own version (used by
the ``workflow_dispatch`` path, which has no tag): it text-parses the
version rather than importing ``inkfoot`` — importing the package would
drag in ``tiktoken`` et al, which aren't installed in the guard job.
Exit code 0 means "safe to publish"; any non-zero exit carries a
human-readable reason on stderr and must stop the pipeline.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# PEP 440 (subset): release segment + a *required* pre-release segment.
# We intentionally do not accept ``.devN`` or ``.postN`` on their own —
# early-access releases must carry an explicit a/b/rc marker so a stray
# ``1.0.0`` or ``1.0.0.post1`` tag can never reach PyPI via this path.
_PRERELEASE_RE = re.compile(
    r"""
    ^\s*
    v?                                  # tolerate a single leading 'v'
    (?P<release>[0-9]+(?:\.[0-9]+)*)    # 1 or 1.0 or 1.0.0 ...
    [-_.]?
    (?P<phase>a|b|c|rc|alpha|beta|pre|preview)  # the pre-release marker
    [-_.]?
    (?P<pre_n>[0-9]+)?                  # optional pre-release number
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


class TagGuardError(ValueError):
    """Raised when a tag is not a publishable pre-release."""


def normalise_tag(tag: str) -> str:
    """Strip a single leading ``v`` and surrounding whitespace.

    ``v1.0.0a1`` and ``1.0.0a1`` are treated as the same version. We
    only strip *one* leading ``v`` — ``vv1.0.0a1`` is a typo, not a
    valid tag, and should fail loudly downstream rather than be
    silently coerced.
    """
    stripped = tag.strip()
    if stripped[:1].lower() == "v":
        return stripped[1:]
    return stripped


def is_prerelease(version: str) -> bool:
    """True iff ``version`` is a PEP 440 a/b/rc pre-release.

    A final release (``1.0.0``), a dev-only (``1.0.0.dev1``), or a
    post-release (``1.0.0.post1``) all return False: none of them are
    the early-access shape this pipeline is allowed to publish.
    """
    return _PRERELEASE_RE.match(version) is not None


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
        raise TagGuardError(
            f"could not find __version__ in {version_file}"
        )
    return match.group("v")


def check(tag: str, package_version: str) -> str:
    """Validate ``tag`` against ``package_version``; return the version.

    Raises :class:`TagGuardError` with an actionable message on any of:
    empty tag, tag/version mismatch, or a non-pre-release version.
    """
    if not tag or not tag.strip():
        raise TagGuardError("empty tag: expected something like 'v1.0.0a1'")

    tag_version = normalise_tag(tag)

    if tag_version != package_version:
        raise TagGuardError(
            f"tag/version mismatch: git tag resolves to '{tag_version}' "
            f"but inkfoot/_version.py declares '{package_version}'. "
            f"Bump _version.py to match the tag (or retag) before publishing."
        )

    if not is_prerelease(package_version):
        raise TagGuardError(
            f"'{package_version}' is not a PEP 440 pre-release. "
            f"The early-access pipeline only publishes a/b/rc releases "
            f"(e.g. 1.0.0a1); the public release lives in Phase 3 IN17."
        )

    return package_version


def _default_version_file() -> Path:
    return Path(__file__).resolve().parents[1] / "inkfoot" / "_version.py"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: check_prerelease_tag.py <git-tag>",
            file=sys.stderr,
        )
        return 2

    try:
        package_version = read_package_version(_default_version_file())
        # ``--from-package`` (the workflow_dispatch path) has no tag to
        # check against, so validate the file's own version: the
        # tag==version step is trivially true and the pre-release
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
