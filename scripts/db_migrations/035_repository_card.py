#!/usr/bin/env python3
"""
Migration 035: the repository card and what it declares (#62).

Changes:
- repository_card: one row per repository, the README front matter of the head commit it was
  read at (``card``, cleaned, JSON; "{}" for a repository without a card) and what it declares as
  fields of their own (``derived``: tags, pipeline_tag, library_name, sdk).

A table of its own on purpose: migration 029 checks the columns of repository_metadata exactly, so
a column added there would make every later run of the migrations (and the run of an older release
after a rollback) report a failure. The older code does not know this table and ignores it.

Nothing is backfilled: a repository without a row is read the first time its info is asked for and
by the next scheduled index, like any snapshot that is due. Plain SQL, not the models: this must keep
meaning what it means today.
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
from _migration_utils import check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 35
TABLE = "repository_card"
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261014035


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_repository_card_predecessor", Path(__file__).with_name("034_branch_commit_lock.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations never skip
    themselves on its account) and its table exists."""
    return _predecessor_applied(database, config) and check_table_exists(database, TABLE)


def _migrate():
    cursor = db.cursor()
    print("Adding the repository card table...")
    cursor.execute(
        f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
        '"repository_id" INTEGER NOT NULL PRIMARY KEY, '
        '"main_sha" VARCHAR(64), '
        '"card" TEXT NOT NULL, '
        '"derived" TEXT NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE)'
    )
    print(f"  ✓ Added {TABLE}")


def run():
    """Run migration 035.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (card table exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repository card")
        print("=" * 70)
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                # held until the transaction ends; a runner that waited finds the work done
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                if is_applied(db, cfg):
                    print(f"Migration {MIGRATION_NUMBER}: Already applied (by another run)")
                    return True
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
