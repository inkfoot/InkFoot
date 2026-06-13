"""Report embeddings-section tests.

The embeddings section is rendered below the causal-attribution chart
and is *never* folded into the ledger totals or the headline cost.
``--exclude-embeddings`` (modelled here by passing ``embeddings=None``)
must reproduce the pre-embeddings output exactly.
"""

from __future__ import annotations

import json

from inkfoot.cli.report import _aggregate_embedding_totals, render


def _run() -> dict:
    return {
        "id": "01HRUN",
        "task": "rag-answer",
        "started_at": 1_700_000_000_000,
        "ended_at": 1_700_000_010_000,
        "outcome": "success",
        "quality_score": 0.9,
        "total_nanodollars": 3_000_000,
    }


def _ledger_totals() -> dict[str, int]:
    return {
        "user_input_tokens": 2_000_000,
        "output_tokens": 1_000_000,
    }


def _embedding_event(
    model: str, tokens: int, nd, batch: int = 1, provider: str = "openai"
) -> dict:
    return {
        "kind": "embedding_call",
        "payload_json": json.dumps(
            {
                "provider": provider,
                "model": model,
                "input_tokens": tokens,
                "batch_size": batch,
                "estimated_nanodollars": nd,
                "token_count_estimated": False,
            }
        ),
    }


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def test_aggregate_sums_per_model_and_total() -> None:
    events = [
        _embedding_event("text-embedding-3-small", 100, 2_000),
        _embedding_event("text-embedding-3-small", 50, 1_000),
        _embedding_event("text-embedding-3-large", 10, 1_300),
        {"kind": "llm_call", "payload_json": "{}"},  # ignored
    ]
    summary = _aggregate_embedding_totals(events)
    assert summary["count"] == 3
    assert summary["input_tokens"] == 160
    assert summary["estimated_nanodollars"] == 4_300
    assert summary["priced"] is True
    small = summary["by_model"][("openai", "text-embedding-3-small")]
    assert small["count"] == 2
    assert small["input_tokens"] == 150


def test_aggregate_with_no_embedding_events_is_zero_count() -> None:
    summary = _aggregate_embedding_totals([{"kind": "llm_call", "payload_json": "{}"}])
    assert summary["count"] == 0
    assert summary["by_model"] == {}


def test_unpriced_call_counts_but_marks_not_priced() -> None:
    summary = _aggregate_embedding_totals(
        [_embedding_event("mystery-embedder", 100, None)]
    )
    assert summary["count"] == 1
    assert summary["input_tokens"] == 100
    assert summary["by_model"][("openai", "mystery-embedder")]["priced"] is False


def test_same_model_name_two_providers_stays_distinct() -> None:
    """Two providers serving a same-named model must not merge."""
    events = [
        _embedding_event("embed-1", 10, 100, provider="openai"),
        _embedding_event("embed-1", 20, 200, provider="cohere"),
    ]
    summary = _aggregate_embedding_totals(events)
    assert ("openai", "embed-1") in summary["by_model"]
    assert ("cohere", "embed-1") in summary["by_model"]
    out = render(
        run=_run(), ledger_totals=_ledger_totals(), smells=[], embeddings=summary
    )
    assert "openai/embed-1" in out
    assert "cohere/embed-1" in out


def test_mixed_priced_total_states_how_many_priced() -> None:
    events = [
        _embedding_event("text-embedding-3-small", 100, 2_000),  # priced
        _embedding_event("mystery-embedder", 50, None),  # unpriced
    ]
    summary = _aggregate_embedding_totals(events)
    out = render(
        run=_run(), ledger_totals=_ledger_totals(), smells=[], embeddings=summary
    )
    assert "1 of 2 calls priced" in out


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def test_section_appears_below_chart_with_separate_accounting_label() -> None:
    embeddings = _aggregate_embedding_totals(
        [_embedding_event("text-embedding-3-small", 100, 2_000)]
    )
    out = render(
        run=_run(),
        ledger_totals=_ledger_totals(),
        smells=[],
        embeddings=embeddings,
    )
    assert "Embeddings (separate accounting" in out
    assert "text-embedding-3-small" in out
    # The chart comes before the embeddings section.
    assert out.index("Causal attribution:") < out.index("Embeddings (")


def test_exclude_embeddings_matches_pre_embeddings_output() -> None:
    """``--exclude-embeddings`` (embeddings=None) must be byte-identical
    to the output a pre-embeddings build produced."""
    base = render(run=_run(), ledger_totals=_ledger_totals(), smells=[])
    excluded = render(
        run=_run(), ledger_totals=_ledger_totals(), smells=[], embeddings=None
    )
    assert excluded == base
    assert "Embeddings (" not in excluded


def test_empty_embeddings_summary_renders_no_section() -> None:
    empty = _aggregate_embedding_totals([])
    out = render(
        run=_run(), ledger_totals=_ledger_totals(), smells=[], embeddings=empty
    )
    assert "Embeddings (" not in out


def test_embeddings_do_not_change_headline_cost() -> None:
    """The header dollar figure is the ledger total only — embeddings
    sit below it and never move it."""
    embeddings = _aggregate_embedding_totals(
        [_embedding_event("text-embedding-3-small", 1000, 9_999_999)]
    )
    with_emb = render(
        run=_run(),
        ledger_totals=_ledger_totals(),
        smells=[],
        embeddings=embeddings,
    ).splitlines()[0]
    without = render(
        run=_run(), ledger_totals=_ledger_totals(), smells=[]
    ).splitlines()[0]
    assert with_emb == without


def test_unpriced_total_renders_unpriced_marker() -> None:
    embeddings = _aggregate_embedding_totals(
        [_embedding_event("mystery-embedder", 100, None)]
    )
    out = render(
        run=_run(),
        ledger_totals=_ledger_totals(),
        smells=[],
        embeddings=embeddings,
    )
    assert "(unpriced)" in out
