"""Markdown link checker for the documentation site.

The docs build already runs ``mkdocs build --strict``, which fails on
links to missing pages. That misses two failure modes that still ship a
404 to a reader: a ``#fragment`` that points at a heading which no
longer exists, and a navigation entry that points at a deleted file.

This module closes both gaps as a fast, build-free test:

* every relative link target resolves to a real file under ``docs/``;
* every ``#fragment`` resolves to a heading anchor (or an explicit
  ``{#id}``) on the target page;
* every ``nav:`` entry in ``mkdocs.yml`` points at a file that exists;
* every page under ``docs/`` is reachable from the nav (no orphans).

External links (anything with a URL scheme) are not fetched — the test
stays offline and deterministic. Links inside fenced or inline code are
ignored, the way a Markdown renderer treats them.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"

# A link destination with an explicit URL scheme (http:, https:,
# mailto:, tel:, …) or a protocol-relative ``//host`` is external and
# never resolved against the filesystem.
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")
# The closing half of a Markdown inline link or image: ``](dest)``.
# Matching only the closing half means a link whose text wraps across
# lines is still caught — the destination is always on one line.
_LINK_CLOSE = re.compile(r"\]\(\s*([^)]+?)\s*\)")
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*$")
_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
# Explicit anchors authors can place: attr_list ``{#id}`` on a heading
# or block, and raw-HTML ``id=``/``name=`` attributes.
_ATTR_ID = re.compile(r"\{:?[^}]*?#([-\w]+)[^}]*\}")
_HTML_ID = re.compile(r"""<[^>]+\b(?:id|name)\s*=\s*["']([-\w]+)["']""")


def _slugify(text: str) -> str:
    """Replicate Python-Markdown's default ``toc`` slugify: drop
    accents, strip everything but word chars / spaces / hyphens,
    lower-case, and collapse runs of spaces and hyphens to one hyphen.
    """
    value = unicodedata.normalize("NFKD", text)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def _heading_anchor(raw: str) -> Tuple[str, Optional[str]]:
    """Return ``(slug, explicit_id)`` for a heading's text. ``slug`` is
    the toc-derived anchor after stripping inline Markdown; ``explicit``
    is a trailing ``{#id}`` when present (which wins over the slug).
    """
    explicit: Optional[str] = None
    attr = re.search(r"\{:?\s*([^}]*)\}\s*$", raw)
    if attr:
        idm = re.search(r"#([-\w]+)", attr.group(1))
        if idm:
            explicit = idm.group(1)
        raw = raw[: attr.start()].rstrip()
    # ``[text](url)`` -> ``text``; ``![alt](url)`` handled by the same
    # capture once the leading ``!`` is dropped.
    raw = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    # Inline-code backticks vanish; ``*``/``~`` emphasis markers are
    # dropped by slugify itself. Underscores are *kept* — they are word
    # characters, so ``threshold_tokens`` slugs with the underscore
    # intact, matching the renderer.
    raw = raw.replace("`", "")
    return _slugify(raw), explicit


def _iter_content_lines(text: str):
    """Yield ``(lineno, line)`` for lines that are *not* inside a fenced
    code block. Fence tracking follows the CommonMark rule: a fence
    closes only on a marker of the same character that is at least as
    long as the opener. Lines are yielded raw — inline-code masking is
    left to callers that want it, so a heading that is itself inline
    code (`` ## `inkfoot report` ``) keeps its text.
    """
    fence: Optional[Tuple[str, int]] = None  # (char, length)
    for lineno, line in enumerate(text.splitlines(), start=1):
        opener = _FENCE_OPEN.match(line)
        if fence is None:
            if opener:
                marker = opener.group(2)
                fence = (marker[0], len(marker))
                continue
            yield lineno, line
        else:
            char, length = fence
            stripped = line.strip()
            if stripped and set(stripped) == {char} and len(stripped) >= length:
                fence = None
            # lines inside a fence are skipped entirely


def _anchors_for(text: str) -> Set[str]:
    """The set of valid in-page anchors: heading slugs (with toc's
    ``_N`` de-duplication), trailing ``{#id}`` ids, and raw-HTML ids.
    """
    anchors: Set[str] = set()
    used: Set[str] = set()

    def _claim(anchor: str) -> str:
        if anchor not in used:
            used.add(anchor)
            return anchor
        i = 1
        while f"{anchor}_{i}" in used:
            i += 1
        unique = f"{anchor}_{i}"
        used.add(unique)
        return unique

    for _lineno, line in _iter_content_lines(text):
        heading = _ATX_HEADING.match(line)
        if heading:
            slug, explicit = _heading_anchor(heading.group(2))
            anchors.add(_claim(explicit if explicit else slug))
        for extra in _ATTR_ID.findall(line):
            anchors.add(extra)
        for extra in _HTML_ID.findall(line):
            anchors.add(extra)
    return anchors


def _links_in(text: str) -> List[Tuple[int, str]]:
    """All link destinations with their line numbers. Inline-code spans
    are blanked first so a literal ``[x](y)`` shown as code isn't read
    as a link.
    """
    out: List[Tuple[int, str]] = []
    for lineno, line in _iter_content_lines(text):
        masked = re.sub(r"`[^`]*`", " ", line)
        for match in _LINK_CLOSE.finditer(masked):
            dest = match.group(1).split()[0]  # drop any ``"title"``
            out.append((lineno, dest))
    return out


def _markdown_files() -> List[Path]:
    return sorted(DOCS_DIR.rglob("*.md"))


def _load_corpus() -> Tuple[Dict[Path, Set[str]], Dict[Path, List[Tuple[int, str]]]]:
    anchors: Dict[Path, Set[str]] = {}
    links: Dict[Path, List[Tuple[int, str]]] = {}
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        anchors[path] = _anchors_for(text)
        links[path] = _links_in(text)
    return anchors, links


class _IgnoreUnknownTags(yaml.SafeLoader):
    """``mkdocs.yml`` carries ``!!python/name:`` tags for superfences;
    resolve them to ``None`` so the nav can be read with safe loading.
    """


_IgnoreUnknownTags.add_multi_constructor(
    "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: None
)
_IgnoreUnknownTags.add_multi_constructor(
    "tag:yaml.org,2002:python/object", lambda loader, suffix, node: None
)


def _nav_pages(nav, out: List[str]) -> None:
    if isinstance(nav, str):
        out.append(nav)
    elif isinstance(nav, list):
        for item in nav:
            _nav_pages(item, out)
    elif isinstance(nav, dict):
        for value in nav.values():
            _nav_pages(value, out)


def _nav_targets() -> List[str]:
    config = yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_IgnoreUnknownTags)
    pages: List[str] = []
    _nav_pages(config.get("nav", []), pages)
    return [p for p in pages if isinstance(p, str)]


def test_internal_links_resolve_to_files_and_anchors() -> None:
    anchors, links = _load_corpus()
    errors: List[str] = []

    for path, dests in links.items():
        rel = path.relative_to(REPO_ROOT)
        for lineno, dest in dests:
            if not dest or dest.startswith("#"):
                # Same-page anchor.
                if dest.startswith("#"):
                    fragment = dest[1:]
                    if fragment and fragment not in anchors[path]:
                        errors.append(f"{rel}:{lineno}: missing anchor '#{fragment}'")
                continue
            if _EXTERNAL.match(dest):
                continue

            path_part, _, fragment = dest.partition("#")
            target = (path.parent / path_part).resolve()
            if not target.exists():
                errors.append(f"{rel}:{lineno}: broken link -> '{dest}' ({target} missing)")
                continue
            if fragment and target.suffix == ".md":
                if fragment not in anchors.get(target, set()):
                    errors.append(
                        f"{rel}:{lineno}: link '{dest}' resolves, but anchor "
                        f"'#{fragment}' is not on {target.relative_to(REPO_ROOT)}"
                    )

    assert not errors, "Broken documentation links:\n" + "\n".join(errors)


def test_nav_targets_exist() -> None:
    missing = [p for p in _nav_targets() if not (DOCS_DIR / p).is_file()]
    assert not missing, "mkdocs.yml nav points at missing files:\n" + "\n".join(missing)


def test_every_page_is_reachable_from_nav() -> None:
    in_nav = {(DOCS_DIR / p).resolve() for p in _nav_targets()}
    orphans = [
        str(p.relative_to(REPO_ROOT))
        for p in _markdown_files()
        if p.resolve() not in in_nav
    ]
    assert not orphans, "Pages not reachable from the nav (orphans):\n" + "\n".join(orphans)


def test_quickstart_leads_with_langchain() -> None:
    """The quickstart's headline path is the LangChain pattern."""
    text = (DOCS_DIR / "quickstart.md").read_text(encoding="utf-8")
    head = text[: text.index("## 4")] if "## 4" in text else text
    assert "inkfoot[langchain]" in head, "Quickstart should install the langchain extra up front"
    assert "langchain" in head.lower()
    # The raw-SDK shape is offered as the alternative, not the headline.
    assert "frameworks/raw-sdk.md" in text
