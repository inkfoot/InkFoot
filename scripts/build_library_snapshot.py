#!/usr/bin/env python3
"""Build the bundled cost smell snapshot from the catalogue YAML files.

Reads every ``cost-smells/smells/*.yaml``, validates each against
``cost-smells/schema/smell.schema.json`` (and a few package-specific
rules), and writes a deterministic ``inkfoot/library/_snapshot.json``
that ships with the package.

The pre-release pipeline regenerates the snapshot at tag time so a
release always carries a snapshot current with the catalogue. The
committed snapshot is kept in sync by a drift test in the package's test
suite; ``--check`` runs the same comparison on demand.

    python scripts/build_library_snapshot.py          # regenerate
    python scripts/build_library_snapshot.py --check   # verify in sync
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SMELLS_DIR = REPO_ROOT / "cost-smells" / "smells"
SCHEMA_PATH = REPO_ROOT / "cost-smells" / "schema" / "smell.schema.json"
SNAPSHOT_PATH = REPO_ROOT / "inkfoot" / "library" / "_snapshot.json"

SNAPSHOT_SCHEMA_VERSION = 1
SOURCE = "cost-smells"

# Reserved fields the savings-estimation worker owns; the seeded snapshot
# never carries them.
RESERVED_FIELDS = ("estimated_savings", "evidence_kind")

# Fields carried into each snapshot entry. Optional ones are normalised
# to an explicit null / empty so consumers see a stable shape.
_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "suggested_policy": None,
    "primary_category": None,
    "evidence_query": "",
}


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _normalise_entry(doc: dict[str, Any]) -> dict[str, Any]:
    """Project a parsed smell YAML into a stable snapshot entry."""
    entry: dict[str, Any] = {
        "id": doc["id"],
        "title": doc["title"],
        "severity": doc["severity"],
        "description": doc["description"],
        "detection": {
            k: v
            for k, v in doc["detection"].items()
            if k in {"language", "query", "trigger_condition"} and v is not None
        },
        "recommendation": doc["recommendation"],
    }
    for field, default in _OPTIONAL_DEFAULTS.items():
        entry[field] = doc.get(field, default)
    # Reserved fields are carried through only if the worker has set them
    # (it never has, for the seeded snapshot).
    for field in RESERVED_FIELDS:
        if doc.get(field) is not None:
            entry[field] = doc[field]
    return entry


def build_snapshot() -> dict[str, Any]:
    """Validate every catalogue smell and return the snapshot object."""
    schema = load_schema()
    validator = Draft202012Validator(schema)

    files = sorted(SMELLS_DIR.glob("*.yaml"))
    if not files:
        raise SystemExit(f"no smell files found in {SMELLS_DIR}")

    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    seen_ids: set[str] = set()

    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = path.name

        schema_errors = sorted(
            validator.iter_errors(doc), key=lambda e: list(e.path)
        )
        for err in schema_errors:
            loc = "/".join(str(p) for p in err.path) or "(root)"
            problems.append(f"{source}: schema error at {loc}: {err.message}")
        if schema_errors:
            continue

        for reserved in RESERVED_FIELDS:
            if reserved in doc:
                problems.append(
                    f"{source}: '{reserved}' is reserved for the "
                    f"savings-estimation worker and must not be set by hand"
                )

        smell_id = doc["id"]
        if smell_id in seen_ids:
            problems.append(f"{source}: duplicate smell id {smell_id!r}")
        seen_ids.add(smell_id)

        entries.append(_normalise_entry(doc))

    if problems:
        raise SystemExit(
            "snapshot build failed:\n  - " + "\n  - ".join(problems)
        )

    entries.sort(key=lambda e: e["id"])
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source": SOURCE,
        "smell_count": len(entries),
        "smells": entries,
    }


def serialise(snapshot: dict[str, Any]) -> str:
    """Deterministic JSON text for the snapshot (stable across runs)."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed snapshot matches the YAML sources; do not write.",
    )
    args = parser.parse_args(argv)

    snapshot = build_snapshot()
    text = serialise(snapshot)

    if args.check:
        if not SNAPSHOT_PATH.exists():
            print(f"FAIL: {SNAPSHOT_PATH} does not exist; run without --check", file=sys.stderr)
            return 1
        current = SNAPSHOT_PATH.read_text(encoding="utf-8")
        if current != text:
            print(
                f"FAIL: {SNAPSHOT_PATH.relative_to(REPO_ROOT)} is out of date.\n"
                f"      Regenerate it with: python scripts/build_library_snapshot.py",
                file=sys.stderr,
            )
            return 1
        print(f"ok: snapshot is in sync ({snapshot['smell_count']} smells)")
        return 0

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")
    print(
        f"wrote {SNAPSHOT_PATH.relative_to(REPO_ROOT)} "
        f"({snapshot['smell_count']} smells)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
