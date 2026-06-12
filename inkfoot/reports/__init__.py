"""Cross-run report rollups.

The modules here are the computation layer behind ``inkfoot
report``'s aggregate view: pure rollups over run-row dicts
(:mod:`inkfoot.reports.cost_per_success`) plus one window-bounded
query helper for tag grouping
(:mod:`inkfoot.reports.tag_groupby`). The CLI in
:mod:`inkfoot.cli.report` loads the rows from storage and prints
the rendered strings; keeping the arithmetic here keeps it
unit-testable without a CLI invocation and free of any
backend-specific SQL.
"""
