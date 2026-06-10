"""Load Token Contract YAML files into validated :class:`Contract`s.

Three entry points:

* :func:`load_contract` — one file to one contract.
* :func:`load_contracts_dir` — a directory to a ``{task: Contract}``
  map, rejecting two files that claim the same task.
* :func:`load_contracts` — a mixed list of file and directory paths,
  used by ``inkfoot.instrument(contracts=[...])``.

Schema-version policy is enforced here, not in the schema module: the
current version loads silently, the immediately-preceding version
loads with a one-time deprecation warning pointing at the changelog,
and anything older (or newer) is rejected with an actionable message.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from inkfoot.contracts.schema import (
    CONTRACT_SCHEMA_VERSION,
    Contract,
    ContractValidationError,
    contract_from_dict,
)

_LOG = logging.getLogger("inkfoot.contracts")

# Where the human-readable record of schema changes lives. Surfaced in
# the deprecation warning so a developer on an old version knows where
# to read the migration notes.
CHANGELOG_URL = "https://inkfoot.dev/docs/reference/contract-schema-changelog"

# Oldest schema version this build will load at all. We accept the
# current version and the one before it (a one-release deprecation
# window); everything older must be migrated.
_MIN_SUPPORTED_SCHEMA_VERSION = CONTRACT_SCHEMA_VERSION - 1

# Track which deprecated versions we've already warned about so a
# directory full of old contracts logs one line per version, not one
# per file.
_warned_versions: set[int] = set()


def _read_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised only without pyyaml
        raise ContractValidationError(
            "loading Token Contracts requires a YAML parser. Install it "
            "with `pip install pyyaml` (it ships with the inkfoot dev "
            "extra as well)."
        ) from exc

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractValidationError(f"cannot read contract file {path}: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractValidationError(
            f"contract file {path} is not valid YAML: {exc}"
        ) from exc


def _check_schema_version(raw: Mapping[str, Any], source: str) -> None:
    """Apply the accept/deprecate/reject policy before full validation.

    Runs first so a too-old file produces "upgrade your contracts"
    rather than a confusing field-level error from a schema that has
    since changed shape.
    """
    version = raw.get("schema_version") if isinstance(raw, Mapping) else None
    if not isinstance(version, int) or isinstance(version, bool):
        # Leave the precise "missing/mistyped schema_version" message to
        # the schema validator; it words it consistently with the rest.
        return
    if version > CONTRACT_SCHEMA_VERSION:
        raise ContractValidationError(
            f"contract {source} declares schema_version {version}, which is "
            f"newer than this build supports (current: "
            f"{CONTRACT_SCHEMA_VERSION}). Upgrade inkfoot, or pin the "
            f"contract to a supported schema_version. See {CHANGELOG_URL}."
        )
    if version < _MIN_SUPPORTED_SCHEMA_VERSION:
        raise ContractValidationError(
            f"contract {source} declares schema_version {version}, which is "
            f"too old for this build (minimum supported: "
            f"{_MIN_SUPPORTED_SCHEMA_VERSION}). Migrate it to schema_version "
            f"{CONTRACT_SCHEMA_VERSION}. See {CHANGELOG_URL}."
        )
    if version < CONTRACT_SCHEMA_VERSION and version not in _warned_versions:
        _warned_versions.add(version)
        _LOG.warning(
            "Token Contract %s uses schema_version %d, which is deprecated "
            "and will stop loading in a future release. Migrate to "
            "schema_version %d — see %s.",
            source,
            version,
            CONTRACT_SCHEMA_VERSION,
            CHANGELOG_URL,
        )


def load_contract(path: str | Path) -> Contract:
    """Load and validate a single contract file.

    Raises :class:`ContractValidationError` for a missing file, invalid
    YAML, an unsupported schema version, or any schema violation.
    """
    p = Path(path)
    if not p.exists():
        raise ContractValidationError(f"contract file not found: {p}")
    raw = _read_yaml(p)
    if raw is None:
        raise ContractValidationError(f"contract file {p} is empty")
    source = str(p)
    _check_schema_version(raw, source)
    return contract_from_dict(raw, source=source)


# Files matched by :func:`load_contracts_dir`. Both YAML suffixes are
# common in the wild; we accept either.
_YAML_GLOBS = ("*.yaml", "*.yml")


def load_contracts_dir(path: str | Path) -> dict[str, Contract]:
    """Load every contract in a directory into a ``{task: Contract}`` map.

    Two files declaring the same ``task`` are a configuration error —
    the runtime couldn't know which budget to enforce — so this raises
    rather than letting one silently win.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise ContractValidationError(f"not a directory: {directory}")

    by_task: dict[str, Contract] = {}
    source_by_task: dict[str, str] = {}
    for file in sorted(_iter_yaml_files(directory)):
        contract = load_contract(file)
        if contract.task in by_task:
            raise ContractValidationError(
                f"duplicate task {contract.task!r}: declared in both "
                f"{source_by_task[contract.task]} and {file}. Each task "
                f"must be defined by exactly one contract file."
            )
        by_task[contract.task] = contract
        source_by_task[contract.task] = str(file)
    return by_task


def _iter_yaml_files(directory: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for glob in _YAML_GLOBS:
        for file in directory.glob(glob):
            if file.is_file() and file not in seen:
                seen.add(file)
                yield file


def load_contracts(paths: Iterable[str | Path]) -> dict[str, Contract]:
    """Load a mix of file and directory paths into one ``{task: Contract}``.

    This is the shape :func:`inkfoot.instrument` consumes. Duplicate
    task names across the combined set are rejected the same way
    :func:`load_contracts_dir` rejects them within one directory.
    """
    merged: dict[str, Contract] = {}
    source_by_task: dict[str, str] = {}

    def _add(contract: Contract, source: str) -> None:
        if contract.task in merged:
            raise ContractValidationError(
                f"duplicate task {contract.task!r}: declared in both "
                f"{source_by_task[contract.task]} and {source}."
            )
        merged[contract.task] = contract
        source_by_task[contract.task] = source

    for path in paths:
        p = Path(path)
        if p.is_dir():
            for task, contract in load_contracts_dir(p).items():
                _add(contract, f"{p} ({task})")
        else:
            _add(load_contract(p), str(p))
    return merged
