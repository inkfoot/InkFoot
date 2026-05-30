"""Verdict threshold presets for ``inkfoot diff``.

The architecture pins three presets:

* ``tight`` — strict CI; small regressions fail the build.
* ``default`` — the spec's documented thresholds (cost +20% warn,
  +50% fail; cache-hit -10% warn, -25% fail).
* ``loose`` — early-stage projects; only catastrophic regressions.

Thresholds are *positive* fractions for cost regressions (where a
positive delta means "worse") and *positive* fractions for cache-hit
drops (where the absolute drop magnitude crosses the threshold).
``_critical_smells`` is the set of smell ids that mean an automatic
``fail`` when newly introduced — The current release ships the two
clearest-cut current smells; future Token Contracts override
this per-task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_THRESHOLD_NAME = "default"


class ThresholdsError(ValueError):
    """Raised when a custom threshold file is invalid."""


@dataclass(frozen=True)
class Thresholds:
    """Per-preset verdict cut-offs.

    Each field is a positive fraction (e.g. ``0.20`` for 20%).
    ``cost_*`` apply to ``p50_nanodollars`` and ``p95_nanodollars``;
    ``cache_*`` to ``mean_cache_hit_rate``. Outcome regressions are
    pinned to the spec — a non-zero drop in success rate is always a
    warn, a drop ≥ 10pp is always a fail.
    """

    name: str
    cost_warn: float
    cost_fail: float
    cache_warn: float
    cache_fail: float
    outcome_warn: float = 0.0
    outcome_fail: float = 0.10
    # Smell ids whose first-time appearance in current.json should
    # trigger an automatic ``fail`` verdict. Stable order so the
    # render layer can list them deterministically.
    critical_smells: tuple[str, ...] = (
        "runaway-retry-loop",
        "expensive-model-low-entropy",
    )

    def __post_init__(self) -> None:
        # Sanity checks: thresholds must be non-negative and warn must
        # be no stricter than fail. A misconfigured preset would
        # silently emit confusing verdicts ("warn but exit 0"); reject
        # at construction so the CLI surfaces it.
        for fname, val in (
            ("cost_warn", self.cost_warn),
            ("cost_fail", self.cost_fail),
            ("cache_warn", self.cache_warn),
            ("cache_fail", self.cache_fail),
            ("outcome_warn", self.outcome_warn),
            ("outcome_fail", self.outcome_fail),
        ):
            if val < 0:
                raise ThresholdsError(
                    f"Thresholds({self.name!r}): {fname} must be >= 0, got {val!r}"
                )
        if self.cost_warn > self.cost_fail:
            raise ThresholdsError(
                f"Thresholds({self.name!r}): cost_warn ({self.cost_warn}) "
                f"must be <= cost_fail ({self.cost_fail})"
            )
        if self.cache_warn > self.cache_fail:
            raise ThresholdsError(
                f"Thresholds({self.name!r}): cache_warn ({self.cache_warn}) "
                f"must be <= cache_fail ({self.cache_fail})"
            )
        if self.outcome_warn > self.outcome_fail:
            raise ThresholdsError(
                f"Thresholds({self.name!r}): outcome_warn ({self.outcome_warn}) "
                f"must be <= outcome_fail ({self.outcome_fail})"
            )


THRESHOLD_PRESETS: Mapping[str, Thresholds] = {
    "tight": Thresholds(
        name="tight",
        cost_warn=0.05,
        cost_fail=0.20,
        cache_warn=0.05,
        cache_fail=0.15,
        outcome_warn=0.0,
        outcome_fail=0.05,
    ),
    "default": Thresholds(
        name="default",
        cost_warn=0.20,
        cost_fail=0.50,
        cache_warn=0.10,
        cache_fail=0.25,
        outcome_warn=0.0,
        outcome_fail=0.10,
    ),
    "loose": Thresholds(
        name="loose",
        cost_warn=0.50,
        cost_fail=1.00,
        cache_warn=0.20,
        cache_fail=0.50,
        outcome_warn=0.05,
        outcome_fail=0.20,
    ),
}


def load_thresholds(name_or_path: Optional[str]) -> Thresholds:
    """Resolve a threshold spec.

    ``None`` -> the default preset. A name in :data:`THRESHOLD_PRESETS`
    returns that preset. Otherwise the value is treated as a path to
    a JSON document; the parsed object is fed into
    :class:`Thresholds`.
    """
    if not name_or_path:
        return THRESHOLD_PRESETS[DEFAULT_THRESHOLD_NAME]
    if name_or_path in THRESHOLD_PRESETS:
        return THRESHOLD_PRESETS[name_or_path]
    path = Path(name_or_path)
    if not path.exists():
        raise ThresholdsError(
            f"unknown threshold preset {name_or_path!r}. Expected one of "
            f"{sorted(THRESHOLD_PRESETS)} or a path to a JSON file."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThresholdsError(
            f"threshold file {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ThresholdsError(
            f"threshold file {path} must contain a JSON object"
        )
    # ``name`` defaults to the file stem so the output is readable.
    raw.setdefault("name", path.stem)
    critical = raw.get("critical_smells", THRESHOLD_PRESETS["default"].critical_smells)
    if isinstance(critical, list):
        critical = tuple(str(s) for s in critical)
    return Thresholds(
        name=str(raw["name"]),
        cost_warn=float(raw.get("cost_warn", 0.20)),
        cost_fail=float(raw.get("cost_fail", 0.50)),
        cache_warn=float(raw.get("cache_warn", 0.10)),
        cache_fail=float(raw.get("cache_fail", 0.25)),
        outcome_warn=float(raw.get("outcome_warn", 0.0)),
        outcome_fail=float(raw.get("outcome_fail", 0.10)),
        critical_smells=critical,
    )
