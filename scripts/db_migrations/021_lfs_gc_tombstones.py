#!/usr/bin/env python3
"""
Migration 021: Add the lfs_object_tombstone, lfs_recent_object, lfs_head_pin and
lfs_gc_state tables.

Changes:
- Add lfs_object_tombstone: LFS objects garbage collection deleted or is
  deleting. History rows are no longer deleted with the object, so this is
  what records that the content is gone (see kohakuhub.lfs_gc, #114)
- Add lfs_recent_object (+ touched_at index): LFS objects recently uploaded or
  claimed by a commit, kept through a grace period so a collection cannot
  race an upload or a commit in flight
- Add lfs_head_pin: LFS objects branch heads link that a keep window cannot
  hold (more distinct branch heads on a path than versions kept)
- Add lfs_gc_state: durable garbage collection state. With lfs_auto_gc on,
  nothing is collected until the storage.reconcile_lfs_references task has
  reconciled the LFS references of every branch head once, so data written
  by earlier versions cannot lose content a branch still links

The DDL mirrors what Peewee generates for ``LfsObjectTombstone`` and
``LfsRecentObject``, ``LfsHeadPin`` and ``LfsGcState`` so databases created by init_db() and databases upgraded
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

    Returns True once the last table it creates, lfs_gc_state, exists.
    """
    return check_table_exists(db, "lfs_gc_state")


def _create(timestamp_type, serial):
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
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "lfs_head_pin" ('
        f'"id" {serial} NOT NULL PRIMARY KEY, '
        '"repository_id" INTEGER NOT NULL, '
        '"path_in_repo" VARCHAR(255) NOT NULL, '
        '"sha256" VARCHAR(64) NOT NULL, '
        f'"created_at" {timestamp_type} NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "lfsheadpin_repository_id" ON "lfs_head_pin" ("repository_id")'
    )
    cursor.execute('CREATE INDEX IF NOT EXISTS "lfsheadpin_sha256" ON "lfs_head_pin" ("sha256")')
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "lfsheadpin_repository_id_path_in_repo_sha256" '
        'ON "lfs_head_pin" ("repository_id", "path_in_repo", "sha256")'
    )
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "lfs_gc_state" ('
        '"key" VARCHAR(64) NOT NULL PRIMARY KEY, '
        '"value" TEXT NOT NULL, '
        f'"updated_at" {timestamp_type} NOT NULL)'
    )
    print("  ✓ Created tables")


def migrate_postgres():
    """Create the tables in PostgreSQL."""
    _create("TIMESTAMP", "SERIAL")


def migrate_sqlite():
    """Create the tables in SQLite."""
    _create("DATETIME", "INTEGER")


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
        print("  • Added lfs_object_tombstone, lfs_recent_object, lfs_head_pin and lfs_gc_state (LFS garbage collection)")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
