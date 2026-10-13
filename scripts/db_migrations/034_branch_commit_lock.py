#!/usr/bin/env python3
"""
Migration 034: one commit at a time per branch (#56).

Changes:
- branch_commit_lock: the commit in flight on a branch of a repository, with a
  token and the time its lease expires. The unique key on (repository, branch)
  decides who holds the branch.

LakeFS commits whatever is staged on a branch, so two commits staging on the
same branch at once committed each other's files. The commit route now takes a
row here before it stages anything (kohakuhub.api.repo.utils.branch_lock).
No commit is in flight when the new code starts, so nothing is backfilled.

The race also left files without rows or LFS links, which let the collection delete
their objects. A reconciliation links what is still stored; the one that ran before this
version cannot have seen those files, so the migration makes the next one due: it removes
the "reconciled" mark, and uploads then stay protected until a new reconciliation has run.

Plain SQL, not the models: this must keep meaning what it means today. The
index has the name peewee gives it, so init_db() does not build a second one.
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

MIGRATION_NUMBER = 34
TABLE = "branch_commit_lock"
# Two containers starting together would both run CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261013034


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_branch_lock_predecessor", Path(__file__).with_name("033_file_branch_drop_old_key.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and its table exists."""
    return _predecessor_applied(database, config) and check_table_exists(database, TABLE)


def _clear_reconciliation():
    """Make the next reconciliation of LFS references due (see the module docstring)."""
    if check_table_exists(db, "lfs_gc_state"):
        db.cursor().execute('DELETE FROM "lfs_gc_state" WHERE "key" = \'references_reconciled_at\'')
        print("  ✓ A new reconciliation of LFS references is due")


def _migrate(timestamp_type: str, serial: str):
    cursor = db.cursor()
    print("Adding the branch commit lock...")
    cursor.execute(
        f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
        f'"id" {serial} NOT NULL PRIMARY KEY, '
        '"repository_id" INTEGER NOT NULL, '
        '"branch" VARCHAR(255) NOT NULL, '
        '"token" VARCHAR(64) NOT NULL, '
        f'"until" {timestamp_type} NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE)'
    )
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "branchcommitlock_repository_id_branch" '
        f'ON "{TABLE}" ("repository_id", "branch")'
    )
    print(f"  ✓ Added {TABLE}")


def run():
    """Run migration 034.

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
            print(f"Migration {MIGRATION_NUMBER}: Already applied (branch commit lock exists)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Branch commit lock")
        print("=" * 70)
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                # held until the transaction ends; a runner that waited finds the work done
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                if is_applied(db, cfg):
                    print(f"Migration {MIGRATION_NUMBER}: Already applied (by another run)")
                    return True
                _migrate("TIMESTAMP", "SERIAL")
            else:
                _migrate("DATETIME", "INTEGER")
            _clear_reconciliation()
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
