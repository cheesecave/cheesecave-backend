#!/usr/bin/env python3
"""
Migration 036: what the files of a repository say about it (#63).

Changes:
- repository_card.info: what is derived from the files and shown only by the repository's own info
  (``config``, ...), JSON; "{}" until the repository is indexed again.
- repository_card.version: the version of the rules that derived this row; 0 for every row that was
  written before this migration (derived without looking at the files), so the next index and the next
  read derive it again. A rules change bumps the number in the code, not in a migration.

Still the table of its own that 035 made, never repository_metadata (see 035). Nothing is backfilled.
Plain SQL, not the models: this must keep meaning what it means today.
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
from _migration_utils import check_column_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 36
TABLE = "repository_card"
COLUMNS = (
    ("info", "TEXT NOT NULL DEFAULT '{}'"),
    ("version", "INTEGER NOT NULL DEFAULT 0"),
)
# Two containers starting together must not both run the ALTERs (the second would fail on a duplicate
# column), so the runners take turns on this advisory lock
LOCK_KEY = 20261015036


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_repository_derived_info_predecessor", Path(__file__).with_name("035_repository_card.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations never skip
    themselves on its account) and both columns exist."""
    return _predecessor_applied(database, config) and all(
        check_column_exists(database, config, TABLE, name) for name, _ in COLUMNS
    )


def _migrate():
    cursor = db.cursor()
    print("Adding the derived-info columns to the repository card table...")
    for name, definition in COLUMNS:
        if check_column_exists(db, cfg, TABLE, name):  # a run that stopped half way
            continue
        cursor.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" {definition}')
        print(f"  ✓ Added {TABLE}.{name}")


def run():
    """Run migration 036.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (columns exist)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repository derived info")
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
