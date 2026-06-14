"""Bundled smell-snapshot tests.

The snapshot is the offline copy of the smell catalogue that ships inside
the package. These tests assert it loads, that every entry validates
against the catalogue schema, that it stays faithful to the built-in
smells it is seeded from, and that it never drifts from the YAML sources
it is generated from.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from inkfoot.library import (
    LibrarySnapshotError,
    SNAPSHOT_PATH,
    get_library_smell,
    list_library_smells,
    load_snapshot,
)
from inkfoot.library import _parse_snapshot, _smell_from_entry
from inkfoot.smells import DEFAULT_SMELLS, get_smell

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "cost-smells" / "schema" / "smell.schema.json"
_BUILDER_PATH = _REPO_ROOT / "scripts" / "build_library_snapshot.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_library_snapshot", _BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_library_snapshot"] = module
    spec.loader.exec_module(module)
    return module


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _raw_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    """Collapse internal whitespace so YAML line-wrapping doesn't matter."""
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------


def test_snapshot_loads() -> None:
    snapshot = load_snapshot()
    assert snapshot.smells
    assert len(snapshot.smells) == len(_raw_snapshot()["smells"])


def test_snapshot_envelope_metadata() -> None:
    snapshot = load_snapshot()
    assert snapshot.schema_version == 1
    assert snapshot.source == "cost-smells"


def test_load_snapshot_is_cached_but_force_rereads() -> None:
    first = load_snapshot()
    assert load_snapshot() is first
    assert load_snapshot(force=True) is not first


def test_list_library_smells_returns_every_entry() -> None:
    assert len(list_library_smells()) == len(_raw_snapshot()["smells"])


def test_get_library_smell_returns_the_named_smell() -> None:
    smell = get_library_smell("unstable-prompt-prefix")
    assert smell.id == "unstable-prompt-prefix"
    assert smell.suggested_policy == "CacheControlPlacer"


def test_get_library_smell_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown smell id"):
        get_library_smell("does-not-exist")


# ----------------------------------------------------------------------
# Schema conformance
# ----------------------------------------------------------------------


def test_every_entry_validates_against_schema() -> None:
    validator = Draft202012Validator(_schema())
    for entry in _raw_snapshot()["smells"]:
        errors = sorted(validator.iter_errors(entry), key=lambda e: list(e.path))
        assert not errors, f"{entry.get('id')}: {[e.message for e in errors]}"


def test_smell_count_matches_entries() -> None:
    raw = _raw_snapshot()
    assert raw["smell_count"] == len(raw["smells"])


def test_entries_are_sorted_by_id() -> None:
    ids = [e["id"] for e in _raw_snapshot()["smells"]]
    assert ids == sorted(ids)


# ----------------------------------------------------------------------
# Fidelity to the built-in smells the snapshot is seeded from
# ----------------------------------------------------------------------


def test_snapshot_ids_match_builtin_smells() -> None:
    snapshot_ids = {s.id for s in list_library_smells()}
    builtin_ids = {s.id for s in DEFAULT_SMELLS}
    assert snapshot_ids == builtin_ids


@pytest.mark.parametrize("smell_id", [s.id for s in DEFAULT_SMELLS])
def test_snapshot_fields_mirror_builtin(smell_id: str) -> None:
    lib = get_library_smell(smell_id)
    builtin = get_smell(smell_id)
    assert lib.title == builtin.title
    assert lib.severity == builtin.severity
    assert lib.suggested_policy == builtin.suggested_policy
    assert lib.primary_category == builtin.primary_category
    assert lib.evidence_query == builtin.evidence_query
    assert _norm(lib.description) == _norm(builtin.description)
    assert _norm(lib.recommendation) == _norm(builtin.recommendation)


def test_seeded_snapshot_has_no_savings_yet() -> None:
    # The seed predates the savings-estimation worker, so no entry carries
    # a savings number or evidence kind.
    for smell in list_library_smells():
        assert smell.estimated_savings is None
        assert smell.evidence_kind is None
        assert smell.has_estimated_savings is False


def test_suggested_policy_references_a_real_policy() -> None:
    import inkfoot.policy as policy_pkg
    from inkfoot.policy import Policy

    real = {
        getattr(policy_pkg, name).__name__
        for name in policy_pkg.__all__
        if isinstance(getattr(policy_pkg, name), type)
        and issubclass(getattr(policy_pkg, name), Policy)
        and getattr(policy_pkg, name) is not Policy
    }
    for smell in list_library_smells():
        if smell.suggested_policy is not None:
            assert smell.suggested_policy in real, smell.id


# ----------------------------------------------------------------------
# The snapshot must not drift from the YAML sources
# ----------------------------------------------------------------------


def test_snapshot_in_sync_with_yaml_sources() -> None:
    builder = _load_builder()
    expected = builder.serialise(builder.build_snapshot())
    actual = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "inkfoot/library/_snapshot.json is out of date; "
        "regenerate with `python scripts/build_library_snapshot.py`"
    )


# ----------------------------------------------------------------------
# Loader structural validation
# ----------------------------------------------------------------------


def test_parse_snapshot_rejects_non_object() -> None:
    with pytest.raises(LibrarySnapshotError, match="must be a JSON object"):
        _parse_snapshot([1, 2, 3])


def test_parse_snapshot_rejects_count_mismatch() -> None:
    bad = {"schema_version": 1, "source": "x", "smell_count": 5, "smells": []}
    with pytest.raises(LibrarySnapshotError, match="smell_count"):
        _parse_snapshot(bad)


def test_parse_snapshot_rejects_duplicate_ids() -> None:
    entry = {
        "id": "dupe",
        "title": "t",
        "severity": "warn",
        "description": "d",
        "detection": {"language": "jsonpath", "query": "$.a"},
        "recommendation": "r",
    }
    with pytest.raises(LibrarySnapshotError, match="duplicate"):
        _parse_snapshot({"smells": [entry, dict(entry)]})


def test_smell_from_entry_requires_core_fields() -> None:
    with pytest.raises(LibrarySnapshotError, match="missing required"):
        _smell_from_entry({"id": "x"}, index=0)


def test_smell_from_entry_requires_detection_shape() -> None:
    entry = {
        "id": "x",
        "title": "t",
        "severity": "warn",
        "description": "d",
        "detection": {"language": "jsonpath"},  # no query
        "recommendation": "r",
    }
    with pytest.raises(LibrarySnapshotError, match="detection"):
        _smell_from_entry(entry, index=0)


def test_load_snapshot_reports_missing_file(tmp_path, monkeypatch) -> None:
    import inkfoot.library as library

    monkeypatch.setattr(library, "SNAPSHOT_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(library, "_cache", None)
    with pytest.raises(LibrarySnapshotError, match="not readable"):
        library.load_snapshot(force=True)
