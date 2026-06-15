"""Hygiene gate for the public open-source surface.

A public repository needs a predictable set of community files: a
license, a contributing guide, a security policy, a changelog, and the
issue / pull-request templates GitHub renders. These tests assert those
files are present and well-formed so a regression — a deleted template,
an issue form that won't render, a planning identifier or customer name
leaking into a public file — fails CI instead of surfacing on the public
repo.

YAML parsing relies on ``pyyaml``, which is a runtime dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

# Files the public repo must ship. The Code of Conduct lives at the repo
# root so GitHub auto-detects it and the root-relative link in
# CONTRIBUTING.md resolves.
REQUIRED_FILES = [
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
]

# The five issue forms, including the two coverage-gap templates that are
# the most common "Inkfoot didn't capture my call" reports.
REQUIRED_ISSUE_FORMS = [
    "bug_report.yml",
    "feature_request.yml",
    "smell_rule_proposal.yml",
    "provider_coverage_gap.yml",
    "langchain_integration_gap.yml",
]

_VALID_ELEMENT_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}

# Internal planning identifiers and customer names that must never appear
# in a public-facing file. Word-anchored so ordinary prose ("history",
# "the longer story of how") doesn't trip them.
_FORBIDDEN = re.compile(
    r"\bdbt\b"  # customer name
    r"|\bE\d+-S\d+\b"  # epic/story ids, e.g. E9-S2
    r"|\bIN1\d\b"  # architecture IN-refs, e.g. IN16/IN17
    r"|\bADR[- ]\d"  # ADR references
    r"|\bepics?\b"
    r"|\bphase[ -]?\d"  # "phase 3", "phase-3"
    r"|\buser stor(?:y|ies)\b"
    r"|\bstory points?\b",
    re.IGNORECASE,
)

# The public-facing files the scrub guard scans (the whole of ``docs/``
# is scanned separately, see ``test_docs_have_no_*``).
_SCRUBBED_FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
]


def _issue_forms() -> list[Path]:
    return sorted(
        p for p in ISSUE_TEMPLATE_DIR.glob("*.yml") if p.name != "config.yml"
    )


# --- required files ------------------------------------------------------


@pytest.mark.parametrize("rel", REQUIRED_FILES)
def test_required_community_file_present_and_nonempty(rel: str) -> None:
    path = REPO_ROOT / rel
    assert path.is_file(), f"missing required community file: {rel}"
    assert path.read_text(encoding="utf-8").strip(), f"{rel} is empty"


def test_license_is_apache_2() -> None:
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text


def test_changelog_documents_the_shipped_version() -> None:
    from inkfoot._version import __version__

    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert __version__ in text, (
        f"CHANGELOG.md has no entry for the shipped version {__version__}"
    )


def test_contributing_points_to_security_policy() -> None:
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "SECURITY.md" in text


# --- issue templates -----------------------------------------------------


def test_issue_template_chooser_disables_blank_issues() -> None:
    cfg = yaml.safe_load((ISSUE_TEMPLATE_DIR / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is False
    # A security contact link must steer reports away from public issues.
    links = " ".join(
        f"{c.get('name', '')} {c.get('url', '')}" for c in cfg.get("contact_links", [])
    ).lower()
    assert "security" in links


def test_all_required_issue_forms_present() -> None:
    names = {p.name for p in _issue_forms()}
    missing = [f for f in REQUIRED_ISSUE_FORMS if f not in names]
    assert not missing, f"missing issue forms: {missing}"


@pytest.mark.parametrize("form", REQUIRED_ISSUE_FORMS)
def test_issue_form_is_valid(form: str) -> None:
    doc = yaml.safe_load((ISSUE_TEMPLATE_DIR / form).read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{form} is not a mapping"
    for key in ("name", "description", "body"):
        assert key in doc, f"{form} missing top-level '{key}'"
    body = doc["body"]
    assert isinstance(body, list) and body, f"{form} has an empty body"

    ids: list[str] = []
    for element in body:
        etype = element.get("type")
        assert etype in _VALID_ELEMENT_TYPES, f"{form}: bad element type {etype!r}"
        if etype != "markdown":
            assert "id" in element, f"{form}: non-markdown element without an id"
            ids.append(element["id"])
        if etype in {"input", "textarea", "dropdown", "checkboxes"}:
            assert "attributes" in element, f"{form}: {etype} element without attributes"
    assert len(ids) == len(set(ids)), f"{form}: duplicate element ids {ids}"


def test_coverage_gap_templates_target_the_right_audience() -> None:
    provider = yaml.safe_load(
        (ISSUE_TEMPLATE_DIR / "provider_coverage_gap.yml").read_text(encoding="utf-8")
    )
    langchain = yaml.safe_load(
        (ISSUE_TEMPLATE_DIR / "langchain_integration_gap.yml").read_text(encoding="utf-8")
    )
    assert "provider" in (provider["name"].lower() + provider["description"].lower())
    assert "langchain" in (langchain["name"].lower() + langchain["description"].lower())


# --- pull-request template ----------------------------------------------


def test_pull_request_template_has_a_checklist() -> None:
    text = PR_TEMPLATE.read_text(encoding="utf-8").lower()
    assert "checklist" in text
    assert "pytest" in text
    # A reviewer should see a box to confirm tests cover the change.
    assert "- [ ]" in PR_TEMPLATE.read_text(encoding="utf-8")


# --- scrub guard: no internal / customer references ----------------------


@pytest.mark.parametrize("rel", _SCRUBBED_FILES)
def test_public_file_has_no_internal_or_customer_references(rel: str) -> None:
    path = REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not present")
    offenders = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = _FORBIDDEN.search(line)
        if match:
            offenders.append(f"{rel}:{lineno}: {match.group(0)!r}")
    assert not offenders, "internal/customer references in public files:\n" + "\n".join(
        offenders
    )


def test_issue_forms_have_no_internal_or_customer_references() -> None:
    offenders = []
    for path in [*_issue_forms(), ISSUE_TEMPLATE_DIR / "config.yml"]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _FORBIDDEN.search(line)
            if match:
                offenders.append(f"{path.name}:{lineno}: {match.group(0)!r}")
    assert not offenders, "internal/customer references in issue templates:\n" + "\n".join(
        offenders
    )


def test_docs_have_no_internal_or_customer_references() -> None:
    # The whole published docs tree is scanned, so a future page — a new
    # concept guide, or a blog post built on reference-repo data that must
    # not name its source — cannot reintroduce a planning id or a customer
    # name without failing CI.
    offenders = []
    for path in sorted((REPO_ROOT / "docs").rglob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _FORBIDDEN.search(line)
            if match:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {match.group(0)!r}")
    assert not offenders, "internal/customer references in docs:\n" + "\n".join(offenders)


# --- code of conduct -----------------------------------------------------


def test_code_of_conduct_is_contributor_covenant() -> None:
    path = REPO_ROOT / "CODE_OF_CONDUCT.md"
    assert path.is_file(), "CODE_OF_CONDUCT.md must live at the repo root"
    text = path.read_text(encoding="utf-8").lower()
    assert "contributor covenant" in text


def test_code_of_conduct_has_a_real_reporting_contact() -> None:
    # A shipped CoC must not still carry the template placeholder.
    text = (REPO_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    assert "[INSERT" not in text.upper(), "CoC still has a placeholder contact"
    assert "@" in text, "CoC has no reporting contact address"
