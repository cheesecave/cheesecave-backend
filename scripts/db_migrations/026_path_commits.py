#!/usr/bin/env python3
"""
Migration 026: Each path's last commit on main.

Changes:
- path_commit (+ unique (repository_id, branch, path) and repository_id
  indexes): the last commit that changed a path on a branch, recorded as
  commits land (kohakuhub.path_commits)
- repository.last_commits_recorded: whether its rows are complete. Existing
  repositories get FALSE: the worker's backfill records them, then sets it

A file listing reads these rows instead of asking LakeFS, whose path-filtered
log scans the whole history.

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

MIGRATION_NUMBER = 26


def is_applied(db, cfg):
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account), the column exists and so does the
    table (which init_db may have made alone)."""
    for table in ("lfs_gc_state", "repository_write", "site_branding"):
        if not check_table_exists(db, table):
            return False
    for column in (
        "main_counted_commit",
        "operation",
        "operation_until",
        "history_root",
        "last_commits_recorded",
    ):
        if not check_column_exists(db, cfg, "repository", column):
            return False
    return check_table_exists(db, "path_commit")


def _migrate(serial: str):
    cursor = db.cursor()
    print("Adding path_commit and repository.last_commits_recorded...")
    if not check_column_exists(db, cfg, "repository", "last_commits_recorded"):
        # Existing repositories have no rows yet: the backfill records them
        cursor.execute(
            'ALTER TABLE "repository" ADD COLUMN "last_commits_recorded" BOOLEAN NOT NULL DEFAULT FALSE'
        )
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS "path_commit" ('
        f'"id" {serial} NOT NULL PRIMARY KEY, '
        '"repository_id" INTEGER NOT NULL, '
        '"branch" VARCHAR(255) NOT NULL, '
        '"path" VARCHAR(255) NOT NULL, '
        '"commit_id" VARCHAR(64) NOT NULL, '
        '"title" TEXT NOT NULL, '
        '"date" BIGINT NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "pathcommit_repository_id" ON "path_commit" ("repository_id")'
    )
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "pathcommit_repository_id_branch_path" '
        'ON "path_commit" ("repository_id", "branch", "path")'
    )
    print("  ✓ Added path_commit and repository.last_commits_recorded")


def run():
    """Run migration 026.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (path_commit exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Last commits per path")
        print("=" * 70)
        with db.atomic():
            _migrate("SERIAL" if cfg.app.db_backend == "postgres" else "INTEGER")
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
