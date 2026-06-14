"""The bundled cost smell library.

The package ships a frozen snapshot of the open smell catalogue
(``_snapshot.json``) so the library is available offline and refreshes
when the package is upgraded. The snapshot is generated at release time
from the catalogue's YAML definitions and validated against the catalogue
schema; this module is the read side — it loads that snapshot into typed
:class:`LibrarySmell` records.

The loader deliberately depends on nothing beyond the standard library:
the snapshot was already validated against the schema when it was built,
so at runtime a light structural check is enough and no schema-validation
dependency is pulled into a user's install. The built-in detectors in
:mod:`inkfoot.smells` remain the authoritative detection path; this
library is the distributable *catalogue* — metadata, recommendations,
and (once estimated) savings figures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from inkfoot.errors import InkfootError

__all__ = [
    "LibrarySmell",
    "LibrarySnapshot",
    "LibrarySnapshotError",
    "SNAPSHOT_PATH",
    "SNAPSHOT_SCHEMA_VERSION",
    "load_snapshot",
    "list_library_smells",
    "get_library_smell",
]

# The snapshot lives next to this module and is shipped as package data.
SNAPSHOT_PATH = Path(__file__).with_name("_snapshot.json")

# The snapshot envelope version. Bumped only if the *envelope* shape
# changes; the per-smell shape is governed by the catalogue schema.
SNAPSHOT_SCHEMA_VERSION = 1

_REQUIRED_SMELL_KEYS = frozenset(
    {"id", "title", "severity", "description", "detection", "recommendation"}
)


class LibrarySnapshotError(InkfootError):
    """Raised when the bundled snapshot is missing or structurally invalid.

    A valid snapshot is written by the build step and validated against
    the schema there, so hitting this at runtime means the shipped file
    was corrupted or hand-edited — the message names what was wrong.
    """


@dataclass(frozen=True, slots=True)
class LibrarySmell:
    """One smell from the bundled catalogue.

    Mirrors the catalogue YAML schema. ``estimated_savings`` and
    ``evidence_kind`` are populated by the savings-estimation worker and
    are ``None`` for smells that have no measured saving yet.
    """

    id: str
    title: str
    severity: str
    description: str
    detection: Mapping[str, Any]
    recommendation: str
    suggested_policy: Optional[str] = None
    primary_category: Optional[str] = None
    evidence_query: str = ""
    evidence_kind: Optional[str] = None
    estimated_savings: Optional[Mapping[str, Any]] = None

    @property
    def has_estimated_savings(self) -> bool:
        """True when the worker has filled in a savings estimate."""
        return self.estimated_savings is not None


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """The loaded snapshot: an ordered, immutable set of library smells."""

    schema_version: int
    source: str
    smells: tuple[LibrarySmell, ...]


# Loaded once, then cached — the snapshot is immutable for the life of
# the process.
_cache: Optional[LibrarySnapshot] = None


def _smell_from_entry(entry: Any, *, index: int) -> LibrarySmell:
    if not isinstance(entry, dict):
        raise LibrarySnapshotError(
            f"snapshot smell at index {index} must be an object, got "
            f"{type(entry).__name__}"
        )
    missing = _REQUIRED_SMELL_KEYS - entry.keys()
    if missing:
        raise LibrarySnapshotError(
            f"snapshot smell at index {index} is missing required field(s): "
            f"{', '.join(sorted(missing))}"
        )
    detection = entry["detection"]
    if not isinstance(detection, dict) or "language" not in detection or "query" not in detection:
        raise LibrarySnapshotError(
            f"snapshot smell {entry.get('id', index)!r}: 'detection' must be an "
            f"object with 'language' and 'query'"
        )
    return LibrarySmell(
        id=entry["id"],
        title=entry["title"],
        severity=entry["severity"],
        description=entry["description"],
        detection=detection,
        recommendation=entry["recommendation"],
        suggested_policy=entry.get("suggested_policy"),
        primary_category=entry.get("primary_category"),
        evidence_query=entry.get("evidence_query", "") or "",
        evidence_kind=entry.get("evidence_kind"),
        estimated_savings=entry.get("estimated_savings"),
    )


def _parse_snapshot(raw: Any) -> LibrarySnapshot:
    if not isinstance(raw, dict):
        raise LibrarySnapshotError(
            f"snapshot must be a JSON object, got {type(raw).__name__}"
        )
    smells_raw = raw.get("smells")
    if not isinstance(smells_raw, list):
        raise LibrarySnapshotError("snapshot 'smells' must be a list")

    smells = tuple(
        _smell_from_entry(entry, index=i) for i, entry in enumerate(smells_raw)
    )

    declared = raw.get("smell_count")
    if declared is not None and declared != len(smells):
        raise LibrarySnapshotError(
            f"snapshot 'smell_count' ({declared}) does not match the number "
            f"of smells ({len(smells)})"
        )

    ids = [s.id for s in smells]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise LibrarySnapshotError(
            f"snapshot contains duplicate smell id(s): {', '.join(dupes)}"
        )

    return LibrarySnapshot(
        schema_version=raw.get("schema_version", SNAPSHOT_SCHEMA_VERSION),
        source=raw.get("source", ""),
        smells=smells,
    )


def load_snapshot(*, force: bool = False) -> LibrarySnapshot:
    """Load (and cache) the bundled snapshot.

    Pass ``force=True`` to bypass the cache and re-read from disk — used
    by tests that write a temporary snapshot. Raises
    :class:`LibrarySnapshotError` when the file is missing or malformed.
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    try:
        text = SNAPSHOT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise LibrarySnapshotError(
            f"bundled smell snapshot is not readable at {SNAPSHOT_PATH}: {exc}"
        ) from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise LibrarySnapshotError(
            f"bundled smell snapshot at {SNAPSHOT_PATH} is not valid JSON: {exc}"
        ) from exc

    snapshot = _parse_snapshot(raw)
    _cache = snapshot
    return snapshot


def list_library_smells() -> list[LibrarySmell]:
    """Return every smell in the bundled catalogue, in snapshot order."""
    return list(load_snapshot().smells)


def get_library_smell(smell_id: str) -> LibrarySmell:
    """Return the library smell with id ``smell_id``.

    Raises :class:`KeyError` when the id is not in the bundled catalogue.
    """
    for smell in load_snapshot().smells:
        if smell.id == smell_id:
            return smell
    known = sorted(s.id for s in load_snapshot().smells)
    raise KeyError(
        f"get_library_smell: unknown smell id {smell_id!r}. Known ids: {known}"
    )
