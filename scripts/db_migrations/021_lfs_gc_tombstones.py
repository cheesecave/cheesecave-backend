#!/usr/bin/env python3
"""
Migration 021: Add the lfs_object_tombstone and lfs_recent_object tables.

Changes:
- Add lfs_object_tombstone: LFS objects garbage collection deleted or is
  deleting. History rows are no longer deleted with the object, so this is
  what records that the content is gone (see kohakuhub.lfs_gc, #114)
- Add lfs_recent_object (+ touched_at index): LFS objects recently uploaded or
  claimed by a commit, kept through a grace period so a collection cannot
  race an upload or a commit in flight

The DDL mirrors what Peewee generates for ``LfsObjectTombstone`` and
``LfsRecentObject`` so databases created by init_db() and databases upgraded
here end up identical.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 21


def is_applied(db, cfg):
    """Check if THIS migration has been applied.

    Returns True once the last table it creates, lfs_recent_object, exists.
    """
    return check_table_exists(db, "lfs_recent_object")


def _create(timestamp_type):
    cursor = db.cursor()
    print("Creating lfs_object_tombstone and lfs_recent_object tables...")
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "lfs_object_tombstone" ('
        '"sha256" VARCHAR(64) NOT NULL PRIMARY KEY, '
        '"state" VARCHAR(16) NOT NULL, '
        f'"created_at" {timestamp_type} NOT NULL, '
        f'"updated_at" {timestamp_type} NOT NULL)'
    )
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "lfs_recent_object" ('
        '"sha256" VARCHAR(64) NOT NULL PRIMARY KEY, '
        f'"touched_at" {timestamp_type} NOT NULL)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "lfsrecentobject_touched_at" '
        'ON "lfs_recent_object" ("touched_at")'
    )
    print("  ✓ Created tables")


def migrate_postgres():
    """Create the tables in PostgreSQL."""
    _create("TIMESTAMP")


def migrate_sqlite():
    """Create the tables in SQLite."""
    _create("DATETIME")


def run():
    """Run migration 021.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        # Check if should skip due to future migrations
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        # Check if already applied
        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (lfs_recent_object table exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Add LFS tombstones and recent objects")
        print("=" * 70)

        # Run migration in transaction
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                migrate_postgres()
            else:
                migrate_sqlite()

        print("\n" + "=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        print("=" * 70)
        print("\nSummary:")
        print("  • Added lfs_object_tombstone and lfs_recent_object (LFS garbage collection)")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
