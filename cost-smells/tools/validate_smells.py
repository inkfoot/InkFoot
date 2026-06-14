#!/usr/bin/env python3
"""Validate cost-smell definition files against the frozen v1 schema.

This is the logic the lint bot runs on every pull request. It checks
three things, in order, and reports *all* problems it finds rather than
stopping at the first:

1. **Schema conformance** — each file is a single smell object that
   validates against ``schema/smell.schema.json`` (missing required
   fields, wrong types, unknown keys, malformed ids all fail here).
2. **Reserved fields** — ``estimated_savings`` and ``evidence_kind`` are
   filled in by the savings-estimation worker, never by a contributor.
   A file that sets either by hand is rejected.
3. **Query cost** — a declarative detection query (``jsonpath`` / ``sql``)
   must terminate in O(events x constant). Cross-joins, implicit
   comma-joins, and nested recursive scans are rejected as pathological.

Optionally it also checks that a smell ships the publish-bar fixtures
(at least 3 positive and 3 negative) when a fixtures directory is
present, or for every community (non-``builtin``) smell when
``--require-fixtures`` is passed.

Run it directly::

    python tools/validate_smells.py                 # all smells/*.yaml
    python tools/validate_smells.py smells/foo.yaml  # specific files
    python tools/validate_smells.py --require-fixtures
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "validate_smells.py needs the 'jsonschema' package: pip install jsonschema"
    ) from exc


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "smell.schema.json"
SMELLS_DIR = REPO_ROOT / "smells"
FIXTURES_DIR = REPO_ROOT / "fixtures"

# Set only by the savings-estimation worker; a hand-authored file that
# carries either is rejected so contributors can't claim savings the
# worker hasn't measured.
RESERVED_FIELDS = ("estimated_savings", "evidence_kind")

MIN_POSITIVE_FIXTURES = 3
MIN_NEGATIVE_FIXTURES = 3


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load and return the parsed JSON Schema."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def looks_like_slow_query(language: str, query: str) -> Optional[str]:
    """Return a reason string when ``query`` looks pathological, else None.

    Detection queries run once per analysed run, so anything worse than
    a single linear pass over the event stream is rejected. ``builtin``
    detectors ship vetted Python and carry only a documentary query, so
    they're exempt.
    """
    if language == "builtin":
        return None

    lowered = query.lower()

    if language == "sql":
        if "cross join" in lowered:
            return "uses CROSS JOIN"
        # Implicit comma-join: a comma between table refs *inside the FROM
        # clause* (`FROM a, b`). Scope the search to the FROM clause so a
        # comma in IN (...), GROUP BY, or ORDER BY isn't misread as a join.
        from_clause = re.search(
            r"\bfrom\b(.*?)(?:\bwhere\b|\bgroup\b|\border\b|\bhaving\b"
            r"|\blimit\b|\bjoin\b|$)",
            lowered,
            re.DOTALL,
        )
        if from_clause and re.search(r"\w+\s*,\s*\w+", from_clause.group(1)):
            return "uses an implicit comma-join (FROM a, b)"
        if len(re.findall(r"\bjoin\b", lowered)) > 1:
            return "chains more than one JOIN (possible fan-out)"
        return None

    if language == "jsonpath":
        # Each `..` is a recursive descent / full subtree scan. One is a
        # linear pass; two or more nests a scan inside a scan.
        if query.count("..") > 1:
            return "nests more than one recursive descent ('..')"
        return None

    return None


def validate_smell_doc(
    doc: Any, schema: dict[str, Any], *, source: str
) -> list[str]:
    """Validate one parsed smell document. Returns a list of error
    messages (empty when the document is valid)."""
    errors: list[str] = []

    if not isinstance(doc, dict):
        return [f"{source}: top-level document must be a mapping, got {type(doc).__name__}"]

    validator = Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{source}: schema error at {location}: {err.message}")

    for reserved in RESERVED_FIELDS:
        if reserved in doc:
            errors.append(
                f"{source}: '{reserved}' is reserved for the savings-estimation "
                f"worker and must not be set by hand"
            )

    detection = doc.get("detection")
    if isinstance(detection, dict):
        language = detection.get("language")
        query = detection.get("query")
        if isinstance(language, str) and isinstance(query, str):
            reason = looks_like_slow_query(language, query)
            if reason is not None:
                errors.append(
                    f"{source}: detection query may not terminate in "
                    f"O(events x constant): {reason}"
                )

    return errors


def validate_fixtures(
    smell_id: str, fixtures_root: Path = FIXTURES_DIR
) -> list[str]:
    """Check the publish-bar fixtures for a smell.

    Expects ``fixtures/<smell-id>/positive/*.json`` and
    ``.../negative/*.json`` with at least three of each. A smell whose
    id is namespaced (``owner/name``) maps to ``fixtures/owner/name/``.
    """
    errors: list[str] = []
    base = fixtures_root.joinpath(*smell_id.split("/"))
    if not base.is_dir():
        return [f"{smell_id}: no fixtures directory at {base.relative_to(fixtures_root.parent)}"]

    for kind, minimum in (
        ("positive", MIN_POSITIVE_FIXTURES),
        ("negative", MIN_NEGATIVE_FIXTURES),
    ):
        found = sorted((base / kind).glob("*.json")) if (base / kind).is_dir() else []
        if len(found) < minimum:
            errors.append(
                f"{smell_id}: needs at least {minimum} {kind} fixtures, found {len(found)}"
            )
        for fixture in found:
            try:
                json.loads(fixture.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                errors.append(f"{smell_id}: fixture {fixture.name} is not valid JSON: {exc}")
    return errors


def validate_file(
    path: Path,
    schema: dict[str, Any],
    *,
    require_fixtures: bool = False,
    fixtures_root: Path = FIXTURES_DIR,
) -> list[str]:
    """Validate a single smell file and (optionally) its fixtures."""
    source = path.name
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{source}: not valid YAML: {exc}"]

    errors = validate_smell_doc(doc, schema, source=source)

    smell_id = doc.get("id") if isinstance(doc, dict) else None
    detection = doc.get("detection") if isinstance(doc, dict) else None
    language = detection.get("language") if isinstance(detection, dict) else None
    # The 'builtin' seed ships its detectors (and their tests) inside the
    # package, so it is exempt from the fixtures bar. Every community
    # (jsonpath/sql) smell must ship fixtures under --require-fixtures. A
    # smell that already has a fixtures directory is always checked.
    is_builtin = language == "builtin"
    has_fixtures_dir = (
        isinstance(smell_id, str)
        and fixtures_root.joinpath(*smell_id.split("/")).is_dir()
    )
    must_check_fixtures = (require_fixtures and not is_builtin) or has_fixtures_dir
    if isinstance(smell_id, str) and must_check_fixtures:
        errors.extend(validate_fixtures(smell_id, fixtures_root))

    return errors


def iter_smell_files(paths: Iterable[str]) -> list[Path]:
    """Resolve CLI path args to a sorted list of smell files."""
    resolved: list[Path] = []
    args = list(paths)
    if not args:
        resolved.extend(sorted(SMELLS_DIR.glob("*.yaml")))
    else:
        for raw in args:
            p = Path(raw)
            if p.is_dir():
                resolved.extend(sorted(p.glob("*.yaml")))
            else:
                resolved.append(p)
    return resolved


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Smell files or directories to check.")
    parser.add_argument(
        "--require-fixtures",
        action="store_true",
        help="Require positive/negative fixtures for every community (non-builtin) smell, not only those that already have a fixtures directory.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCHEMA_PATH,
        help="Path to smell.schema.json.",
    )
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)
    files = iter_smell_files(args.paths)
    if not files:
        print("no smell files found", file=sys.stderr)
        return 1

    total_errors = 0
    for path in files:
        if not path.exists():
            print(f"FAIL {path}: file not found")
            total_errors += 1
            continue
        errors = validate_file(
            path, schema, require_fixtures=args.require_fixtures
        )
        if errors:
            total_errors += len(errors)
            print(f"FAIL {path.name}")
            for err in errors:
                print(f"  - {err}")
        else:
            print(f"ok   {path.name}")

    if total_errors:
        print(f"\n{total_errors} problem(s) across {len(files)} file(s)", file=sys.stderr)
        return 1
    print(f"\nall {len(files)} smell file(s) valid")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
