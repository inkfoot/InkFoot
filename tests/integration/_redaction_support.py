"""Shared helpers for the on-disk redaction scans.

The redaction guarantee is "no un-redacted secret byte reaches disk".
SQLite in WAL mode spreads recently written pages across the main
``runs.db`` file *and* a ``runs.db-wal`` sibling until a checkpoint
folds them in. A clean ``close()`` checkpoints, so in practice the
siblings are empty or gone by the time a test reads back — but the
guarantee should not depend on that. Reading the DB together with its
WAL/SHM siblings makes the scans hold regardless of checkpoint timing.
"""

from __future__ import annotations

from pathlib import Path

# SQLite's rollback-journal / WAL companion files for ``<db>``.
_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")


def read_db_and_siblings(db_path: Path) -> bytes:
    """Return the bytes of ``db_path`` concatenated with any of its
    SQLite sidecar files (``-wal`` / ``-shm`` / ``-journal``) that
    exist, so an on-disk scan sees content wherever SQLite parked it."""
    blob = bytearray(db_path.read_bytes())
    for suffix in _SIBLING_SUFFIXES:
        sibling = db_path.with_name(db_path.name + suffix)
        if sibling.exists():
            blob += sibling.read_bytes()
    return bytes(blob)
