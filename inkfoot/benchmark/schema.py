"""The benchmark JSON artefact schema .

A benchmark run produces one of these. ``inkfoot diff`` consumes
exactly the shape defined here, so the schema is the contract
between the two CLIs and the GitHub Action. Stability matters:
``schema_version`` is bumped (and a translation layer added in
:mod:`inkfoot.diff.compare`) whenever any field changes shape.

A Pydantic v2 model would be the obvious shape; we keep the dependency
footprint flat by using plain dataclasses + explicit
:meth:`from_dict` / :meth:`to_dict` validators. Behaviourally
equivalent — round-trip through JSON is lossless — without
introducing a runtime dep that ``inkfoot diff`` users would also pay
to install.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


# Bump this whenever a field changes shape. ``inkfoot diff`` reads
# the version and refuses to compare across major versions to avoid
# silent semantic shifts.
BENCHMARK_SCHEMA_VERSION = "1"


class BenchmarkSchemaError(ValueError):
    """Raised when a JSON document doesn't conform to the artefact schema.

    Distinct from :class:`ValueError` so callers can distinguish
    "the file isn't valid JSON" (``json.JSONDecodeError``) from "the
    file is JSON but not a benchmark artefact" (this class).
    """


@dataclass(frozen=True)
class SmellCount:
    """How many runs in a scenario triggered a given smell.

    ``count`` is an absolute count across the scenario's runs, not a
    rate. ``inkfoot diff`` derives "appeared / disappeared" by
    comparing counts in baseline vs. current.
    """

    id: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "count": int(self.count)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SmellCount":
        smell_id = raw.get("id")
        if not isinstance(smell_id, str) or not smell_id:
            raise BenchmarkSchemaError(
                "SmellCount: 'id' must be a non-empty string"
            )
        count = raw.get("count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise BenchmarkSchemaError(
                f"SmellCount({smell_id!r}): 'count' must be a non-negative int, "
                f"got {count!r}"
            )
        return cls(id=smell_id, count=count)


@dataclass(frozen=True)
class ScenarioResult:
    """Aggregate stats for one scenario across all its fixtures.

    All cost figures are integer nanodollars (matches the project-wide
    money model — see ``inkfoot.money``).
    """

    task: str
    runs: int
    successes: int
    p50_nanodollars: int
    p95_nanodollars: int
    mean_llm_calls: float
    mean_cache_hit_rate: float
    smells_seen: tuple[SmellCount, ...] = field(default_factory=tuple)

    # Validation rules apply at the boundary (``from_dict`` and the
    # runner's ``aggregate``). The dataclass itself is intentionally
    # permissive so tests can construct hand-crafted fixtures without
    # threading every invariant through every helper.

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "runs": int(self.runs),
            "successes": int(self.successes),
            "p50_nanodollars": int(self.p50_nanodollars),
            "p95_nanodollars": int(self.p95_nanodollars),
            "mean_llm_calls": float(self.mean_llm_calls),
            "mean_cache_hit_rate": float(self.mean_cache_hit_rate),
            "smells_seen": [s.to_dict() for s in self.smells_seen],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScenarioResult":
        if not isinstance(raw, Mapping):
            raise BenchmarkSchemaError("ScenarioResult: expected a JSON object")
        task = raw.get("task")
        if not isinstance(task, str) or not task:
            raise BenchmarkSchemaError(
                "ScenarioResult: 'task' must be a non-empty string"
            )
        runs = _coerce_nonneg_int(raw, "runs", task)
        successes = _coerce_nonneg_int(raw, "successes", task)
        if successes > runs:
            raise BenchmarkSchemaError(
                f"ScenarioResult({task!r}): 'successes' ({successes}) cannot "
                f"exceed 'runs' ({runs})"
            )
        p50 = _coerce_nonneg_int(raw, "p50_nanodollars", task)
        p95 = _coerce_nonneg_int(raw, "p95_nanodollars", task)
        mean_calls = _coerce_nonneg_float(raw, "mean_llm_calls", task)
        mean_cache = _coerce_rate(raw, "mean_cache_hit_rate", task)
        smells_raw = raw.get("smells_seen", [])
        if not isinstance(smells_raw, Sequence) or isinstance(smells_raw, (str, bytes)):
            raise BenchmarkSchemaError(
                f"ScenarioResult({task!r}): 'smells_seen' must be a list"
            )
        smells = tuple(SmellCount.from_dict(s) for s in smells_raw)
        return cls(
            task=task,
            runs=runs,
            successes=successes,
            p50_nanodollars=p50,
            p95_nanodollars=p95,
            mean_llm_calls=mean_calls,
            mean_cache_hit_rate=mean_cache,
            smells_seen=smells,
        )


@dataclass(frozen=True)
class BenchmarkArtifact:
    """Top-level JSON document the runner emits and ``inkfoot diff`` reads.

    ``captured_at`` is an ISO-8601 UTC timestamp (``Z`` suffix).
    Bench runs from different versions of inkfoot can still be
    compared — ``inkfoot_version`` is informational; ``schema_version``
    is load-bearing.
    """

    inkfoot_version: str
    schema_version: str
    captured_at: str
    scenarios: tuple[ScenarioResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "inkfoot_version": self.inkfoot_version,
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    def write(self, path: Path | str, *, indent: Optional[int] = 2) -> None:
        """Persist the artefact at ``path``. Parents are created on demand.

        Uses ``utf-8`` and a trailing newline — friendlier to ``diff``
        and to scripts that ``jq`` over the output."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkArtifact":
        if not isinstance(raw, Mapping):
            raise BenchmarkSchemaError(
                "BenchmarkArtifact: expected a JSON object at the top level"
            )
        ver = raw.get("schema_version")
        if not isinstance(ver, str) or not ver:
            raise BenchmarkSchemaError(
                "BenchmarkArtifact: missing required 'schema_version' string"
            )
        if ver != BENCHMARK_SCHEMA_VERSION:
            # Forward-compat: refuse to silently load a different
            # major version. ``inkfoot diff`` surfaces this as a
            # user-visible error rather than producing nonsense diffs.
            raise BenchmarkSchemaError(
                f"BenchmarkArtifact: schema_version {ver!r} is not supported "
                f"by this version of inkfoot (expected {BENCHMARK_SCHEMA_VERSION!r}). "
                f"Re-run `inkfoot benchmark` to regenerate the artefact."
            )
        inkfoot_version = raw.get("inkfoot_version")
        if not isinstance(inkfoot_version, str) or not inkfoot_version:
            raise BenchmarkSchemaError(
                "BenchmarkArtifact: 'inkfoot_version' must be a non-empty string"
            )
        captured_at = raw.get("captured_at")
        if not isinstance(captured_at, str) or not captured_at:
            raise BenchmarkSchemaError(
                "BenchmarkArtifact: 'captured_at' must be a non-empty string"
            )
        scenarios_raw = raw.get("scenarios")
        if not isinstance(scenarios_raw, Sequence) or isinstance(
            scenarios_raw, (str, bytes)
        ):
            raise BenchmarkSchemaError(
                "BenchmarkArtifact: 'scenarios' must be a list"
            )
        scenarios = tuple(ScenarioResult.from_dict(s) for s in scenarios_raw)
        seen: set[str] = set()
        for sc in scenarios:
            if sc.task in seen:
                raise BenchmarkSchemaError(
                    f"BenchmarkArtifact: duplicate scenario task name "
                    f"{sc.task!r}; tasks must be unique within an artefact"
                )
            seen.add(sc.task)
        return cls(
            inkfoot_version=inkfoot_version,
            schema_version=ver,
            captured_at=captured_at,
            scenarios=scenarios,
        )

    @classmethod
    def load(cls, path: Path | str) -> "BenchmarkArtifact":
        """Read + validate a JSON artefact from disk.

        Surfaces three distinct failure modes so the CLI can render
        a useful error: missing file, malformed JSON, schema-invalid.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"benchmark artefact not found at {p}"
            )
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkSchemaError(
                f"benchmark artefact at {p} is not valid JSON: {exc}"
            ) from exc
        return cls.from_dict(raw)


# ----------------------------------------------------------------------
# Coercion helpers — explicit, boundary-only validation.
# ----------------------------------------------------------------------


def _coerce_nonneg_int(raw: Mapping[str, Any], key: str, task: str) -> int:
    val = raw.get(key)
    # ``bool`` is an ``int`` subclass — exclude it explicitly so
    # ``"runs": true`` doesn't silently coerce to 1.
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise BenchmarkSchemaError(
            f"ScenarioResult({task!r}): {key!r} must be a non-negative int, "
            f"got {val!r}"
        )
    return val


def _coerce_nonneg_float(raw: Mapping[str, Any], key: str, task: str) -> float:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
        raise BenchmarkSchemaError(
            f"ScenarioResult({task!r}): {key!r} must be a non-negative number, "
            f"got {val!r}"
        )
    return float(val)


def _coerce_rate(raw: Mapping[str, Any], key: str, task: str) -> float:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise BenchmarkSchemaError(
            f"ScenarioResult({task!r}): {key!r} must be a float in [0, 1]"
        )
    f = float(val)
    if not 0.0 <= f <= 1.0:
        raise BenchmarkSchemaError(
            f"ScenarioResult({task!r}): {key!r} must be in [0, 1], got {f}"
        )
    return f


# ----------------------------------------------------------------------
# Numerical helpers shared by the runner.
# ----------------------------------------------------------------------


def percentile(values: Iterable[int | float], pct: float) -> float:
    """Linear-interpolated percentile (matches numpy's default).

    Empty input returns 0.0. ``pct`` is clamped to [0, 100]. We
    interpolate so a single-element list returns that element for
    every percentile, and a two-element list returns a value between
    the two for p50.
    """
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    p = max(0.0, min(100.0, float(pct))) / 100.0
    if len(vals) == 1:
        return vals[0]
    idx = p * (len(vals) - 1)
    low = int(idx)
    high = min(low + 1, len(vals) - 1)
    frac = idx - low
    return vals[low] + (vals[high] - vals[low]) * frac
