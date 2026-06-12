"""Smell registry tests."""

from __future__ import annotations

from typing import Any, Iterable

import pytest

from inkfoot.smells import (
    CostSmell,
    DEFAULT_SMELLS,
    DetectionResult,
    clear_registry,
    get_smell,
    list_smells,
    register_smell,
)


def _no_op_detect(run, events):
    return None


def _smell(smell_id: str = "test-smell") -> CostSmell:
    return CostSmell(
        id=smell_id,
        title="t",
        description="d",
        severity="info",
        detect=_no_op_detect,
        recommendation="r",
    )


@pytest.fixture(autouse=True)
def restore_registry() -> None:
    """Each test starts with the default registry and restores
    it on exit so a test that clears the registry doesn't poison
    subsequent tests."""
    saved = list_smells()
    yield
    # Restore: clear + repopulate.
    clear_registry()
    for s in saved:
        register_smell(s)


# ----------------------------------------------------------------------
# Acceptance
# ----------------------------------------------------------------------


def test_default_smells_has_exactly_eleven_entries() -> None:
    assert len(DEFAULT_SMELLS) == 11


def test_default_smells_are_all_unique_ids() -> None:
    ids = [s.id for s in DEFAULT_SMELLS]
    assert len(ids) == len(set(ids))


def test_get_smell_returns_known_smell_by_id() -> None:
    smell = get_smell("unstable-prompt-prefix")
    assert smell.id == "unstable-prompt-prefix"
    assert smell.severity == "warn"


def test_get_smell_raises_keyerror_for_unknown_id() -> None:
    with pytest.raises(KeyError, match="unknown"):
        get_smell("nope-not-real")


def test_register_smell_rejects_duplicate_id() -> None:
    """The first registration sticks; a second attempt with the same
    id raises rather than silently replacing the prior smell."""
    with pytest.raises(ValueError, match="already registered"):
        register_smell(get_smell("unstable-prompt-prefix"))


def test_register_smell_succeeds_for_new_id() -> None:
    new = _smell("custom-community-contributed")
    register_smell(new)
    assert get_smell("custom-community-contributed") is new


def test_list_smells_returns_a_snapshot_not_a_view() -> None:
    snap = list_smells()
    snap.append(_smell("not-actually-registered"))
    # Mutating the snapshot must not mutate the registry.
    with pytest.raises(KeyError):
        get_smell("not-actually-registered")


def test_clear_registry_drops_every_smell() -> None:
    clear_registry()
    assert list_smells() == []
    # Repopulate so the autouse-fixture restore step has work to do.
    register_smell(_smell("after-clear"))
    assert len(list_smells()) == 1


def test_default_smell_ids_match_spec() -> None:
    """Pin the canonical set so a future refactor that drops or
    renames a smell fails loudly."""
    expected = {
        "unstable-prompt-prefix",
        "runaway-retry-loop",
        "oversized-tool-result-recycled",
        "expensive-model-low-entropy",
        "recurring-cache-writes",
        "summariser-quality-regression",
        "tool-schema-drift",
        "cost-skewed-by-outlier",
        "unbounded-conversation-history",
        "over-instrumented-retries",
        "summariser-not-firing",
    }
    assert {s.id for s in DEFAULT_SMELLS} == expected
