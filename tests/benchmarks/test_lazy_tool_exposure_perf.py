"""LazyToolExposure classifier benchmark.

Asserts the policy perf budget: ``before_call`` p95 < 100 µs on a
realistic request — a dozen tool schemas and a multi-turn message
history. The classifier runs in front of every adapter-routed LLM
call, so its freshness bookkeeping and whole-word mention scan must
stay micro-scale.

A fresh ``CallContext`` is built per round in the (untimed) pedantic
``setup`` hook because the policy edits the outgoing request's tools
array in place — reusing one context would shrink the workload after
the first round. The run's turn counter still advances round over
round, so the measurement includes the steady-state stale-drop work,
not just the first-sight fast path. Run locally with::

    pytest tests/benchmarks/ --benchmark-only
"""

from __future__ import annotations

import pytest

from inkfoot.policy import CallContext
from inkfoot.policy.lazy_tool_exposure import LazyToolExposure

_ROUNDS = 5_000
# Budget asserted on p95; median asserted at the same constant as an
# outlier-robust backstop against shared-runner scheduler blips.
_P95_BUDGET_S = 0.000_100  # 100 µs
_MEDIAN_BUDGET_S = 0.000_100

_TOOL_NAMES = [
    "search_tickets",
    "read_ticket",
    "summarise_thread",
    "lookup_customer",
    "check_entitlements",
    "query_billing",
    "create_followup",
    "escalate_to_human",
    "post_reply",
    "close_ticket",
    "fetch_deploy_notes",
    "submit_answer",
]

_MESSAGES = [
    {"role": "user", "content": "The export job fails since the deploy."},
    {
        "role": "assistant",
        "content": "Let me search_tickets for similar reports first.",
    },
    {"role": "user", "content": "It's ticket 4812, enterprise customer."},
    {
        "role": "assistant",
        "content": "Pulling the ticket and the deploy notes now.",
    },
    {"role": "user", "content": "Please check_entitlements before replying."},
    {
        "role": "assistant",
        "content": "Entitlements confirmed; drafting the reply.",
    },
    {
        "role": "user",
        "content": "Good — post_reply and create_followup for Monday.",
    },
]


def _tools() -> list[dict]:
    return [
        {
            "name": name,
            "description": f"Tool {name} for the support workflow.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
        for name in _TOOL_NAMES
    ]


def test_before_call_p95_under_hundred_microseconds(benchmark) -> None:
    policy = LazyToolExposure(stale_after_turns=3, core_tools=("submit_answer",))

    def make_ctx():
        ctx = CallContext(
            provider="anthropic",
            model="claude-haiku-4-5",
            run_id="run-perf",
            request_kwargs={
                "model": "claude-haiku-4-5",
                "max_tokens": 1024,
                "tools": _tools(),
                "messages": list(_MESSAGES),
            },
        )
        return (ctx,), {}

    # Sanity: the policy yields a usable decision on this workload.
    (ctx,), _ = make_ctx()
    assert policy.before_call(ctx).action in ("allow", "warn")

    benchmark.pedantic(
        policy.before_call, setup=make_ctx, rounds=_ROUNDS, iterations=1
    )

    stats = benchmark.stats.stats
    assert stats.median < _MEDIAN_BUDGET_S, (
        f"median before_call {stats.median * 1e6:.1f} µs exceeded "
        f"{_MEDIAN_BUDGET_S * 1e6:.1f} µs budget"
    )
    sample = sorted(stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert p95 < _P95_BUDGET_S, (
        f"p95 before_call {p95 * 1e6:.1f} µs exceeded "
        f"{_P95_BUDGET_S * 1e6:.1f} µs budget"
    )
