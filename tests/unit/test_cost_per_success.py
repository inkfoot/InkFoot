"""Cost-per-success rollup + table renderer tests.

Runs against a synthetic 100-run fixture with a known outcome mix so
every ratio in the table is hand-checkable:

* ``triage`` — 50 runs at $0.001 each: 20 success, 10
  accepted_answer, 10 failure, 10 with no outcome.
* ``search`` — 30 runs at $0.002 each: 10 success, 20 failure.
* ``doc-gen`` — 20 runs at $0.003 each, none instrumented.

The 30 outcome-less runs (10 triage + 20 doc-gen) land in the
synthetic ``uninstrumented`` bucket: 70M nanodollars — the biggest
spend of any bucket, which is exactly why it must stay pinned last
instead of competing on the spend sort.
"""

from __future__ import annotations

import pytest

from inkfoot.reports.cost_per_success import (
    UNINSTRUMENTED_BUCKET,
    BucketRow,
    render_aggregate_table,
    rollup_cost_per_success,
)


def _hundred_run_fixture() -> list[dict]:
    runs: list[dict] = []

    def add(n: int, task: str, outcome, total_nd: int) -> None:
        for _ in range(n):
            runs.append(
                {
                    "id": f"r{len(runs)}",
                    "task": task,
                    "outcome": outcome,
                    "total_nanodollars": total_nd,
                }
            )

    add(20, "triage", "success", 1_000_000)
    add(10, "triage", "accepted_answer", 1_000_000)
    add(10, "triage", "failure", 1_000_000)
    add(10, "triage", None, 1_000_000)
    add(10, "search", "success", 2_000_000)
    add(20, "search", "failure", 2_000_000)
    add(20, "doc-gen", None, 3_000_000)
    assert len(runs) == 100
    return runs


def _rollup(runs):
    return rollup_cost_per_success(runs, bucket_of=lambda run: run["task"])


def _row(rows, bucket) -> BucketRow:
    matches = [r for r in rows if r.bucket == bucket]
    assert len(matches) == 1, f"expected one {bucket!r} row, got {matches}"
    return matches[0]


# ----------------------------------------------------------------------
# Rollup
# ----------------------------------------------------------------------


def test_hundred_runs_roll_up_into_task_buckets_plus_uninstrumented() -> None:
    rows = _rollup(_hundred_run_fixture())
    assert [r.bucket for r in rows] == [
        "search",  # 60M spend
        "triage",  # 40M spend
        UNINSTRUMENTED_BUCKET,  # 70M spend — pinned last anyway
    ]
    assert sum(r.n_runs for r in rows) == 100


def test_cost_per_success_divides_bucket_spend_by_success_count() -> None:
    rows = _rollup(_hundred_run_fixture())
    triage = _row(rows, "triage")
    # 40 outcome-bearing runs × 1M = 40M nd across 20 successes:
    # the failures' and accepted answers' spend is folded in.
    assert triage.n_runs == 40
    assert triage.total_nanodollars == 40_000_000
    assert triage.n_success == 20
    assert triage.cost_per_success_nd == 2_000_000

    search = _row(rows, "search")
    assert search.cost_per_success_nd == 6_000_000


def test_cost_per_accepted_answer_is_its_own_ratio() -> None:
    rows = _rollup(_hundred_run_fixture())
    triage = _row(rows, "triage")
    assert triage.n_accepted_answer == 10
    assert triage.cost_per_accepted_answer_nd == 4_000_000
    # search has no accepted answers — the ratio is undefined.
    assert _row(rows, "search").cost_per_accepted_answer_nd is None


def test_success_rate_counts_only_strict_success() -> None:
    rows = _rollup(_hundred_run_fixture())
    # accepted_answer runs are not counted as plain successes.
    assert _row(rows, "triage").success_rate == pytest.approx(0.5)
    assert _row(rows, "search").success_rate == pytest.approx(1 / 3)


def test_outcome_less_runs_divert_to_the_uninstrumented_bucket() -> None:
    rows = _rollup(_hundred_run_fixture())
    uninstrumented = _row(rows, UNINSTRUMENTED_BUCKET)
    assert uninstrumented.uninstrumented is True
    assert uninstrumented.n_runs == 30
    assert uninstrumented.total_nanodollars == 70_000_000
    # No outcome maths over runs that never reported one.
    assert uninstrumented.cost_per_success_nd is None
    assert uninstrumented.cost_per_accepted_answer_nd is None
    assert uninstrumented.success_rate is None


def test_uninstrumented_runs_never_reach_bucket_of() -> None:
    """The bucket function only sees outcome-bearing runs — a tag
    lookup or column read never has to handle the diverted ones."""
    seen: list[str] = []

    def bucket_of(run):
        seen.append(run["id"])
        assert run["outcome"]
        return run["task"]

    rollup_cost_per_success(_hundred_run_fixture(), bucket_of=bucket_of)
    assert len(seen) == 70


def test_empty_outcome_string_counts_as_uninstrumented() -> None:
    rows = rollup_cost_per_success(
        [
            {"id": "a", "task": "t", "outcome": "", "total_nanodollars": 5},
            {"id": "b", "task": "t", "outcome": "success", "total_nanodollars": 7},
        ],
        bucket_of=lambda run: run["task"],
    )
    assert _row(rows, UNINSTRUMENTED_BUCKET).n_runs == 1
    assert _row(rows, "t").n_runs == 1


def test_task_literally_named_uninstrumented_stays_separate() -> None:
    """A task whose *name* is "uninstrumented" must not corrupt the
    synthetic bucket: two rows, only one of them pinned."""
    rows = rollup_cost_per_success(
        [
            {
                "id": "a",
                "task": "uninstrumented",
                "outcome": "success",
                "total_nanodollars": 9,
            },
            {"id": "b", "task": "other", "outcome": None, "total_nanodollars": 3},
        ],
        bucket_of=lambda run: run["task"],
    )
    assert [r.bucket for r in rows] == [
        UNINSTRUMENTED_BUCKET,
        UNINSTRUMENTED_BUCKET,
    ]
    named, synthetic = rows
    assert named.uninstrumented is False
    assert named.n_success == 1
    assert synthetic.uninstrumented is True
    assert synthetic.n_success == 0


def test_p95_comes_from_the_bucket_total_distribution() -> None:
    runs = [
        {"id": f"r{i}", "task": "t", "outcome": "success", "total_nanodollars": nd}
        for i, nd in enumerate([1, 2, 3, 4, 100])
    ]
    rows = rollup_cost_per_success(runs, bucket_of=lambda run: run["task"])
    # idx = int(5 × 0.95) = 4 → the outlier value.
    assert _row(rows, "t").p95_nanodollars == 100


def test_skips_malformed_rows_and_totals() -> None:
    rows = rollup_cost_per_success(
        [
            "not-a-dict",
            {
                "id": "a",
                "task": "t",
                "outcome": "success",
                "total_nanodollars": "NaN",
            },
        ],
        bucket_of=lambda run: run["task"],
    )
    row = _row(rows, "t")
    assert row.n_runs == 1
    assert row.total_nanodollars == 0


def test_empty_input_rolls_up_to_no_rows() -> None:
    assert rollup_cost_per_success([], bucket_of=lambda run: "x") == []


# ----------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------


def test_table_leads_with_cost_per_success() -> None:
    out = render_aggregate_table(
        _rollup(_hundred_run_fixture()),
        window_label="7d",
        group_label="task",
    )
    header = [ln for ln in out.splitlines() if "avg_$" in ln][0]
    assert (
        header.index("runs")
        < header.index("cost/success")
        < header.index("cost/accepted_answer")
        < header.index("avg_$")
        < header.index("p95_$")
        < header.index("success%")
    )


def test_table_renders_hand_checked_dollar_figures() -> None:
    out = render_aggregate_table(
        _rollup(_hundred_run_fixture()),
        window_label="7d",
        group_label="task",
    )
    triage_line = [ln for ln in out.splitlines() if "triage" in ln][0]
    assert "$0.0020" in triage_line  # cost/success = 2M nd
    assert "$0.0040" in triage_line  # cost/accepted_answer = 4M nd
    assert "$0.0010" in triage_line  # avg (and p95) = 1M nd
    assert "50.0%" in triage_line


def test_uninstrumented_row_renders_last_with_dashes() -> None:
    out = render_aggregate_table(
        _rollup(_hundred_run_fixture()),
        window_label="7d",
        group_label="task",
    )
    last_line = out.splitlines()[-1]
    assert UNINSTRUMENTED_BUCKET in last_line
    # cost/success, cost/accepted_answer, and success% all dash out.
    assert last_line.count("—") == 3
    # The spend itself stays visible — the row is a coverage gauge.
    assert "$0.0023" in last_line  # avg = 70M nd / 30 runs


def test_window_and_group_labels_appear_in_the_title() -> None:
    out = render_aggregate_table(
        [], window_label="24h", group_label="tag.customer_tier"
    )
    assert "Recent runs (24h, grouped by tag.customer_tier):" in out.splitlines()[0]
