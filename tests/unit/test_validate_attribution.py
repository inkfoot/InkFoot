"""Tests for ``scripts/validate_attribution.py``.

The harness is the current quantitative go/no-go gate; if it can't
catch a regression it can't gate CI. These tests build minimal
corpora in-process and exercise the pass + fail paths.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "validate_attribution.py"


def _load_script_module():
    """Import the script as a module without it being on the path.
    The test still gets to call ``run_validation`` directly."""
    spec = importlib.util.spec_from_file_location(
        "validate_attribution", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_attribution"] = module
    spec.loader.exec_module(module)
    return module


_validate = _load_script_module()


def _write_fixture(
    *,
    corpus_dir: Path,
    name: str,
    provider: str,
    model: str,
    request: dict[str, Any],
    response: dict[str, Any],
) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / name).write_text(
        json.dumps(
            {
                "provider": provider,
                "model": model,
                "request": request,
                "response": response,
            }
        )
    )


def _write_labels(corpus_dir: Path, labels: dict[str, Any]) -> None:
    (corpus_dir / "labels.json").write_text(json.dumps(labels))


def _simple_anthropic(
    corpus_dir: Path, name: str = "fixture.json"
) -> None:
    _write_fixture(
        corpus_dir=corpus_dir,
        name=name,
        provider="anthropic",
        model="claude-sonnet-4-6",
        request={
            "model": "claude-sonnet-4-6",
            "system": "Brief system.",
            "messages": [{"role": "user", "content": "Hi."}],
        },
        response={
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "Hi back."}],
        },
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


def test_run_validation_passes_when_labels_match(tmp_path: Path) -> None:
    """Snapshot the translator's actual output as the label →
    harness reports 0% error and passes."""
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    _simple_anthropic(tmp_path)
    # Compute the "correct" label by running the translator.
    fixture_data = json.loads(
        (tmp_path / "fixture.json").read_text()
    )
    state = InMemoryRunState()
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    expected = {cat: int(getattr(call.ledger, cat)) for cat in INPUT_CATEGORIES}
    _write_labels(
        tmp_path,
        {"fixture.json": {"expected": expected}},
    )

    passed, report = _validate.run_validation(corpus_dir=tmp_path)
    assert passed is True
    assert report["fixture_count"] == 1
    assert all(
        v == 0.0 for v in report["per_category_mean_error"].values()
    )


def test_run_validation_skips_unlabelled_fixtures(tmp_path: Path) -> None:
    _simple_anthropic(tmp_path, name="labelled.json")
    _simple_anthropic(tmp_path, name="unlabelled.json")
    # Only label one.
    from inkfoot.ledger import INPUT_CATEGORIES

    _write_labels(
        tmp_path,
        {
            "labelled.json": {
                "expected": {cat: 0 for cat in INPUT_CATEGORIES}
            }
        },
    )

    _passed, report = _validate.run_validation(corpus_dir=tmp_path)
    assert report["fixture_count"] == 1
    assert "unlabelled.json" in report["skipped_fixtures"]


# ----------------------------------------------------------------------
# The harness must actually catch regressions
# ----------------------------------------------------------------------


def test_run_validation_fails_when_label_disagrees_beyond_threshold(
    tmp_path: Path,
) -> None:
    """Inflate one expected count by 100% → mean error in that
    category is 100% > 10% threshold → harness fails."""
    _simple_anthropic(tmp_path)
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    fixture_data = json.loads((tmp_path / "fixture.json").read_text())
    state = InMemoryRunState()
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    expected = {cat: int(getattr(call.ledger, cat)) for cat in INPUT_CATEGORIES}
    # Sabotage system_static: claim 2× the actual.
    expected["system_static_tokens"] = max(2, call.ledger.system_static_tokens * 2)

    _write_labels(tmp_path, {"fixture.json": {"expected": expected}})

    passed, report = _validate.run_validation(corpus_dir=tmp_path)
    assert passed is False
    assert "system_static_tokens" in report["failing_categories"]


def test_run_validation_respects_custom_threshold(tmp_path: Path) -> None:
    """A small mismatch passes at 0.20 but fails at 0.01.

    Use an absolute +1 offset rather than a multiplicative one so
    the integer rounding doesn't swallow the lie on small token
    counts (the simple fixture's user_input is a handful of tokens;
    ``int(2 × 1.15) == 2``)."""
    _simple_anthropic(tmp_path)
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    fixture_data = json.loads((tmp_path / "fixture.json").read_text())
    state = InMemoryRunState()
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=state,
        started_at=0,
        ended_at=1,
    )
    expected = {cat: int(getattr(call.ledger, cat)) for cat in INPUT_CATEGORIES}
    actual = call.ledger.user_input_tokens
    if actual == 0:
        pytest.skip("fixture produced zero user_input_tokens")
    # Add 1 extra expected token so the absolute mismatch is 1.
    # Relative error = 1 / max(1, expected) — on the simple fixture
    # this lands around 50% (user_input ≈ 2), well above a 1%
    # threshold and below 100%.
    expected["user_input_tokens"] = actual + 1
    _write_labels(tmp_path, {"fixture.json": {"expected": expected}})

    # Tight bar: fail.
    passed_tight, _ = _validate.run_validation(
        corpus_dir=tmp_path, threshold=0.01
    )
    assert passed_tight is False
    # Loose bar (100%): pass.
    passed_loose, _ = _validate.run_validation(
        corpus_dir=tmp_path, threshold=1.5
    )
    assert passed_loose is True


# ----------------------------------------------------------------------
# Failure modes
# ----------------------------------------------------------------------


def test_missing_corpus_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        _validate.run_validation(corpus_dir=tmp_path / "nope")


def test_empty_corpus_raises(tmp_path: Path) -> None:
    """A corpus with a labels file but no fixtures is an error —
    silently passing on an empty corpus would let a CI breakage
    slip past."""
    _write_labels(tmp_path, {})
    with pytest.raises(RuntimeError, match="no fixture"):
        _validate.run_validation(corpus_dir=tmp_path)


def test_missing_labels_file_raises(tmp_path: Path) -> None:
    _simple_anthropic(tmp_path)
    with pytest.raises(RuntimeError, match="no labels"):
        _validate.run_validation(corpus_dir=tmp_path)


def test_unknown_provider_in_fixture_raises(tmp_path: Path) -> None:
    _write_fixture(
        corpus_dir=tmp_path,
        name="bad.json",
        provider="not-a-real-provider",
        model="x",
        request={"messages": []},
        response={"usage": {}},
    )
    _write_labels(tmp_path, {"bad.json": {"expected": {}}})
    with pytest.raises(ValueError, match="unknown provider"):
        _validate.run_validation(corpus_dir=tmp_path)


# ----------------------------------------------------------------------
# Shipped starter corpus passes
# ----------------------------------------------------------------------


def test_shipped_starter_corpus_passes() -> None:
    """The corpus committed under tests/fixtures/validation/ must
    pass at the default 10% threshold. A CI failure here means the
    translators drifted from the snapshotted labels — investigate
    before re-snapshotting."""
    corpus = _REPO_ROOT / "tests" / "fixtures" / "validation"
    if not corpus.exists():
        pytest.skip("starter corpus not present")
    passed, report = _validate.run_validation(corpus_dir=corpus)
    assert passed is True
    # And the ground-truth subset must be non-empty — the corpus needs
    # at least one fixture whose labels are derived from
    # raw text + provider usage, not snapshotted from the translator.
    assert report["ground_truth_fixture_count"] >= 1


# ----------------------------------------------------------------------
# Ground-truth fixture handling
# ----------------------------------------------------------------------


def test_ground_truth_fixtures_tracked_separately(tmp_path: Path) -> None:
    """Two fixtures: one snapshot-labelled, one marked
    ``ground_truth: true``. Both contribute to the full-corpus
    aggregate; the GT bucket only counts the second."""
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    _simple_anthropic(tmp_path, name="snapshot.json")
    _simple_anthropic(tmp_path, name="ground-truth.json")

    fixture_data = json.loads((tmp_path / "snapshot.json").read_text())
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    snapshot_label = {cat: int(getattr(call.ledger, cat)) for cat in INPUT_CATEGORIES}
    _write_labels(
        tmp_path,
        {
            "snapshot.json": {"expected": snapshot_label},
            "ground-truth.json": {
                "expected": snapshot_label,
                "ground_truth": True,
            },
        },
    )
    passed, report = _validate.run_validation(corpus_dir=tmp_path)
    assert passed is True
    assert report["ground_truth_fixture_count"] == 1
    assert report["ground_truth_fixtures"] == ["ground-truth.json"]
    # Headline + ground-truth means both at 0%.
    assert all(
        v == 0.0 for v in report["per_category_mean_error"].values()
    )
    assert all(
        v == 0.0
        for v in report["ground_truth_per_category_mean_error"].values()
    )


def test_ground_truth_failure_surfaces_independently(tmp_path: Path) -> None:
    """A ground-truth fixture whose label disagrees with the
    translator must fail CI even if no snapshot-labelled fixture
    sees the same disagreement."""
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    _simple_anthropic(tmp_path)
    fixture_data = json.loads((tmp_path / "fixture.json").read_text())
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    expected = {cat: int(getattr(call.ledger, cat)) for cat in INPUT_CATEGORIES}
    # Sabotage system_static by claiming twice the actual count —
    # this label is "ground truth" so the harness must fail even
    # though the translator's output would match its own snapshot.
    expected["system_static_tokens"] = max(2, call.ledger.system_static_tokens * 2)
    _write_labels(
        tmp_path,
        {"fixture.json": {"expected": expected, "ground_truth": True}},
    )
    passed, report = _validate.run_validation(corpus_dir=tmp_path)
    assert passed is False
    assert (
        "system_static_tokens"
        in report["failing_ground_truth_categories"]
    )


def test_shipped_ground_truth_fixture_uses_tiktoken_label_provenance() -> None:
    """The labels.json entry for the shipped ground-truth fixture
    must declare itself as ground-truth so the harness counts it
    separately. Catches a future re-snapshot script that
    accidentally erases the flag."""
    corpus = _REPO_ROOT / "tests" / "fixtures" / "validation"
    if not corpus.exists():
        pytest.skip("starter corpus not present")
    labels = json.loads((corpus / "labels.json").read_text())
    gt = labels.get("anthropic-ground-truth-tiktoken.json")
    assert gt is not None, (
        "expected anthropic-ground-truth-tiktoken.json in labels.json"
    )
    assert gt.get("ground_truth") is True
    # The label_source string explains how to verify the counts
    # independently. If a future contributor re-snapshots from the
    # translator without realising it, this string is the safety
    # net that calls attention to the override.
    assert "tiktoken" in (gt.get("label_source") or "").lower()


# ----------------------------------------------------------------------
# --report-json flag
# ----------------------------------------------------------------------


def test_main_report_json_flag_writes_file_and_prints_human_report(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--report-json PATH`` writes the JSON to disk AND still
    prints the human-friendly report to stdout — one invocation
    serves both jobs (CI used to run the harness twice)."""
    _simple_anthropic(tmp_path)
    from inkfoot.ledger import INPUT_CATEGORIES
    from inkfoot.normalise.anthropic import AnthropicTranslator
    from inkfoot.run import InMemoryRunState

    fixture_data = json.loads((tmp_path / "fixture.json").read_text())
    call = AnthropicTranslator().translate(
        request=fixture_data["request"],
        response=fixture_data["response"],
        run_state=InMemoryRunState(),
        started_at=0,
        ended_at=1,
    )
    _write_labels(
        tmp_path,
        {
            "fixture.json": {
                "expected": {
                    cat: int(getattr(call.ledger, cat))
                    for cat in INPUT_CATEGORIES
                }
            }
        },
    )

    report_path = tmp_path / "report.json"
    rc = _validate.main(
        [
            "--corpus",
            str(tmp_path),
            "--report-json",
            str(report_path),
        ]
    )
    assert rc == 0
    # JSON file written.
    assert report_path.exists()
    payload = json.loads(report_path.read_text())
    assert payload["passed"] is True
    # Human report still printed to stdout.
    captured = capsys.readouterr()
    assert "Validation corpus" in captured.out
    assert "PASS" in captured.out


# ----------------------------------------------------------------------
# Per-category error semantics
# ----------------------------------------------------------------------


def test_per_category_error_relative_with_zero_expected_and_zero_actual() -> None:
    e = _validate.PerCategoryError(
        fixture="x", category="user_input_tokens", expected=0, actual=0
    )
    assert e.relative == 0.0


def test_per_category_error_relative_with_zero_expected_nonzero_actual() -> None:
    """Zero-expected non-zero-actual surfaces a finding (divisor
    clamped at 1 so we don't lose the signal)."""
    e = _validate.PerCategoryError(
        fixture="x", category="user_input_tokens", expected=0, actual=5
    )
    assert e.relative == 5.0


def test_per_category_error_relative_typical_case() -> None:
    e = _validate.PerCategoryError(
        fixture="x", category="user_input_tokens", expected=100, actual=90
    )
    assert e.relative == 0.10
