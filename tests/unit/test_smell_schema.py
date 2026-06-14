"""Tests for the smell-definition schema and the lint validator.

The validator is the logic the catalogue's lint bot runs on every pull
request. These tests pin its three jobs — schema conformance, rejecting
reserved fields, and rejecting expensive detection queries — plus the
publish-bar fixture check, and assert the schema itself reserves the
savings fields.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALIDATOR_PATH = _REPO_ROOT / "cost-smells" / "tools" / "validate_smells.py"
_SCHEMA_PATH = _REPO_ROOT / "cost-smells" / "schema" / "smell.schema.json"
_SMELLS_DIR = _REPO_ROOT / "cost-smells" / "smells"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_smells", _VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_smells"] = module
    spec.loader.exec_module(module)
    return module


_validator = _load_validator()
_SCHEMA = _validator.load_schema(_SCHEMA_PATH)


def _valid_smell() -> dict:
    return {
        "id": "my-org/example-smell",
        "title": "Example smell",
        "severity": "warn",
        "description": "An example pattern that wastes tokens.",
        "detection": {
            "language": "jsonpath",
            "query": "$..ledger.system_dynamic_tokens",
            "trigger_condition": "value > 0.10",
        },
        "recommendation": "Do the one obvious thing.",
        "suggested_policy": "CacheControlPlacer",
    }


def _errors(doc: dict) -> list[str]:
    return _validator.validate_smell_doc(doc, _SCHEMA, source="test.yaml")


# ----------------------------------------------------------------------
# Schema conformance
# ----------------------------------------------------------------------


def test_valid_smell_passes() -> None:
    assert _errors(_valid_smell()) == []


def test_missing_required_field_fails() -> None:
    doc = _valid_smell()
    del doc["recommendation"]
    errors = _errors(doc)
    assert any("recommendation" in e for e in errors)


def test_wrong_type_fails() -> None:
    doc = _valid_smell()
    doc["title"] = 123
    assert any("title" in e for e in _errors(doc))


def test_unknown_top_level_field_fails() -> None:
    doc = _valid_smell()
    doc["whoops"] = "extra"
    assert any("whoops" in e or "additional" in e.lower() for e in _errors(doc))


def test_unknown_severity_fails() -> None:
    doc = _valid_smell()
    doc["severity"] = "catastrophic"
    assert any("severity" in e for e in _errors(doc))


def test_bad_id_pattern_fails() -> None:
    doc = _valid_smell()
    doc["id"] = "Not Kebab Case"
    assert any("id" in e for e in _errors(doc))


def test_namespaced_id_is_allowed() -> None:
    doc = _valid_smell()
    doc["id"] = "acme/widget-overhead"
    assert _errors(doc) == []


def test_detection_missing_query_fails() -> None:
    doc = _valid_smell()
    del doc["detection"]["query"]
    assert any("query" in e for e in _errors(doc))


def test_null_suggested_policy_is_allowed() -> None:
    doc = _valid_smell()
    doc["suggested_policy"] = None
    assert _errors(doc) == []


# ----------------------------------------------------------------------
# Reserved fields
# ----------------------------------------------------------------------


def test_manually_set_estimated_savings_is_rejected() -> None:
    doc = _valid_smell()
    doc["estimated_savings"] = {
        "corpus_runs": 10,
        "triggered_in": 5,
        "estimated_potential_saved_avg_percent": 12.0,
        "estimated_potential_saved_nanodollars_per_run": 1000,
        "confidence": "low",
        "last_computed": "2026-01-01",
    }
    assert any("estimated_savings" in e and "reserved" in e for e in _errors(doc))


def test_manually_set_evidence_kind_is_rejected() -> None:
    doc = _valid_smell()
    doc["evidence_kind"] = "simulation"
    assert any("evidence_kind" in e and "reserved" in e for e in _errors(doc))


# ----------------------------------------------------------------------
# Detection query cost
# ----------------------------------------------------------------------


def test_sql_cross_join_is_rejected() -> None:
    assert _validator.looks_like_slow_query("sql", "SELECT a FROM x CROSS JOIN y")


def test_sql_comma_join_is_rejected() -> None:
    assert _validator.looks_like_slow_query("sql", "SELECT a FROM runs r, events e")


def test_sql_multiple_joins_is_rejected() -> None:
    q = "SELECT a FROM x JOIN y ON x.id=y.x JOIN z ON y.id=z.y"
    assert _validator.looks_like_slow_query("sql", q)


def test_jsonpath_double_recursive_descent_is_rejected() -> None:
    assert _validator.looks_like_slow_query("jsonpath", "$..a..b")


def test_simple_jsonpath_is_allowed() -> None:
    assert _validator.looks_like_slow_query("jsonpath", "$..ledger.memory_tokens") is None


def test_simple_single_table_sql_is_allowed() -> None:
    q = "SELECT memory_tokens FROM events WHERE run_id = :run_id"
    assert _validator.looks_like_slow_query("sql", q) is None


def test_sql_in_clause_comma_is_not_a_join() -> None:
    q = "SELECT a FROM events WHERE x IN (1, 2)"
    assert _validator.looks_like_slow_query("sql", q) is None


def test_sql_group_by_comma_is_not_a_join() -> None:
    q = "SELECT a FROM events GROUP BY model, day"
    assert _validator.looks_like_slow_query("sql", q) is None


def test_sql_order_by_comma_is_not_a_join() -> None:
    q = "SELECT a FROM events ORDER BY a, b"
    assert _validator.looks_like_slow_query("sql", q) is None


def test_sql_real_from_comma_join_is_still_rejected() -> None:
    assert _validator.looks_like_slow_query("sql", "SELECT a FROM runs r, events e")


def test_builtin_queries_are_exempt_from_cost_check() -> None:
    # A documentary builtin query that would look pathological as SQL is
    # not checked, because builtin detectors ship vetted Python.
    assert _validator.looks_like_slow_query("builtin", "x JOIN y JOIN z, w") is None


def test_slow_query_surfaces_through_validate_smell_doc() -> None:
    doc = _valid_smell()
    doc["detection"] = {"language": "sql", "query": "SELECT a FROM x CROSS JOIN y"}
    assert any("O(events" in e for e in _errors(doc))


# ----------------------------------------------------------------------
# Fixtures (the publish bar)
# ----------------------------------------------------------------------


def _write_fixtures(root: Path, smell_id: str, positives: int, negatives: int) -> None:
    for kind, count in (("positive", positives), ("negative", negatives)):
        d = root / smell_id / kind
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (d / f"case-{i}.json").write_text(json.dumps({"name": f"{kind}-{i}"}))


def test_fixtures_pass_with_three_each(tmp_path: Path) -> None:
    _write_fixtures(tmp_path, "demo", 3, 3)
    assert _validator.validate_fixtures("demo", tmp_path) == []


def test_fixtures_fail_with_too_few_positives(tmp_path: Path) -> None:
    _write_fixtures(tmp_path, "demo", 2, 3)
    errors = _validator.validate_fixtures("demo", tmp_path)
    assert any("positive" in e for e in errors)


def test_fixtures_fail_when_directory_absent(tmp_path: Path) -> None:
    errors = _validator.validate_fixtures("missing", tmp_path)
    assert any("no fixtures directory" in e for e in errors)


def test_fixtures_fail_on_invalid_json(tmp_path: Path) -> None:
    _write_fixtures(tmp_path, "demo", 3, 3)
    (tmp_path / "demo" / "positive" / "broken.json").write_text("{not json")
    errors = _validator.validate_fixtures("demo", tmp_path)
    assert any("not valid JSON" in e for e in errors)


# ----------------------------------------------------------------------
# --require-fixtures: required for community smells, exempt for builtin
# ----------------------------------------------------------------------


def _write_smell_file(tmp_path: Path, *, language: str, smell_id: str = "demo-smell") -> Path:
    import yaml as _yaml

    doc = _valid_smell()
    doc["id"] = smell_id
    query = "documentary" if language == "builtin" else "$..ledger.memory_tokens"
    doc["detection"] = {"language": language, "query": query}
    path = tmp_path / f"{smell_id}.yaml"
    path.write_text(_yaml.safe_dump(doc))
    return path


def test_require_fixtures_exempts_builtin_smell(tmp_path: Path) -> None:
    path = _write_smell_file(tmp_path, language="builtin")
    errors = _validator.validate_file(
        path, _SCHEMA, require_fixtures=True, fixtures_root=tmp_path / "fixtures"
    )
    assert errors == []


def test_require_fixtures_demands_fixtures_for_community_smell(tmp_path: Path) -> None:
    path = _write_smell_file(tmp_path, language="jsonpath")
    errors = _validator.validate_file(
        path, _SCHEMA, require_fixtures=True, fixtures_root=tmp_path / "fixtures"
    )
    assert any("fixtures" in e for e in errors)


def test_require_fixtures_passes_when_community_smell_ships_them(tmp_path: Path) -> None:
    path = _write_smell_file(tmp_path, language="jsonpath", smell_id="demo-smell")
    fixtures_root = tmp_path / "fixtures"
    _write_fixtures(fixtures_root, "demo-smell", 3, 3)
    errors = _validator.validate_file(
        path, _SCHEMA, require_fixtures=True, fixtures_root=fixtures_root
    )
    assert errors == []


@pytest.mark.parametrize(
    "path", sorted(_SMELLS_DIR.glob("*.yaml")), ids=lambda p: p.name
)
def test_catalogue_passes_under_require_fixtures(path: Path) -> None:
    # The seed is all `builtin`, so it stays green under the lint flag the
    # workflow uses; the one smell that ships fixtures is still validated.
    assert _validator.validate_file(path, _SCHEMA, require_fixtures=True) == []


# ----------------------------------------------------------------------
# The schema reserves the savings fields
# ----------------------------------------------------------------------


def test_schema_reserves_estimated_savings_and_evidence_kind() -> None:
    props = _SCHEMA["properties"]
    assert "estimated_savings" in props
    assert "evidence_kind" in props
    assert props["evidence_kind"]["enum"] == [
        "simulation",
        "replay_pair",
        "production_pair",
    ]


def test_schema_rejects_unknown_top_level_fields() -> None:
    assert _SCHEMA["additionalProperties"] is False


# ----------------------------------------------------------------------
# The shipped catalogue passes its own validator
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", sorted(_SMELLS_DIR.glob("*.yaml")), ids=lambda p: p.name
)
def test_every_catalogue_file_is_valid(path: Path) -> None:
    assert _validator.validate_file(path, _SCHEMA) == []
