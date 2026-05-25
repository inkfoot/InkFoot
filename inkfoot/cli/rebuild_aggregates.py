"""``inkfoot rebuild-aggregates`` subcommand.

Marks every run dirty and drains the aggregator. Useful after a
crash, after manually editing the database, or after adding a new
projection column to ``runs``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from inkfoot.storage.aggregator import AggregatorWorker
from inkfoot.storage.sqlite import SQLiteStorage, _default_db_path


def run(args: argparse.Namespace) -> int:
    db_path: Path | str
    if args.db is None:
        db_path = _default_db_path()
    elif args.db == ":memory:":  # pragma: no cover — exposed for tests only
        db_path = ":memory:"
    else:
        db_path = Path(args.db)

    storage = SQLiteStorage(path=db_path)
    try:
        storage.connect()
        n_marked = storage.mark_all_dirty()
        worker = AggregatorWorker(storage)
        n_drained = worker.drain_once()
        print(
            f"rebuild-aggregates: marked {n_marked} runs dirty; "
            f"drained {n_drained} runs."
        )
    finally:
        storage.close()
    return 0
