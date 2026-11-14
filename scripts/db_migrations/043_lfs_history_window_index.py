#!/usr/bin/env python3
"""
Migration 043: an index for the newest LFS history rows of a path (#153).

Changes:
- lfsobjecthistory: index ``(repository_id, path_in_repo, created_at, id)``, so the retention decision of the LFS
  collection reads the newest rows of a path without sorting its whole history; it replaces the
  ``(repository_id, path_in_repo)`` index, whose lookups it serves as well.

The rows are untouched. Building the index reads the table once (seconds for a million rows); the deployment runs
migrations while the api and the worker are stopped, so no writer waits on it. Plain SQL, not the models.
"""

import importlib.util
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import should_skip_due_to_future_migrations

MIGRATION_NUMBER = 43
TABLE = "lfsobjecthistory"
NEW_INDEX = "lfsobjecthistory_repository_id_path_in_repo_created_at_id"
OLD_INDEX = "lfsobjecthistory_repository_id_path_in_repo"
# Two containers starting together would both build the index; the runners take turns on this advisory lock
LOCK_KEY = 20261115043


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_history_index_predecessor", Path(__file__).with_name("042_collections.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def _index_exists(database) -> bool:
    return any(index.name == NEW_INDEX for index in database.get_indexes(TABLE))


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete and the new index exists."""
    return _predecessor_applied(database, config) and _index_exists(database)


def _migrate():
    print("Adding the index of the newest LFS history rows of a path...")
    db.execute_sql(
        f'CREATE INDEX IF NOT EXISTS "{NEW_INDEX}" ON "{TABLE}" '
        '("repository_id", "path_in_repo", "created_at", "id")'
    )
    db.execute_sql(f'DROP INDEX IF EXISTS "{OLD_INDEX}"')
    print(f"  ✓ Added {NEW_INDEX}, dropped {OLD_INDEX}")


def run():
    """Run migration 043.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (index exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: LFS history window index")
        print("=" * 70)
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                # held until the transaction ends; a runner that waited finds the work done
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            _migrate()
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
