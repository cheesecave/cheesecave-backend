#!/usr/bin/env python3
"""
Migration 023: Storage usage kept up to date per repository.

Changes:
- repository.main_regular_bytes: the regular files on main
- repository.lfs_bytes: the stored LFS objects its history links, once each
- repository.main_counted_commit: the main commit main_regular_bytes counts
  up to (NULL: not aligned yet)
- index repository(namespace, private): a namespace's usage is summed from
  its repositories

used_bytes is their sum. Earlier versions recomputed usage by listing
repositories after every commit (every repository of the namespace);
kohakuhub.usage now applies each change's difference instead.

The new columns are filled from what the database knows: lfs_bytes exactly,
main_regular_bytes as the rest of the stored used_bytes. A full recount (the
usage.recount background task, scheduled here) then sets exact values from
LakeFS and reports how far they were off; the admin portal shows it.

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

MIGRATION_NUMBER = 23

# Per repository: its history's distinct sha256 objects not tombstoned
STORED_LFS = (
    "SELECT d.repository_id, SUM(d.size) AS total FROM ("
    "SELECT DISTINCT h.repository_id, h.sha256, h.size FROM lfsobjecthistory h "
    "WHERE LENGTH(h.sha256) = 64 "
    'AND h.sha256 NOT IN (SELECT sha256 FROM "lfs_object_tombstone")'
    ") d GROUP BY d.repository_id"
)


def is_applied(db, cfg):
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and its last column exists."""
    if not check_table_exists(db, "lfs_gc_state"):
        return False
    return check_column_exists(db, cfg, "repository", "main_counted_commit")


def _migrate(greatest: str):
    cursor = db.cursor()
    print("Adding the usage counters to repository...")
    for column in ("main_regular_bytes", "lfs_bytes"):
        cursor.execute(
            f'ALTER TABLE "repository" ADD COLUMN "{column}" BIGINT NOT NULL DEFAULT 0'
        )
    cursor.execute(
        'ALTER TABLE "repository" ADD COLUMN "main_counted_commit" VARCHAR(64)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS "repository_namespace_private" '
        'ON "repository" ("namespace", "private")'
    )
    print("Filling them from the stored usage...")
    cursor.execute(
        'UPDATE "repository" SET lfs_bytes = s.total '
        f'FROM ({STORED_LFS}) s WHERE s.repository_id = "repository".id'
    )
    cursor.execute(
        f'UPDATE "repository" SET main_regular_bytes = {greatest}(used_bytes - lfs_bytes, 0)'
    )
    cursor.execute(
        'UPDATE "repository" SET used_bytes = main_regular_bytes + lfs_bytes'
    )

    from kohakuhub import usage

    usage.enqueue_recount()
    print("  ✓ Added the counters and scheduled a full recount (usage.recount)")


def run():
    """Run migration 023.

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
                f"Migration {MIGRATION_NUMBER}: Already applied (usage counters exist)"
            )
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Storage usage kept per repository")
        print("=" * 70)
        with db.atomic():
            _migrate("GREATEST" if cfg.app.db_backend == "postgres" else "MAX")
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
