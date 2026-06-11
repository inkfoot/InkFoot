"""Smell-engine hot-path benchmark.

Budget: all built-in smells evaluate against a 50-event run in
under 10 ms. The smell engine is lazy + off the shim hot
path; this benchmark gates the slice CI sees, not the SDK wrapper
path.

Asserts on median + p95 (per the review pattern) so a noisy
shared CI runner doesn't trigger false failures.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from inkfoot.smells import DEFAULT_SMELLS
from inkfoot.smells.engine import SmellEngine


_SMELLS_MEDIAN_BUDGET_S = 0.010  # 10 ms
_SMELLS_P95_BUDGET_S = 0.020  # 20 ms p95 (looser on noisy CI)


def _build_fifty_event_run() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Materialise a 50-event run with a representative payload
    mix — enough cache reads to trigger one smell, an oversized
    tool result for a second, and a low-entropy expensive call for
    a third. Three smells fire; the rest stay silent."""
    run = {
        "id": "smell-bench-run",
        "task": "smell-bench",
        "started_at": 1_700_000_000_000,
        "ended_at": 1_700_000_000_100,
        "outcome": "success",
        "quality_score": 0.9,
        "total_nanodollars": 0,
    }

    events: list[dict[str, Any]] = []
    for i in range(50):
        ledger = {
            "system_static_tokens": 100,
            "system_dynamic_tokens": 30,
            "user_input_tokens": 25,
            "tool_schema_tokens": 15,
            "tool_result_tokens": 2200 if i % 7 == 0 else 50,
            "retrieved_context_tokens": 0,
            "memory_tokens": 40,
            "retry_overhead_tokens": 0,
            "summariser_tokens": 0,
            "reasoning_tokens": 0,
            "guardrail_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 80,
            "output_tokens": 25,
        }
        payload = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "sequence": i + 1,
            "started_at": 1_700_000_000_000 + i,
            "ended_at": 1_700_000_000_000 + i + 1,
            "ledger": ledger,
            "estimated_nanodollars": 1_000,
            "tools_offered": ["search"],
            "tools_called": ["search"] if i % 4 == 0 else [],
            "cache_status": "hit",
        }
        events.append(
            {
                "id": f"e-{i}",
                "run_id": "smell-bench-run",
                "kind": "llm_call",
                "occurred_at": 1_700_000_000_000 + i + 1,
                "sequence": i + 1,
                "payload_json": json.dumps(payload),
                "capture_mode": "metadata",
            }
        )
    return run, events


def test_all_builtin_smells_evaluate_under_ten_ms_for_fifty_events(
    benchmark,
) -> None:
    run, events = _build_fifty_event_run()
    engine = SmellEngine(list(DEFAULT_SMELLS))

    def one_eval() -> None:
        engine.evaluate(run, events)

    # Warm-up.
    one_eval()
    benchmark.pedantic(one_eval, rounds=100, iterations=1)

    median = benchmark.stats.stats.median
    sample = sorted(benchmark.stats.stats.data)
    p95 = sample[int(len(sample) * 0.95)]
    assert median < _SMELLS_MEDIAN_BUDGET_S, (
        f"smell engine median {median * 1000:.2f} ms exceeded "
        f"{_SMELLS_MEDIAN_BUDGET_S * 1000:.0f} ms"
    )
    assert p95 < _SMELLS_P95_BUDGET_S, (
        f"smell engine p95 {p95 * 1000:.2f} ms exceeded "
        f"{_SMELLS_P95_BUDGET_S * 1000:.0f} ms"
    )
