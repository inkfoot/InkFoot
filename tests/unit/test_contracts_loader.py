"""Tests for the Token Contract YAML loader + schema validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from inkfoot.contracts import loader as loader_mod
from inkfoot.contracts.loader import (
    load_contract,
    load_contracts,
    load_contracts_dir,
)
from inkfoot.contracts.schema import (
    CONTRACT_SCHEMA_VERSION,
    Contract,
    ContractValidationError,
    DegradeAction,
)

_VALID = """
schema_version: 1
task: customer-support-triage
cheap_model: claude-haiku-4-5
budget:
  max_nanodollars: 50_000_000
  max_llm_calls: 8
  cache_hit_rate_min: 0.70
outcome:
  required_success_rate: 0.95
  measure_window_runs: 100
degrade:
  - at_percent: 100
    action: block
  - at_percent: 80
    action: warn
  - at_percent: 90
    action: switch_to_cheap_model
overrides:
  free_tier:
    budget:
      max_nanodollars: 10_000_000
"""


def _write(tmp_path: Path, text: str, name: str = "triage.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_contract_loads(tmp_path: Path) -> None:
    contract = load_contract(_write(tmp_path, _VALID))
    assert isinstance(contract, Contract)
    assert contract.task == "customer-support-triage"
    assert contract.budget.max_nanodollars == 50_000_000
    assert contract.cheap_model == "claude-haiku-4-5"
    # The ladder is sorted ascending by at_percent regardless of file order.
    assert [s.at_percent for s in contract.degrade] == [80, 90, 100]
    assert contract.degrade[2].action is DegradeAction.BLOCK


def test_override_layers_over_base(tmp_path: Path) -> None:
    contract = load_contract(_write(tmp_path, _VALID))
    base = contract.resolved_budget()
    free = contract.resolved_budget("free_tier")
    assert base.max_nanodollars == 50_000_000
    assert free.max_nanodollars == 10_000_000
    # Unset override fields fall back to the base clause.
    assert free.max_llm_calls == 8


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    text = "schema_version: 1\nbudget:\n  max_llm_calls: 3\n"
    with pytest.raises(ContractValidationError, match="task"):
        load_contract(_write(tmp_path, text))


def test_wrong_type_rejected(tmp_path: Path) -> None:
    text = "schema_version: 1\ntask: t\nbudget:\n  max_llm_calls: not-a-number\n"
    with pytest.raises(ContractValidationError, match="max_llm_calls"):
        load_contract(_write(tmp_path, text))


def test_at_percent_out_of_range_rejected(tmp_path: Path) -> None:
    text = (
        "schema_version: 1\ntask: t\n"
        "degrade:\n  - at_percent: 150\n    action: warn\n"
    )
    with pytest.raises(ContractValidationError, match="at_percent"):
        load_contract(_write(tmp_path, text))


def test_unknown_field_rejected(tmp_path: Path) -> None:
    text = "schema_version: 1\ntask: t\nbudget:\n  max_nanodolars: 5\n"
    with pytest.raises(ContractValidationError, match="unknown field"):
        load_contract(_write(tmp_path, text))


def test_switch_without_cheap_model_rejected(tmp_path: Path) -> None:
    text = (
        "schema_version: 1\ntask: t\n"
        "degrade:\n  - at_percent: 90\n    action: switch_to_cheap_model\n"
    )
    with pytest.raises(ContractValidationError, match="cheap_model"):
        load_contract(_write(tmp_path, text))


def test_duplicate_at_percent_rejected(tmp_path: Path) -> None:
    text = (
        "schema_version: 1\ntask: t\n"
        "degrade:\n  - at_percent: 80\n    action: warn\n"
        "  - at_percent: 80\n    action: warn\n"
    )
    with pytest.raises(ContractValidationError, match="duplicate"):
        load_contract(_write(tmp_path, text))


def test_zero_max_nanodollars_rejected(tmp_path: Path) -> None:
    # A 0-dollar ceiling is nonsensical; it must fail loudly rather than
    # being silently treated as "no limit".
    text = "schema_version: 1\ntask: t\nbudget:\n  max_nanodollars: 0\n"
    with pytest.raises(ContractValidationError, match="max_nanodollars"):
        load_contract(_write(tmp_path, text))


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractValidationError, match="not found"):
        load_contract(tmp_path / "nope.yaml")


def test_empty_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractValidationError, match="empty"):
        load_contract(_write(tmp_path, "\n"))


# ----------------------------------------------------------------------
# Directory + multi-path loading
# ----------------------------------------------------------------------


def test_load_dir_rejects_duplicate_task(tmp_path: Path) -> None:
    _write(tmp_path, _VALID, "a.yaml")
    _write(tmp_path, _VALID, "b.yml")
    with pytest.raises(ContractValidationError, match="duplicate task"):
        load_contracts_dir(tmp_path)


def test_load_dir_returns_task_map(tmp_path: Path) -> None:
    _write(tmp_path, _VALID, "a.yaml")
    other = _VALID.replace("customer-support-triage", "billing")
    _write(tmp_path, other, "b.yaml")
    contracts = load_contracts_dir(tmp_path)
    assert set(contracts) == {"customer-support-triage", "billing"}


def test_load_contracts_mixes_files_and_dirs(tmp_path: Path) -> None:
    d = tmp_path / "contracts"
    d.mkdir()
    _write(d, _VALID, "a.yaml")
    standalone = tmp_path / "billing.yaml"
    standalone.write_text(
        _VALID.replace("customer-support-triage", "billing"), encoding="utf-8"
    )
    contracts = load_contracts([d, standalone])
    assert set(contracts) == {"customer-support-triage", "billing"}


# ----------------------------------------------------------------------
# Schema versioning + deprecation
# ----------------------------------------------------------------------


def test_too_new_schema_version_rejected(tmp_path: Path) -> None:
    text = _VALID.replace("schema_version: 1", f"schema_version: {CONTRACT_SCHEMA_VERSION + 1}")
    with pytest.raises(ContractValidationError, match="newer than this build"):
        load_contract(_write(tmp_path, text))


def test_too_old_schema_version_rejected(tmp_path: Path) -> None:
    text = _VALID.replace("schema_version: 1", "schema_version: -1")
    with pytest.raises(ContractValidationError, match="too old"):
        load_contract(_write(tmp_path, text))


def test_previous_schema_version_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The immediately-preceding version (current - 1) loads, but with a
    # one-time deprecation warning per process.
    loader_mod._warned_versions.clear()
    text = _VALID.replace(
        "schema_version: 1", f"schema_version: {CONTRACT_SCHEMA_VERSION - 1}"
    )
    path = _write(tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="inkfoot.contracts"):
        contract = load_contract(path)
        load_contract(path)
    assert contract.schema_version == CONTRACT_SCHEMA_VERSION - 1
    deprecation_lines = [
        r for r in caplog.records if "deprecated" in r.getMessage()
    ]
    assert len(deprecation_lines) == 1
