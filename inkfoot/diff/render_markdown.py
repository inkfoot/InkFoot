"""Markdown rendering for the ``inkfoot diff`` PR comment.

Output structure:

1. Verdict header (`✅ ok` / `⚠️ warn` / `❌ fail`) with thresholds preset name.
2. Per-scenario table — task, p50 delta, p95 delta, cache delta,
   calls delta, verdict.
3. Regressions section listing the per-scenario reasons.
4. Smell deltas (appeared / disappeared / changed) — only emitted
   when there is something to say.
5. A footnote with the captured-at timestamps and the inkfoot
   version on each side.

The renderer is a pure function — no I/O, no clock reads. Snapshot
tests assert byte-for-byte equality against a fixture; rendering
glue (sticky comment marker etc.) is the action's responsibility.
"""

from __future__ import annotations

from inkfoot.diff.compare import DiffReport, ScenarioDiff, SmellDelta, Verdict


# Hidden HTML marker that the sticky-comment logic uses to identify
# its own previous comment on subsequent runs. Renders to nothing in
# the rendered Markdown but survives raw-comment storage.
STICKY_COMMENT_MARKER = "<!-- inkfoot-diff-action -->"


_VERDICT_BADGE: dict[Verdict, str] = {
    Verdict.OK: "✅ ok",
    Verdict.WARN: "⚠️ warn",
    Verdict.FAIL: "❌ fail",
}


def render_markdown(report: DiffReport, *, include_marker: bool = True) -> str:
    """Render ``report`` as the PR-comment markdown.

    ``include_marker`` flips the hidden HTML marker on; the diff
    CLI keeps it on by default so the GH action can find prior
    comments, but tests can flip it off to assert against a clean
    fixture.
    """
    lines: list[str] = []
    if include_marker:
        lines.append(STICKY_COMMENT_MARKER)
    lines.append(
        f"## Inkfoot cost diff · {_VERDICT_BADGE[report.verdict]}"
    )
    lines.append("")
    lines.append(
        f"_Thresholds preset: **{report.thresholds.name}** · "
        f"baseline {report.baseline_captured_at} → "
        f"current {report.current_captured_at}_"
    )
    lines.append("")

    if not report.scenario_diffs:
        lines.append("No scenarios in either artefact.")
        return "\n".join(lines) + "\n"

    lines.append("| Scenario | p50 Δ | p95 Δ | cache hit Δ | LLM calls Δ | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for sd in report.scenario_diffs:
        lines.append(_table_row(sd))
    lines.append("")

    regressions = [
        sd for sd in report.scenario_diffs if sd.verdict is not Verdict.OK
    ]
    if regressions:
        lines.append("### Regressions")
        lines.append("")
        for sd in regressions:
            lines.append(f"- **{sd.task}** — {_VERDICT_BADGE[sd.verdict]}")
            for reason in sd.reasons:
                lines.append(f"  - {reason}")
        lines.append("")

    smell_changes = [
        (sd.task, sd.smell_deltas)
        for sd in report.scenario_diffs
        if sd.smell_deltas
    ]
    if smell_changes:
        lines.append("### Smell changes")
        lines.append("")
        for task, deltas in smell_changes:
            for delta in deltas:
                lines.append(
                    f"- **{task}** — `{delta.id}`: "
                    f"{_describe(delta)}"
                )
        lines.append("")

    lines.append(
        f"_Inkfoot baseline `{report.baseline_version}` → "
        f"current `{report.current_version}`._"
    )
    return "\n".join(lines) + "\n"


def _table_row(sd: ScenarioDiff) -> str:
    """One Markdown table row.

    Renders ``—`` for axes the diff can't express (new / removed
    scenarios) so the table doesn't show "+inf%" or "nan".
    """
    if sd.baseline is None and sd.current is not None:
        return (
            f"| {sd.task} (new) | — | — | — | — | "
            f"{_VERDICT_BADGE[sd.verdict]} |"
        )
    if sd.current is None and sd.baseline is not None:
        return (
            f"| {sd.task} (removed) | — | — | — | — | "
            f"{_VERDICT_BADGE[sd.verdict]} |"
        )
    return (
        f"| {sd.task} | "
        f"{_fmt_pct(sd.p50_delta_fraction)} | "
        f"{_fmt_pct(sd.p95_delta_fraction)} | "
        f"{_fmt_pp(sd.cache_hit_delta)} | "
        f"{_fmt_decimal(sd.llm_calls_delta)} | "
        f"{_VERDICT_BADGE[sd.verdict]} |"
    )


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val * 100:.1f}%"


def _fmt_pp(val: float | None) -> str:
    if val is None:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val * 100:.1f}pp"


def _fmt_decimal(val: float | None) -> str:
    if val is None:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}"


def _describe(delta: SmellDelta) -> str:
    if delta.status == "appeared":
        return f"appeared ({delta.current_count} runs affected)"
    if delta.status == "disappeared":
        return f"resolved (was {delta.baseline_count} runs)"
    if delta.status == "increased":
        return f"{delta.baseline_count} → {delta.current_count} runs"
    if delta.status == "decreased":
        return f"{delta.baseline_count} → {delta.current_count} runs"
    return f"{delta.current_count} runs (unchanged)"
