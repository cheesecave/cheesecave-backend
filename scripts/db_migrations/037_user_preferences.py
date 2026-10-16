#!/usr/bin/env python3
"""
Migration 037: what a reader chose for themselves (the README style, to start with).

Changes:
- user_preference: one row per account that has chosen something (``data``, JSON, only the keys that were
  set; no row means "follow the site's defaults"), gone with the account.

A table of its own on purpose, as for 035: a release from before it keeps running on the same database
and ignores the table. Nothing is backfilled. Plain SQL, not the models: this must keep meaning what it
means today.
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

MIGRATION_NUMBER = 37
TABLE = "user_preference"
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261016037


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_user_preference_predecessor", Path(__file__).with_name("036_repository_derived_info.py")
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
    print("Adding the user preference table...")
    cursor.execute(
        f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
        '"user_id" INTEGER NOT NULL PRIMARY KEY, '
        '"data" TEXT NOT NULL, '
        'FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE)'
    )
    print(f"  ✓ Added {TABLE}")


def run():
    """Run migration 037.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (table exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: User preferences")
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
