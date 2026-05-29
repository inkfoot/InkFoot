"""Unit tests for ``inkfoot.diff.render_markdown`` + ``render_json``.

The Markdown renderer is snapshot-tested against a curated fixture
artefact so byte-for-byte regressions are caught immediately.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from inkfoot.benchmark.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkArtifact,
    ScenarioResult,
    SmellCount,
)
from inkfoot.diff.compare import compare_artifacts
from inkfoot.diff.render_json import render_json
from inkfoot.diff.render_markdown import STICKY_COMMENT_MARKER, render_markdown


_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "diff"


def _scenario(
    task: str = "demo",
    *,
    runs: int = 4,
    successes: int = 4,
    p50: int = 1_000_000,
    p95: int = 2_000_000,
    mean_calls: float = 2.0,
    cache_rate: float = 0.5,
    smells=(),
) -> ScenarioResult:
    return ScenarioResult(
        task=task,
        runs=runs,
        successes=successes,
        p50_nanodollars=p50,
        p95_nanodollars=p95,
        mean_llm_calls=mean_calls,
        mean_cache_hit_rate=cache_rate,
        smells_seen=tuple(smells),
    )


def _artifact(*scenarios: ScenarioResult, version: str = "1.0.0") -> BenchmarkArtifact:
    return BenchmarkArtifact(
        inkfoot_version=version,
        schema_version=BENCHMARK_SCHEMA_VERSION,
        captured_at="2026-05-25T12:00:00Z",
        scenarios=tuple(scenarios),
    )


def test_markdown_includes_sticky_comment_marker_by_default():
    baseline = _artifact(_scenario())
    current = _artifact(_scenario(p50=1_500_000))
    report = compare_artifacts(baseline, current)
    md = render_markdown(report)
    assert STICKY_COMMENT_MARKER in md


def test_markdown_omits_marker_when_include_marker_is_false():
    baseline = _artifact(_scenario())
    current = _artifact(_scenario())
    md = render_markdown(compare_artifacts(baseline, current), include_marker=False)
    assert STICKY_COMMENT_MARKER not in md


def test_markdown_matches_snapshot_fixture():
    baseline = _artifact(
        _scenario(task="customer-support-triage", p50=41_000_000, p95=83_000_000),
        _scenario(task="email-summary", p50=10_000_000, p95=20_000_000),
    )
    current = _artifact(
        _scenario(task="customer-support-triage", p50=62_000_000, p95=125_000_000),
        _scenario(
            task="email-summary",
            p50=11_000_000,
            p95=21_000_000,
            smells=[SmellCount(id="unstable-prompt-prefix", count=2)],
        ),
    )
    report = compare_artifacts(baseline, current)
    md = render_markdown(report)
    snapshot_path = _FIXTURE_DIR / "diff_snapshot.md"
    # Only write the snapshot when the maintainer explicitly opts in.
    # The old "create if missing" branch silently regenerated the
    # snapshot when the file was deleted (intentionally or in a bad
    # rebase), defeating the byte-for-byte guarantee from the spec.
    if "INKFOOT_UPDATE_SNAPSHOTS" in os.environ:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(md, encoding="utf-8")
    assert snapshot_path.exists(), (
        f"snapshot fixture missing at {snapshot_path}. Re-run with "
        f"INKFOOT_UPDATE_SNAPSHOTS=1 if the renderer output changed "
        f"intentionally, then review the diff before committing."
    )
    assert md == snapshot_path.read_text(encoding="utf-8")


def test_render_json_is_parseable_and_matches_to_dict():
    baseline = _artifact(_scenario())
    current = _artifact(_scenario(p50=1_500_000))
    report = compare_artifacts(baseline, current)
    rendered = render_json(report)
    assert json.loads(rendered) == report.to_dict()


def test_render_json_compact_mode_is_single_line():
    baseline = _artifact(_scenario())
    current = _artifact(_scenario())
    rendered = render_json(compare_artifacts(baseline, current), indent=None)
    assert "\n" not in rendered.strip()


def test_markdown_new_scenario_row_uses_em_dash():
    baseline = _artifact()
    current = _artifact(_scenario(task="brand-new"))
    md = render_markdown(compare_artifacts(baseline, current))
    assert "(new)" in md
    assert "—" in md


def test_markdown_emits_smell_changes_block_when_present():
    baseline = _artifact(_scenario())
    current = _artifact(
        _scenario(
            smells=[SmellCount(id="unstable-prompt-prefix", count=2)],
        )
    )
    md = render_markdown(compare_artifacts(baseline, current))
    assert "### Smell changes" in md
    assert "unstable-prompt-prefix" in md
