"""Shared fixtures for the integration suite.

Postgres fixtures: opt-in via ``INKFOOT_TEST_PG_DSN`` (any libpq DSN
with permission to create schemas). Each test gets a throwaway
schema, injected through the connection's ``search_path``, so tests
are isolated from each other and from anything else in the database;
the schema is dropped afterwards. Postgres advisory locks are
cluster-wide (not schema-scoped), which is fine while the suite runs
serially.
"""

from __future__ import annotations

import os
import uuid

import pytest

_PG_ENV = "INKFOOT_TEST_PG_DSN"


@pytest.fixture
def pg_dsn():
    """A DSN scoped to a fresh, dedicated schema for this test."""
    base = os.environ.get(_PG_ENV)
    if not base:
        pytest.skip(f"{_PG_ENV} not set")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import make_conninfo

    schema = f"inkfoot_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield make_conninfo(base, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_storage(pg_dsn):
    """A connected PostgresStorage on the test's private schema."""
    from inkfoot.storage.postgres import PostgresStorage

    storage = PostgresStorage(dsn=pg_dsn, pool_min=1, pool_max=3)
    storage.connect()
    try:
        yield storage
    finally:
        storage.close()
