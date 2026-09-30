#!/usr/bin/env python3
"""
Migration 024: A history operation can hold a repository.

Changes:
- repository.operation: the operation holding the repository (with a token)
- repository.operation_until: until when; an expired hold is free

Super Squash moves a branch in a way LakeFS cannot make conditional; while it
runs, writes to the repository are refused or wait
(kohakuhub.api.repo.utils.operation_lock).

Plain SQL, not the models: this must keep meaning what it means today.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import (
    check_column_exists,
    check_table_exists,
    should_skip_due_to_future_migrations,
)

MIGRATION_NUMBER = 24


def is_applied(db, cfg):
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and its last column exists."""
    if not check_table_exists(db, "lfs_gc_state"):
        return False
    if not check_column_exists(db, cfg, "repository", "main_counted_commit"):
        return False
    return check_column_exists(db, cfg, "repository", "operation_until")


def _migrate(timestamp_type: str):
    cursor = db.cursor()
    print("Adding the operation lock to repository...")
    cursor.execute('ALTER TABLE "repository" ADD COLUMN "operation" VARCHAR(64)')
    cursor.execute(
        f'ALTER TABLE "repository" ADD COLUMN "operation_until" {timestamp_type}'
    )
    print("  ✓ Added repository.operation and repository.operation_until")


def run():
    """Run migration 024.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(
                f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)"
            )
            return True

        if is_applied(db, cfg):
            print(
                f"Migration {MIGRATION_NUMBER}: Already applied (operation lock exists)"
            )
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repository operation lock")
        print("=" * 70)
        with db.atomic():
            _migrate("TIMESTAMP" if cfg.app.db_backend == "postgres" else "DATETIME")
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
