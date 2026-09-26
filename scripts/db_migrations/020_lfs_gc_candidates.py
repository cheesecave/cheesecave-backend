#!/usr/bin/env python3
"""
Migration 020: Add the lfs_gc_candidate table.

Changes:
- Add lfs_gc_candidate table: LFS objects whose last known reference may be
  gone, recorded when a repository row is deleted without the regular delete
  path (for example by force-deleting its owner) and collected by the
  storage.collect_lfs background task (see kohakuhub.storage_cleanup, #109)

The DDL mirrors what Peewee generates for ``LfsGcCandidate`` so databases
created by init_db() and databases upgraded here end up identical.
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

MIGRATION_NUMBER = 20


def is_applied(db, cfg):
    """Check if THIS migration has been applied.

    Returns True if the lfs_gc_candidate table exists.
    """
    return check_table_exists(db, "lfs_gc_candidate")


def _create(timestamp_type):
    cursor = db.cursor()
    print("Creating lfs_gc_candidate table...")
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "lfs_gc_candidate" ('
        '"sha256" VARCHAR(64) NOT NULL PRIMARY KEY, '
        f'"created_at" {timestamp_type} NOT NULL)'
    )
    print("  ✓ Created lfs_gc_candidate table")


def migrate_postgres():
    """Create lfs_gc_candidate table in PostgreSQL."""
    _create("TIMESTAMP")


def migrate_sqlite():
    """Create lfs_gc_candidate table in SQLite."""
    _create("DATETIME")


def run():
    """Run migration 020.

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
            print(f"Migration {MIGRATION_NUMBER}: Already applied (lfs_gc_candidate table exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Add lfs_gc_candidate table")
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
        print("  • Added lfs_gc_candidate table (LFS garbage collection after owner deletion)")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
