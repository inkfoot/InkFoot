#!/usr/bin/env python3
"""Validation harness for per-category attribution accuracy.

Loads the hand-labelled corpus under ``tests/fixtures/validation/``,
runs the matching per-provider translator against each fixture's
request + response, and compares the resulting :class:`CausalTokenLedger`
to the ground-truth labels in ``labels.yaml``. Computes per-category
mean relative error; fails the CI gate when mean error > 10%.

This is the attribution gate's quantitative half. Every
PR runs this; a regression in any translator that pushes a category
past 10% blocks the build.

Usage::

    python scripts/validate_attribution.py
    python scripts/validate_attribution.py --corpus tests/fixtures/validation/
    python scripts/validate_attribution.py --json   # machine-readable output

Exit codes: 0 = pass, 1 = some category over threshold, 2 = usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

# Allow ``python scripts/validate_attribution.py`` from the repo
# root without an ``inkfoot`` install.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from inkfoot.ledger import INPUT_CATEGORIES  # noqa: E402
from inkfoot.normalise.anthropic import AnthropicTranslator  # noqa: E402
from inkfoot.normalise.openai import OpenAITranslator  # noqa: E402
from inkfoot.run import InMemoryRunState  # noqa: E402

# Per-category mean-relative-error threshold (§5.3 invariant).
# The current implementation ships at 10%; the underlying ledger.validate_against_usage
# uses 2% for the input-total sum; the harder per-category bar is
# 10% because individual categories drift more than the aggregate.
DEFAULT_THRESHOLD = 0.10


_LOG = logging.getLogger("inkfoot.validate")


@dataclasses.dataclass(frozen=True)
class PerCategoryError:
    """One fixture's relative error in one ledger category."""

    fixture: str
    category: str
    expected: int
    actual: int

    @property
    def relative(self) -> float:
        """``|actual - expected| / max(1, expected)``.

        Skipping the divisor's zero would special-case the
        common "this category isn't used in this fixture" path; we
        use ``max(1, expected)`` so a zero-expected non-zero-actual
        finding still shows up (rare, but worth catching)."""
        if self.expected == 0 and self.actual == 0:
            return 0.0
        return abs(self.actual - self.expected) / max(1, self.expected)


def _translator_for(provider: str):
    """Map provider name → translator. Unknown providers are an
    early-exit error rather than a silent skip — a fixture labelled
    with the wrong provider would otherwise pass the validation
    check trivially."""
    if provider == "anthropic":
        return AnthropicTranslator()
    if provider == "openai":
        return OpenAITranslator()
    raise ValueError(
        f"validate_attribution: unknown provider {provider!r}; "
        f"expected 'anthropic' or 'openai'"
    )


def _load_labels(corpus_dir: Path) -> dict[str, dict[str, Any]]:
    """Read ``labels.yaml`` from the corpus. We hand-roll a tiny
    YAML loader (PyYAML isn't in the runtime deps yet) — the labels
    file is strictly key/value-of-int with simple structure, so a
    full YAML parser is overkill.

    Actually: we *do* require PyYAML when running the script. Falls
    back to a JSON-mirror file if PyYAML isn't installed."""
    yaml_path = corpus_dir / "labels.yaml"
    json_path = corpus_dir / "labels.json"
    if yaml_path.exists():
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "validate_attribution: labels.yaml found but PyYAML is "
                "not installed. Install with `pip install pyyaml`, or "
                "ship a labels.json file alongside the YAML."
            ) from exc
        with yaml_path.open() as fh:
            data = yaml.safe_load(fh) or {}
    elif json_path.exists():
        with json_path.open() as fh:
            data = json.load(fh)
    else:
        raise RuntimeError(
            f"validate_attribution: no labels.yaml or labels.json in "
            f"{corpus_dir}"
        )
    if not isinstance(data, dict):
        raise RuntimeError(
            f"validate_attribution: labels file must be a mapping, "
            f"got {type(data).__name__}"
        )
    return data


def _load_fixture(path: Path) -> dict[str, Any]:
    """Read a single fixture file. Format: a JSON object with keys
    ``provider``, ``model``, ``request``, ``response`` (each a
    dict). The translator consumes ``request`` + ``response`` the
    way it would in production."""
    with path.open() as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"validate_attribution: fixture {path} must be a mapping"
        )
    for required in ("provider", "model", "request", "response"):
        if required not in data:
            raise RuntimeError(
                f"validate_attribution: fixture {path} missing required "
                f"key {required!r}"
            )
    return data


def _run_one_fixture(
    *, fixture_path: Path, label: dict[str, Any]
) -> list[PerCategoryError]:
    """Translate one fixture and compute per-category errors against
    its label. Returns one :class:`PerCategoryError` per
    :data:`INPUT_CATEGORIES`."""
    data = _load_fixture(fixture_path)
    provider = data["provider"]
    translator = _translator_for(provider)
    state = InMemoryRunState()
    call = translator.translate(
        request=data["request"],
        response=data["response"],
        run_state=state,
        started_at=0,
        ended_at=1,
    )

    expected = label.get("expected", {})
    if not isinstance(expected, dict):
        raise RuntimeError(
            f"validate_attribution: label for {fixture_path.name} must "
            f"have an 'expected' mapping"
        )

    out: list[PerCategoryError] = []
    for category in INPUT_CATEGORIES:
        actual = int(getattr(call.ledger, category, 0))
        expected_val = int(expected.get(category, 0))
        out.append(
            PerCategoryError(
                fixture=fixture_path.name,
                category=category,
                expected=expected_val,
                actual=actual,
            )
        )
    return out


def run_validation(
    *,
    corpus_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[bool, dict[str, Any]]:
    """Run validation across the entire corpus.

    Returns ``(passed, report_dict)``. ``passed`` is ``True`` iff
    every category's *mean relative error* across the corpus is
    under ``threshold``. The report dict is JSON-serialisable and
    suitable for both CI output and `--json` mode.

    Ground-truth fixtures (those whose label entry sets
    ``ground_truth: true``) are counted in the headline aggregate
    AND tracked in a separate ``ground_truth_per_category_mean_error``
    bucket. That bucket is the load-bearing check — labels are
    derived from raw text + provider usage, not snapshotted from
    the translator — so a translator regression that the snapshot-
    based labels would hide still surfaces here.
    """
    if not corpus_dir.exists():
        raise RuntimeError(
            f"validate_attribution: corpus directory {corpus_dir} does "
            f"not exist"
        )
    labels = _load_labels(corpus_dir)
    fixtures = sorted(corpus_dir.glob("*.json"))
    # Exclude ``labels.json`` from the fixture list — it's not a fixture.
    fixtures = [p for p in fixtures if p.name not in {"labels.json"}]

    if not fixtures:
        raise RuntimeError(
            f"validate_attribution: no fixture JSON files in {corpus_dir}"
        )

    all_errors: list[PerCategoryError] = []
    ground_truth_errors: list[PerCategoryError] = []
    skipped: list[str] = []
    ground_truth_fixtures: list[str] = []
    for path in fixtures:
        label = labels.get(path.name)
        if label is None:
            skipped.append(path.name)
            continue
        errors = _run_one_fixture(fixture_path=path, label=label)
        all_errors.extend(errors)
        if isinstance(label, dict) and label.get("ground_truth") is True:
            ground_truth_errors.extend(errors)
            ground_truth_fixtures.append(path.name)

    # Aggregate: per-category mean relative error across the corpus.
    per_category_mean = _mean_per_category(all_errors)
    ground_truth_mean = _mean_per_category(ground_truth_errors)

    failing = {
        cat: err
        for cat, err in per_category_mean.items()
        if err > threshold
    }
    # Ground-truth failures are reported independently so a
    # snapshot-passing-but-ground-truth-failing run surfaces both
    # facts in the same report.
    failing_ground_truth = {
        cat: err
        for cat, err in ground_truth_mean.items()
        if err > threshold
    }
    passed = not failing and not failing_ground_truth
    report = {
        "corpus_dir": str(corpus_dir),
        "fixture_count": len(fixtures) - len(skipped),
        "ground_truth_fixture_count": len(ground_truth_fixtures),
        "ground_truth_fixtures": ground_truth_fixtures,
        "skipped_fixtures": skipped,
        "threshold": threshold,
        "per_category_mean_error": per_category_mean,
        "ground_truth_per_category_mean_error": ground_truth_mean,
        "failing_categories": failing,
        "failing_ground_truth_categories": failing_ground_truth,
        "passed": passed,
    }
    return passed, report


def _mean_per_category(
    errors: list[PerCategoryError],
) -> dict[str, float]:
    """Per-category mean relative error across the given list. A
    category with no errors lands at 0.0 — there's nothing to
    measure."""
    out: dict[str, float] = {}
    for category in INPUT_CATEGORIES:
        cat_errors = [e for e in errors if e.category == category]
        if not cat_errors:
            out[category] = 0.0
            continue
        out[category] = sum(e.relative for e in cat_errors) / len(cat_errors)
    return out


def _render_human_report(report: dict[str, Any]) -> str:
    lines = [
        f"Validation corpus: {report['corpus_dir']}",
        f"Fixtures evaluated: {report['fixture_count']}"
        f"  (ground-truth: {report['ground_truth_fixture_count']})",
    ]
    if report["skipped_fixtures"]:
        lines.append(
            f"Skipped (no label): {len(report['skipped_fixtures'])} fixture(s)"
        )
    lines.append(
        f"Per-category mean error threshold: "
        f"{report['threshold'] * 100:.1f}%"
    )
    lines.append("")
    lines.append("Per-category mean error (full corpus):")
    per_cat = report["per_category_mean_error"]
    for category in INPUT_CATEGORIES:
        err = per_cat.get(category, 0.0)
        marker = "❌" if err > report["threshold"] else "✓"
        lines.append(f"  {marker} {category:<32} {err * 100:6.2f}%")

    if report["ground_truth_fixture_count"] > 0:
        lines.append("")
        lines.append(
            "Per-category mean error (ground-truth subset only — "
            "labels independent of translator):"
        )
        gt_per_cat = report["ground_truth_per_category_mean_error"]
        for category in INPUT_CATEGORIES:
            err = gt_per_cat.get(category, 0.0)
            marker = "❌" if err > report["threshold"] else "✓"
            lines.append(f"  {marker} {category:<32} {err * 100:6.2f}%")

    failed_full = bool(report["failing_categories"])
    failed_gt = bool(report["failing_ground_truth_categories"])
    if failed_full or failed_gt:
        lines.append("")
        if failed_full:
            lines.append(
                f"FAIL: {len(report['failing_categories'])} categor(ies) "
                f"exceed the {report['threshold'] * 100:.1f}% threshold "
                f"on the full corpus."
            )
        if failed_gt:
            lines.append(
                f"FAIL: {len(report['failing_ground_truth_categories'])} "
                f"categor(ies) exceed the {report['threshold'] * 100:.1f}% "
                f"threshold on the ground-truth subset."
            )
    else:
        lines.append("")
        lines.append("PASS: every category under threshold (incl. ground-truth).")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_attribution",
        description=(
            "Run per-category attribution accuracy validation against "
            "the hand-labelled corpus."
        ),
    )
    parser.add_argument(
        "--corpus",
        default=str(_REPO_ROOT / "tests" / "fixtures" / "validation"),
        help="Path to the corpus directory (default: tests/fixtures/validation).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Per-category mean-error threshold (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout instead of the "
            "human-friendly report."
        ),
    )
    parser.add_argument(
        "--report-json",
        default=None,
        metavar="PATH",
        help=(
            "Write the machine-readable JSON report to ``PATH`` while "
            "still printing the human-friendly report to stdout. "
            "Lets CI emit both formats in one harness invocation."
        ),
    )
    args = parser.parse_args(argv)

    try:
        passed, report = run_validation(
            corpus_dir=Path(args.corpus),
            threshold=args.threshold,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    json_blob = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        # ``--json`` mode: machine-readable to stdout, nothing else.
        print(json_blob)
    else:
        print(_render_human_report(report))

    # ``--report-json PATH`` writes the JSON regardless of the
    # ``--json`` flag — CI uses this to upload an artefact in the
    # same step that gates the build.
    if args.report_json:
        Path(args.report_json).write_text(json_blob)

    return 0 if passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
