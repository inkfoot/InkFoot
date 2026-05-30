"""Unit tests for ``inkfoot.diff.thresholds``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkfoot.diff.thresholds import (
    DEFAULT_THRESHOLD_NAME,
    THRESHOLD_PRESETS,
    Thresholds,
    ThresholdsError,
    load_thresholds,
)


def test_default_preset_matches_documented_contract():
    t = THRESHOLD_PRESETS["default"]
    assert t.cost_warn == pytest.approx(0.20)
    assert t.cost_fail == pytest.approx(0.50)
    assert t.cache_warn == pytest.approx(0.10)
    assert t.cache_fail == pytest.approx(0.25)


def test_load_thresholds_returns_default_when_none():
    assert load_thresholds(None) is THRESHOLD_PRESETS[DEFAULT_THRESHOLD_NAME]


def test_load_thresholds_returns_preset_by_name():
    assert load_thresholds("tight") is THRESHOLD_PRESETS["tight"]
    assert load_thresholds("loose") is THRESHOLD_PRESETS["loose"]


def test_load_thresholds_raises_on_unknown_name():
    with pytest.raises(ThresholdsError, match="unknown threshold preset"):
        load_thresholds("nonsense")


def test_load_thresholds_reads_json_file(tmp_path: Path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "cost_warn": 0.01,
                "cost_fail": 0.05,
                "cache_warn": 0.01,
                "cache_fail": 0.05,
                "critical_smells": ["recurring-cache-writes"],
            }
        )
    )
    t = load_thresholds(str(path))
    assert t.name == "policy"
    assert t.cost_fail == pytest.approx(0.05)
    assert t.critical_smells == ("recurring-cache-writes",)


def test_thresholds_rejects_warn_above_fail():
    with pytest.raises(ThresholdsError):
        Thresholds(
            name="bad",
            cost_warn=0.5,
            cost_fail=0.2,
            cache_warn=0.1,
            cache_fail=0.2,
        )


def test_thresholds_rejects_negative_values():
    with pytest.raises(ThresholdsError):
        Thresholds(
            name="neg",
            cost_warn=-0.1,
            cost_fail=0.2,
            cache_warn=0.1,
            cache_fail=0.2,
        )
