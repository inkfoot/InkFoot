"""Unit tests for the Postgres storage backend — no server needed.

Everything here exercises logic that runs *before* any connection is
opened: DSN/pool-size resolution, argument validation, the optional-
dependency install hint, Protocol signature alignment, and the
migration list's invariants (including column parity with the SQLite
schema). The behavioral contract against a real server lives in
``tests/integration/test_postgres_storage.py``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import sys

import pytest

import inkfoot
import inkfoot._instrument as instrument_mod
from inkfoot.errors import StorageError
from inkfoot.storage import PROJECTION_COLUMNS, Storage
from inkfoot.storage.postgres import (
    _DEFAULT_POOL_MAX,
    _DEFAULT_POOL_MIN,
    PostgresStorage,
    _resolve_pool_sizes,
)
from inkfoot.storage.postgres_migrations import (
    _MIGRATIONS,
    advisory_lock_key,
)


@pytest.fixture(autouse=True)
def _clean_pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no Postgres-related env vars set."""
    for name in (
        "INKFOOT_PG_DSN",
        "INKFOOT_PG_POOL_MIN",
        "INKFOOT_PG_POOL_MAX",
    ):
        monkeypatch.delenv(name, raising=False)


_DSN = "postgresql://inkfoot:inkfoot@localhost:5432/inkfoot"


# ----------------------------------------------------------------------
# Construction: DSN resolution + argument validation
# ----------------------------------------------------------------------


def test_explicit_dsn_is_used() -> None:
    storage = PostgresStorage(dsn=_DSN)
    assert storage.dsn == _DSN


def test_dsn_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFOOT_PG_DSN", _DSN)
    storage = PostgresStorage()
    assert storage.dsn == _DSN


def test_explicit_dsn_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INKFOOT_PG_DSN", "postgresql://other/db")
    storage = PostgresStorage(dsn=_DSN)
    assert storage.dsn == _DSN


def test_missing_dsn_raises_with_env_var_hint() -> None:
    with pytest.raises(ValueError, match="INKFOOT_PG_DSN"):
        PostgresStorage()


def test_empty_env_dsn_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFOOT_PG_DSN", "")
    with pytest.raises(ValueError, match="INKFOOT_PG_DSN"):
        PostgresStorage()


@pytest.mark.parametrize("timeout", [0, -1, -0.5])
def test_non_positive_connect_timeout_raises(timeout: float) -> None:
    with pytest.raises(ValueError, match="connect_timeout"):
        PostgresStorage(dsn=_DSN, connect_timeout=timeout)


# ----------------------------------------------------------------------
# Pool sizing: constructor args strict, env vars lenient
# ----------------------------------------------------------------------


def test_pool_defaults() -> None:
    assert _resolve_pool_sizes(None, None) == (
        _DEFAULT_POOL_MIN,
        _DEFAULT_POOL_MAX,
    )


def test_pool_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKFOOT_PG_POOL_MIN", "2")
    monkeypatch.setenv("INKFOOT_PG_POOL_MAX", "8")
    assert _resolve_pool_sizes(None, None) == (2, 8)


def test_explicit_args_win_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INKFOOT_PG_POOL_MIN", "2")
    monkeypatch.setenv("INKFOOT_PG_POOL_MAX", "8")
    assert _resolve_pool_sizes(3, 5) == (3, 5)


@pytest.mark.parametrize("bad_min", [0, -1])
def test_explicit_pool_min_below_one_raises(bad_min: int) -> None:
    with pytest.raises(ValueError, match="pool_min"):
        _resolve_pool_sizes(bad_min, None)


@pytest.mark.parametrize("bad_max", [0, -3])
def test_explicit_pool_max_below_one_raises(bad_max: int) -> None:
    with pytest.raises(ValueError, match="pool_max"):
        _resolve_pool_sizes(None, bad_max)


def test_explicit_min_above_max_raises() -> None:
    with pytest.raises(ValueError, match="pool_min"):
        _resolve_pool_sizes(5, 2)


def test_garbage_env_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INKFOOT_PG_POOL_MIN", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="inkfoot.storage.postgres"):
        resolved = _resolve_pool_sizes(None, None)
    assert resolved == (_DEFAULT_POOL_MIN, _DEFAULT_POOL_MAX)
    assert "not an integer" in caplog.text


def test_env_zero_is_clamped_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INKFOOT_PG_POOL_MIN", "0")
    monkeypatch.setenv("INKFOOT_PG_POOL_MAX", "0")
    assert _resolve_pool_sizes(None, None) == (1, 1)


def test_env_min_above_max_raises_max_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INKFOOT_PG_POOL_MIN", "6")
    monkeypatch.setenv("INKFOOT_PG_POOL_MAX", "2")
    with caplog.at_level(logging.WARNING, logger="inkfoot.storage.postgres"):
        resolved = _resolve_pool_sizes(None, None)
    assert resolved == (6, 6)
    assert "raising max" in caplog.text


# ----------------------------------------------------------------------
# Lifecycle guards (no server contact)
# ----------------------------------------------------------------------


def test_methods_raise_before_connect() -> None:
    storage = PostgresStorage(dsn=_DSN)
    with pytest.raises(RuntimeError, match="not connected"):
        storage.start_run(
            run_id="r1", task=None, agent_kind=None, started_at=1
        )
    with pytest.raises(RuntimeError, match="not connected"):
        list(storage.iter_events("r1"))
    with pytest.raises(RuntimeError, match="not connected"):
        storage.read_dirty()


def test_connect_after_close_raises() -> None:
    storage = PostgresStorage(dsn=_DSN)
    storage.close()
    with pytest.raises(RuntimeError, match="closed"):
        storage.connect()


def test_close_is_idempotent() -> None:
    storage = PostgresStorage(dsn=_DSN)
    storage.close()
    storage.close()  # must not raise


def test_validation_errors_fire_before_connection() -> None:
    """Argument validation happens before the (absent) connection is
    touched, so the error a caller sees is the *useful* one."""
    storage = PostgresStorage(dsn=_DSN)
    with pytest.raises(ValueError, match="capture_mode"):
        storage.insert_event(
            event_id="e1",
            run_id="r1",
            kind="llm_call",
            occurred_at=1,
            sequence=1,
            capture_mode="bogus",
        )
    with pytest.raises(ValueError, match="status"):
        storage.end_run(run_id="r1", ended_at=2, status="bogus")
    with pytest.raises(ValueError, match="limit"):
        storage.read_dirty(limit=0)
    with pytest.raises(ValueError, match="unknown keys"):
        storage.write_totals(run_id="r1", totals={"nope": 1})
    with pytest.raises(ValueError, match="at least one"):
        storage.write_totals(run_id="r1", totals={})
    with pytest.raises(ValueError, match="unknown keys"):
        storage.update_aggregates(run_id="r1", totals={"nope": 1})


def test_missing_driver_raises_storage_error_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the postgres extra installed, connect() must point the
    user at the fix, not dump a bare ImportError."""
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)
    storage = PostgresStorage(dsn=_DSN)
    with pytest.raises(StorageError, match=r"inkfoot\[postgres\]"):
        storage.connect()


# ----------------------------------------------------------------------
# Protocol conformance
# ----------------------------------------------------------------------


_PROTOCOL_METHODS = (
    "connect",
    "close",
    "start_run",
    "end_run",
    "insert_event",
    "mark_dirty",
    "read_dirty",
    "claim_clean",
    "write_totals",
    "update_aggregates",
    "iter_events",
    "find_runs_with_status",
)


@pytest.mark.parametrize("method", _PROTOCOL_METHODS)
def test_postgres_signature_matches_protocol(method: str) -> None:
    """Every Protocol parameter must exist on the Postgres impl —
    same drift guard the SQLite backend has."""
    proto_params = inspect.signature(getattr(Storage, method)).parameters
    impl_params = inspect.signature(
        getattr(PostgresStorage, method)
    ).parameters
    for name in proto_params:
        assert name in impl_params, (
            f"PostgresStorage.{method} missing Protocol param {name!r}"
        )


def test_postgres_storage_satisfies_runtime_protocol() -> None:
    assert isinstance(PostgresStorage(dsn=_DSN), Storage)


def test_postgres_accepts_and_exposes_redaction_hook() -> None:
    """The redaction wiring installs the hook on whichever backend
    writes content; the Postgres backend must accept it the same way
    the SQLite one does, so replay content is masked on either."""
    hook = object()
    storage = PostgresStorage(dsn=_DSN, redaction_hook=hook)
    assert storage._redaction_hook is hook
    replacement = object()
    storage.set_redaction_hook(replacement)
    assert storage._redaction_hook is replacement
    storage.set_redaction_hook(None)
    assert storage._redaction_hook is None


def test_lazy_reexport_from_storage_package() -> None:
    from inkfoot.storage import PostgresStorage as ReExported

    assert ReExported is PostgresStorage


def test_external_aggregator_flag() -> None:
    """The Postgres backend hands aggregation to a separate process;
    SQLite keeps the in-process worker."""
    from inkfoot.storage.sqlite import SQLiteStorage

    assert PostgresStorage.external_aggregator is True
    assert not getattr(SQLiteStorage, "external_aggregator", False)


# ----------------------------------------------------------------------
# instrument() wiring: external aggregator + abandoned-run guard
# ----------------------------------------------------------------------


class _FakeExternalStorage:
    """Storage stand-in that declares external aggregation."""

    external_aggregator = True

    def __init__(self) -> None:
        self.closed = False

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def find_runs_with_status(self, status: str) -> list[str]:
        return []


def test_instrument_skips_in_process_worker_for_external_aggregator() -> None:
    instrument_mod.shutdown()  # clear any leaked state from other tests
    storage = _FakeExternalStorage()
    try:
        inkfoot.instrument(storage=storage)
        assert instrument_mod._WORKER is None
    finally:
        instrument_mod.shutdown()
    assert storage.closed is True


def test_instrument_starts_worker_for_sqlite(tmp_path) -> None:
    from inkfoot.storage.sqlite import SQLiteStorage

    instrument_mod.shutdown()
    storage = SQLiteStorage(path=tmp_path / "runs.db")
    try:
        inkfoot.instrument(storage=storage)
        assert instrument_mod._WORKER is not None
    finally:
        instrument_mod.shutdown()


def test_mark_abandoned_runs_skips_storage_without_finder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third-party Storage written against the older Protocol (no
    ``find_runs_with_status``) must not crash the atexit hook."""
    from inkfoot._run_lifecycle import _mark_abandoned_runs

    class _LegacyStorage:
        pass

    monkeypatch.setattr(instrument_mod, "_STORAGE", _LegacyStorage())
    _mark_abandoned_runs()  # must not raise


# ----------------------------------------------------------------------
# Migration list invariants
# ----------------------------------------------------------------------


def test_advisory_lock_key_is_stable_across_calls() -> None:
    assert advisory_lock_key("inkfoot_aggregator") == advisory_lock_key(
        "inkfoot_aggregator"
    )


def test_advisory_lock_key_matches_sha256_derivation() -> None:
    """The key must be reproducible by any process and any release —
    pin the derivation so a refactor can't silently change it (two
    releases disagreeing on the key would break mutual exclusion)."""
    name = "inkfoot_aggregator"
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    expected = int.from_bytes(digest[:8], "big", signed=True)
    assert advisory_lock_key(name) == expected


def test_advisory_lock_key_fits_signed_int64() -> None:
    for name in ("inkfoot_aggregator", "inkfoot_migrations", "x"):
        key = advisory_lock_key(name)
        assert -(2**63) <= key < 2**63


def test_advisory_lock_keys_differ_per_name() -> None:
    assert advisory_lock_key("inkfoot_aggregator") != advisory_lock_key(
        "inkfoot_migrations"
    )


def test_migration_versions_are_strictly_increasing_from_one() -> None:
    versions = [version for version, _, _ in _MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1


def test_migrations_have_descriptions_and_statements() -> None:
    for version, description, statements in _MIGRATIONS:
        assert description.strip(), f"v{version} lacks a description"
        assert statements, f"v{version} has no statements"
        assert all(
            isinstance(s, str) and s.strip() for s in statements
        ), f"v{version} contains an empty statement"


def test_initial_schema_creates_all_tables_and_indexes() -> None:
    _, _, statements = _MIGRATIONS[0]
    ddl = "\n".join(statements)
    for table in (
        "runs",
        "events",
        "event_contents",
        "aggregator_heartbeat",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    for index in (
        "events_run_seq",
        "runs_started",
        "runs_task_started",
        "runs_dirty",
        "runs_parent",
    ):
        assert index in ddl


def test_parent_fk_is_deferrable() -> None:
    """Bulk migration copies parents and children in one transaction
    in arbitrary order — the self-referencing FK must defer to
    commit time for that to work."""
    _, _, statements = _MIGRATIONS[0]
    runs_ddl = next(s for s in statements if "CREATE TABLE" in s and " runs " in s)
    assert "DEFERRABLE INITIALLY DEFERRED" in runs_ddl


def test_child_tables_cascade_on_delete() -> None:
    _, _, statements = _MIGRATIONS[0]
    events_ddl = next(s for s in statements if " events " in s)
    contents_ddl = next(s for s in statements if "event_contents" in s)
    assert "ON DELETE CASCADE" in events_ddl
    assert "ON DELETE CASCADE" in contents_ddl


def test_postgres_columns_match_sqlite_columns() -> None:
    """Drift guard: the migration tool's canonical column lists must
    appear in both backends' DDL, so the two schemas can't diverge
    without this test failing."""
    from inkfoot.cli.migrate import (
        _CONTENTS_COLUMNS,
        _EVENTS_COLUMNS,
        _RUNS_COLUMNS,
    )
    from inkfoot.storage.migrations import _MIGRATIONS as _SQLITE_MIGRATIONS

    pg_ddl = "\n".join(_MIGRATIONS[0][2])
    sqlite_ddl = _SQLITE_MIGRATIONS[0][2]
    for column_set in (_RUNS_COLUMNS, _EVENTS_COLUMNS, _CONTENTS_COLUMNS):
        for column in column_set:
            assert column in pg_ddl, f"{column!r} missing from Postgres DDL"
            assert column in sqlite_ddl, (
                f"{column!r} missing from SQLite DDL"
            )


def test_projection_columns_exist_in_postgres_runs_ddl() -> None:
    runs_ddl = next(
        s for s in _MIGRATIONS[0][2] if "CREATE TABLE" in s and " runs " in s
    )
    for column in PROJECTION_COLUMNS:
        assert column in runs_ddl
