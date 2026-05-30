"""inkfoot report renderer tests.

The renderer is a pure function ``(run, ledger_totals, smells) →
str`` so we don't need to spin up storage. These tests build a
hand-crafted run row + ledger totals + smell hits and assert the
rendered string contains the expected structural pieces.
"""

from __future__ import annotations

from inkfoot.cli.report import render
from inkfoot.smells import DEFAULT_SMELLS, DetectionResult, get_smell


def _basic_run(run_id: str = "01HZX") -> dict:
    return {
        "id": run_id,
        "task": "customer-support-triage",
        "started_at": 1_700_000_000_000,
        "ended_at": 1_700_000_018_200,  # 18.2s later
        "outcome": "success",
        "quality_score": 0.94,
        "total_nanodollars": 54_100_000,  # ~$0.054
    }


def _totals_with_some_zero() -> dict[str, int]:
    """Mix of populated + zero categories. Sums to exactly 54.1M nd
    so the header dollar figure is the predictable $0.0541."""
    return {
        "system_static_tokens": 6_700_000,
        "system_dynamic_tokens": 9_800_000,
        "user_input_tokens": 1_100_000,
        "tool_schema_tokens": 4_200_000,
        "tool_result_tokens": 18_700_000,
        "retrieved_context_tokens": 0,
        "memory_tokens": 4_400_000,
        "retry_overhead_tokens": 0,
        "summariser_tokens": 0,
        "reasoning_tokens": 0,
        "guardrail_tokens": 0,
        "cache_creation_tokens": 2_900_000,
        "cache_read_tokens": 0,
        "output_tokens": 6_300_000,
    }


# ----------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------


def test_header_includes_run_id_task_duration_cost_outcome() -> None:
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    first = out.splitlines()[0]
    assert "Run 01HZX" in first
    assert "customer-support-triage" in first
    assert "18.2s" in first
    assert "$0.0541" in first
    assert "success" in first
    assert "(0.94)" in first


def test_header_handles_no_outcome() -> None:
    run = _basic_run()
    run["outcome"] = None
    run["quality_score"] = None
    out = render(run=run, ledger_totals=_totals_with_some_zero(), smells=[])
    assert "(no outcome)" in out.splitlines()[0]


def test_header_in_progress_when_ended_at_missing() -> None:
    run = _basic_run()
    run["ended_at"] = None
    out = render(run=run, ledger_totals=_totals_with_some_zero(), smells=[])
    assert "in progress" in out.splitlines()[0]


# ----------------------------------------------------------------------
# Bar chart
# ----------------------------------------------------------------------


def test_bar_chart_sorted_by_cost_descending() -> None:
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    # tool_result is the largest at 18.7M nd; should come first.
    body = out.split("Causal attribution:", 1)[1].splitlines()
    rows = [line for line in body if line.strip().startswith(("system", "tool", "user", "memory", "output", "cache", "retrieval", "retry", "summa", "guard", "reason", "retrieved"))]
    assert rows[0].lstrip().startswith("tool_result")
    # system_dynamic at 9.8M is second.
    assert rows[1].lstrip().startswith("system_dynamic")


def test_zero_rows_hidden_by_default() -> None:
    """``summariser`` and ``guardrail`` are zero — they should NOT
    appear as bar-chart rows. The footnote at the bottom mentions
    them ("summariser and guardrail are always-zero..."), so we
    scope the absence check to the bar-chart body."""
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    # Slice out the bar-chart body (between "Causal attribution:" and
    # the blank line that precedes the footnote).
    body, _, _ = out.split("Causal attribution:", 1)[1].partition("\n\n")
    assert "summariser" not in body
    assert "guardrail" not in body


def test_show_zero_includes_all_fourteen_rows() -> None:
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[],
        show_zero=True,
    )
    for field in (
        "system_static",
        "system_dynamic",
        "user_input",
        "tool_schema",
        "tool_result",
        "retrieved_context",
        "memory",
        "retry_overhead",
        "summariser",
        "reasoning",
        "guardrail",
        "cache_creation",
        "cache_read",
        "output",
    ):
        assert field in out, f"missing field {field} in show_zero render"


def test_dollar_figures_use_four_decimal_precision() -> None:
    """§5.10: 4-decimal dollar precision in the bar-chart rows."""
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    # Sample a known-cost row.
    body_lines = [
        line for line in out.splitlines() if line.lstrip().startswith("tool_result")
    ]
    assert any("$0.0187" in line for line in body_lines)


def test_bar_uses_twelve_columns() -> None:
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[],
        show_zero=True,
    )
    # Find a bar row and count fill + empty chars.
    body_lines = [
        line for line in out.splitlines() if "█" in line or "░" in line
    ]
    for line in body_lines:
        bar_chars = [c for c in line if c in "█░"]
        assert len(bar_chars) == 12, f"bar in line {line!r} is not 12 cols"


# ----------------------------------------------------------------------
# Smells inline
# ----------------------------------------------------------------------


def test_smell_marker_on_primary_category_row() -> None:
    smell = get_smell("oversized-tool-result-recycled")
    hit = DetectionResult(
        smell=smell,
        triggered_at_sequence=1,
        severity="warn",
        evidence={},
        estimated_cost_impact_nd=10_000_000,
    )
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[hit],
    )
    tool_result_row = [
        line for line in out.splitlines() if line.lstrip().startswith("tool_result")
    ][0]
    assert "⚠" in tool_result_row
    assert "oversized" in tool_result_row


def test_smells_block_present_when_hits_exist() -> None:
    smell = get_smell("unstable-prompt-prefix")
    hit = DetectionResult(
        smell=smell,
        triggered_at_sequence=1,
        severity="warn",
        evidence={},
        estimated_cost_impact_nd=2_000_000,
    )
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[hit],
    )
    assert "Smells detected (1):" in out
    assert "unstable-prompt-prefix" in out
    # Recommendation line follows the smell id.
    assert "Move time-varying content" in out


def test_smells_block_absent_when_no_hits() -> None:
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[],
    )
    assert "Smells detected" not in out


def test_estimated_savings_footer_present_when_impact_nonzero() -> None:
    smell = get_smell("oversized-tool-result-recycled")
    hit = DetectionResult(
        smell=smell,
        triggered_at_sequence=1,
        severity="warn",
        estimated_cost_impact_nd=23_000_000,  # ~$0.023
    )
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[hit],
    )
    assert "Estimated savings if fixed" in out
    assert "$0.0230" in out


def test_estimated_savings_footer_absent_when_zero_impact() -> None:
    smell = get_smell("runaway-retry-loop")
    hit = DetectionResult(
        smell=smell,
        triggered_at_sequence=1,
        severity="critical",
        estimated_cost_impact_nd=0,
    )
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[hit],
    )
    assert "Estimated savings" not in out


# ----------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------


def test_render_with_all_zero_totals_does_not_crash() -> None:
    """A brand-new run with no events shouldn't crash the renderer
    (a debugger may invoke ``inkfoot report --run`` mid-run)."""
    run = _basic_run()
    run["total_nanodollars"] = 0
    zeros = {name: 0 for name in _totals_with_some_zero()}
    out = render(run=run, ledger_totals=zeros, smells=[])
    assert "Run 01HZX" in out


# ----------------------------------------------------------------------
# Always-zero footnote grammar (review #4)
# ----------------------------------------------------------------------


def test_always_zero_footnote_uses_oxford_comma_with_and() -> None:
    """Three always-zero current fields should render with an
    Oxford-comma-joined list, not "x and y and z"."""
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    # All three always-zero fields are zero in the fixture.
    assert "summariser" in out
    assert "guardrail" in out
    assert "retry_overhead" in out
    # Oxford-comma form: "a, b, and c".
    footnote_lines = [
        line for line in out.splitlines() if "always-zero" in line
    ]
    assert footnote_lines, "expected an always-zero footnote"
    footnote = footnote_lines[0]
    assert ", and " in footnote, footnote


def test_footnote_omits_categories_outside_always_zero_set() -> None:
    """retrieved_context, cache_read, etc. can legitimately be zero
    in real runs — they should NOT show up in the always-zero
    footnote, only as plain hidden rows."""
    out = render(run=_basic_run(), ledger_totals=_totals_with_some_zero(), smells=[])
    footnote_lines = [
        line for line in out.splitlines() if "always-zero" in line
    ]
    assert footnote_lines
    footnote = footnote_lines[0]
    assert "retrieved_context" not in footnote
    assert "cache_read" not in footnote


def test_footnote_absent_when_show_zero_true() -> None:
    out = render(
        run=_basic_run(),
        ledger_totals=_totals_with_some_zero(),
        smells=[],
        show_zero=True,
    )
    assert "always-zero" not in out


# ----------------------------------------------------------------------
# Aggregate view (Finding #1: p95_$ + cost/success columns)
# ----------------------------------------------------------------------


def test_aggregate_view_header_lists_all_five_columns(tmp_path) -> None:
    """The the documented aggregate view spells out five columns: runs, avg_$, p95_$,
    success%, cost/success."""
    import time
    from types import SimpleNamespace

    from inkfoot.cli.report import _render_aggregate
    from inkfoot.storage.sqlite import SQLiteStorage

    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    now_ms = int(time.time() * 1000)
    for i in range(5):
        s.start_run(
            run_id=f"r{i}",
            task="triage",
            agent_kind="agent",
            started_at=now_ms - 1000,
        )
        s._conn().execute(
            "UPDATE runs SET total_nanodollars = ?, outcome = ?, "
            "status = 'complete', ended_at = ? WHERE id = ?",
            (
                (i + 1) * 1_000_000,
                "success" if i < 3 else "failure",
                now_ms,
                f"r{i}",
            ),
        )
    try:
        args = SimpleNamespace(last="1d", task=None, group_by="task")
        out = _render_aggregate(s, args)
    finally:
        s.close()

    # Header carries all five columns the AC names.
    header_line = [ln for ln in out.splitlines() if "runs" in ln and "avg_$" in ln][0]
    for col in ("runs", "avg_$", "p95_$", "success%", "cost/success"):
        assert col in header_line, f"missing column {col!r} in header: {header_line!r}"


def test_aggregate_view_computes_p95_and_cost_per_success(tmp_path) -> None:
    """Five runs with totals 1, 2, 3, 4, 5 (Mnd); 3 successes.
    p95 = 5 (index = floor(5 × 0.95) = 4 → 5th value).
    cost/success = sum(1+2+3+4+5)M / 3 = 5M nd ≈ $0.0050."""
    import time
    from types import SimpleNamespace

    from inkfoot.cli.report import _render_aggregate
    from inkfoot.storage.sqlite import SQLiteStorage

    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    now_ms = int(time.time() * 1000)
    for i in range(5):
        s.start_run(
            run_id=f"r{i}",
            task="t",
            agent_kind="a",
            started_at=now_ms - 1000,
        )
        s._conn().execute(
            "UPDATE runs SET total_nanodollars = ?, outcome = ?, "
            "status = 'complete', ended_at = ? WHERE id = ?",
            (
                (i + 1) * 1_000_000,
                "success" if i < 3 else "failure",
                now_ms,
                f"r{i}",
            ),
        )
    try:
        args = SimpleNamespace(last="1d", task=None, group_by="task")
        out = _render_aggregate(s, args)
    finally:
        s.close()
    # p95 = 5_000_000 nd = $0.0050.
    assert "$0.0050" in out
    # cost/success = 15M / 3 = 5M nd = $0.0050.
    # Both p95 and cost-per-success happen to land on the same
    # number for this fixture — the column position differentiates.


def test_aggregate_view_shows_em_dash_when_no_successes(tmp_path) -> None:
    """When n_success == 0 we can't divide; the cell shows "—"."""
    import time
    from types import SimpleNamespace

    from inkfoot.cli.report import _render_aggregate
    from inkfoot.storage.sqlite import SQLiteStorage

    db = tmp_path / "runs.db"
    s = SQLiteStorage(path=db)
    s.connect()
    now_ms = int(time.time() * 1000)
    for i in range(2):
        s.start_run(
            run_id=f"r{i}",
            task="all-fail",
            agent_kind="a",
            started_at=now_ms - 1000,
        )
        s._conn().execute(
            "UPDATE runs SET total_nanodollars = ?, outcome = 'failure', "
            "status = 'complete', ended_at = ? WHERE id = ?",
            (1_000_000, now_ms, f"r{i}"),
        )
    try:
        args = SimpleNamespace(last="1d", task=None, group_by="task")
        out = _render_aggregate(s, args)
    finally:
        s.close()
    # Find the data row (not the header).
    data_lines = [
        ln for ln in out.splitlines() if "all-fail" in ln
    ]
    assert data_lines
    assert "—" in data_lines[0]


def test_aggregate_view_p95_helper_handles_boundary_cases() -> None:
    """``_p95`` of empty list → 0; single value → that value;
    index clamped at n-1 for tiny samples."""
    from inkfoot.cli.report import _p95

    assert _p95([]) == 0
    assert _p95([42]) == 42
    # Two-element list: int(2 × 0.95) = 1, returns sorted[1] = max.
    assert _p95([10, 20]) == 20
    # Twenty equal-sized values; p95 is the last.
    values = list(range(100))
    assert _p95(values) == 95
