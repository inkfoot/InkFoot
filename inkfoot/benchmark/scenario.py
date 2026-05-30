"""Scenario discovery + loading for ``inkfoot benchmark``.

A scenario is a single ``.py`` file that exports two names:

* ``INKFOOT_SCENARIO`` — a dict describing the scenario (``task``,
  ``fixtures``, ``expected_outcome``, ``runs_per_fixture``).
* ``run(fixture)`` — a callable that takes one loaded fixture and
  returns whatever the agent returns. The benchmark records cost
  and outcome; the return value itself is informational.

The discovery walk is shallow-deep but stable: scenarios sort by
filename, so the artefact preserves a deterministic ordering even
when two runs hit the same directory in a different file-system
enumeration order.

Fixture files are loaded with the JSON loader by default. Subclasses
of :class:`ScenarioLoader` (or test code) may override
:meth:`load_fixture` to support YAML, text, etc.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# Module-level attribute name the scenario file must export.
_SCENARIO_ATTR = "INKFOOT_SCENARIO"
_RUN_ATTR = "run"


class ScenarioLoadError(ValueError):
    """Raised when a scenario file is missing required exports or has
    bad metadata. Surfaced to the CLI with the offending path so the
    user can fix the file."""


@dataclass(frozen=True)
class Scenario:
    """One discovered scenario, ready to run.

    The ``module`` reference keeps the loaded module alive while the
    runner iterates fixtures — relying on ``sys.modules`` alone is
    fragile when scenarios share a parent package.
    """

    path: Path
    task: str
    fixtures: tuple[str, ...]
    expected_outcome: str
    runs_per_fixture: int
    run: Callable[[Any], Any]
    module: Any = field(repr=False)

    @property
    def name(self) -> str:
        """Short identifier used in CLI output."""
        return self.path.stem


@dataclass
class ScenarioLoader:
    """Loads scenarios from a directory. Holds the search rules in one
    place so tests can patch behaviour without re-implementing the walk."""

    fixture_root: Optional[Path] = None

    def discover(self, scenarios_dir: Path | str) -> list[Scenario]:
        """Walk ``scenarios_dir`` and return every valid scenario.

        Files prefixed with ``_`` or ``conftest`` are skipped — the
        former is the Python convention for "private", the latter
        belongs to pytest. Both are useful in a scenarios directory
        but they aren't scenarios themselves.
        """
        root = Path(scenarios_dir)
        if not root.exists():
            raise FileNotFoundError(
                f"scenarios directory does not exist: {root}"
            )
        if not root.is_dir():
            raise NotADirectoryError(
                f"scenarios path is not a directory: {root}"
            )

        # Resolve fixture root to the scenarios dir by default; tests
        # may pin it explicitly to disentangle the layout.
        if self.fixture_root is None:
            self.fixture_root = root

        results: list[Scenario] = []
        for py in sorted(root.glob("*.py")):
            if py.name.startswith("_") or py.name.startswith("conftest"):
                continue
            try:
                scenario = self.load_path(py)
            except ScenarioLoadError:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                raise ScenarioLoadError(
                    f"scenario {py} failed to import: {exc!r}"
                ) from exc
            results.append(scenario)
        return results

    def load_path(self, path: Path) -> Scenario:
        """Import ``path`` as a fresh module and return its
        :class:`Scenario` view.

        A unique module name (uuid suffix) keeps repeated loads of the
        same file independent — important for tests that mutate the
        scenario between runs and don't want a stale cache."""
        if not path.exists():
            raise ScenarioLoadError(f"scenario file does not exist: {path}")
        if path.suffix != ".py":
            raise ScenarioLoadError(
                f"scenario file must be .py, got {path.suffix!r}: {path}"
            )

        mod_name = f"_inkfoot_scenario_{path.stem}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:  # pragma: no cover — defensive
            raise ScenarioLoadError(
                f"could not build import spec for {path}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise

        meta = getattr(module, _SCENARIO_ATTR, None)
        if not isinstance(meta, dict):
            raise ScenarioLoadError(
                f"{path}: missing or non-dict module attribute {_SCENARIO_ATTR!r}"
            )
        run_fn = getattr(module, _RUN_ATTR, None)
        if not callable(run_fn):
            raise ScenarioLoadError(
                f"{path}: missing required callable `run(fixture)`"
            )

        task = meta.get("task")
        if not isinstance(task, str) or not task:
            raise ScenarioLoadError(
                f"{path}: INKFOOT_SCENARIO['task'] must be a non-empty string"
            )
        fixtures_raw = meta.get("fixtures", [])
        if not isinstance(fixtures_raw, (list, tuple)):
            raise ScenarioLoadError(
                f"{path}: INKFOOT_SCENARIO['fixtures'] must be a list of paths"
            )
        fixtures = tuple(str(f) for f in fixtures_raw)

        expected_outcome = meta.get("expected_outcome", "success")
        if not isinstance(expected_outcome, str) or not expected_outcome:
            raise ScenarioLoadError(
                f"{path}: INKFOOT_SCENARIO['expected_outcome'] must be a string"
            )
        runs_per_fixture = meta.get("runs_per_fixture", 1)
        if (
            not isinstance(runs_per_fixture, int)
            or isinstance(runs_per_fixture, bool)
            or runs_per_fixture < 1
        ):
            raise ScenarioLoadError(
                f"{path}: INKFOOT_SCENARIO['runs_per_fixture'] must be a "
                f"positive int, got {runs_per_fixture!r}"
            )

        return Scenario(
            path=path,
            task=task,
            fixtures=fixtures,
            expected_outcome=expected_outcome,
            runs_per_fixture=runs_per_fixture,
            run=run_fn,
            module=module,
        )

    # ------------------------------------------------------------------
    # Fixture loading — overridable for non-JSON formats.
    # ------------------------------------------------------------------

    def iter_fixture_payloads(
        self, scenario: Scenario
    ) -> Iterable[tuple[str, Any]]:
        """Yield ``(fixture_path, fixture_payload)`` pairs.

        The ``fixture_path`` carries through into events so reports
        can show "which fixture produced this result". A scenario
        with no declared fixtures gets a single ``(None, None)`` pair
        so ``run(None)`` is still invoked once — useful for warm-up
        or smoke-test scenarios."""
        if not scenario.fixtures:
            yield (f"{scenario.task}#default", None)
            return
        for fx in scenario.fixtures:
            resolved = self._resolve(fx)
            yield (str(resolved), self.load_fixture(resolved))

    def _resolve(self, fixture_path: str) -> Path:
        """Resolve a fixture path against the configured
        ``fixture_root``. Absolute paths win unchanged; relative paths
        join to the root."""
        p = Path(fixture_path)
        if p.is_absolute():
            return p
        assert self.fixture_root is not None  # populated in discover()
        return self.fixture_root / p

    def load_fixture(self, path: Path) -> Any:
        """Read + parse a fixture file. Default: JSON; missing files
        raise :class:`FileNotFoundError` so the runner records the
        failure cleanly."""
        if not path.exists():
            raise FileNotFoundError(
                f"fixture file does not exist: {path}"
            )
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".json", ".jsn"):
            return json.loads(text)
        # Default: hand the raw text to the scenario. The scenario is
        # the source of truth on its own fixture format; the loader
        # intentionally doesn't try to guess YAML / TOML / etc.
        return text
