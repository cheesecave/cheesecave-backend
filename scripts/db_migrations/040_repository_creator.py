#!/usr/bin/env python3
"""
Migration 040: who created each repository.

Changes:
- repository_creator: one row per repository (``repository_id``, the key; ``user_id``, the person who created it,
  released when their account goes; ``source``). A repository belongs to a user or an organization, and an activity
  feed has to name a person, which the owner of an organization's repository is not.
- The repositories that exist are filled once: the creator of an old repository is taken to be the author of its
  first commit on ``main``, when that commit came within a day of the repository, and ``source`` says ``inferred``.
  A repository without such a commit stays without a row, which the feed shows as an unknown creator. New
  repositories are written by the request that creates them (``source = recorded``) and are never touched here.

A table of its own on purpose, as for 035, 037, 038 and 039: a release from before it keeps running on the same
database and ignores the table. The fill only adds rows for repositories that have none, so running it again (or
after ``init_db()`` built the empty table) is harmless. Plain SQL, not the models: this must keep meaning what it means
today.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 40
TABLE = "repository_creator"
# How soon after a repository a first commit still counts as its creator's
WINDOW = timedelta(days=1)
BATCH = 500
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261021040


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_repository_creator_predecessor", Path(__file__).with_name("039_password_reset_tokens.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations never skip
    themselves on its account) and its table exists."""
    return _predecessor_applied(database, config) and check_table_exists(database, TABLE)


DDL = [
    f"""CREATE TABLE IF NOT EXISTS "{TABLE}" (
    "repository_id" INTEGER NOT NULL PRIMARY KEY,
    "user_id" INTEGER,
    "source" VARCHAR(16) NOT NULL,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE SET NULL)""",
    f'CREATE INDEX IF NOT EXISTS "repositorycreator_user_id" ON "{TABLE}" ("user_id")',
]

# The first commit on main of every repository without a creator row, by time and then by row
FIRST_COMMITS = f"""
SELECT r.id, r.created_at, c.author_id, c.created_at
FROM "repository" r
JOIN "commit" c ON c.id = (
    SELECT c2.id FROM "commit" c2
    WHERE c2.repository_id = r.id AND c2.branch = 'main'
    ORDER BY c2.created_at, c2.id LIMIT 1)
WHERE NOT EXISTS (SELECT 1 FROM "{TABLE}" x WHERE x.repository_id = r.id)
ORDER BY r.id"""


def _as_datetime(value):
    """SQLite hands timestamps back as text; PostgreSQL as datetimes. Both are UTC without a zone."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=None)


def _infer(cursor) -> int:
    cursor.execute(FIRST_COMMITS)
    rows = [
        (repository_id, author_id)
        for repository_id, created, author_id, first in cursor.fetchall()
        if _as_datetime(first) - _as_datetime(created) < WINDOW
    ]
    marker = "%s" if cfg.app.db_backend == "postgres" else "?"
    for start in range(0, len(rows), BATCH):
        cursor.executemany(
            f'INSERT INTO "{TABLE}" ("repository_id", "user_id", "source") VALUES ({marker}, {marker}, \'inferred\')',
            rows[start : start + BATCH],
        )
    return len(rows)


def _migrate():
    cursor = db.cursor()
    print("Adding the repository creator table...")
    for statement in DDL:
        cursor.execute(statement)
    print(f"  ✓ Added {TABLE}")
    print("Inferring the creator of the repositories that exist from their first commit...")
    print(f"  ✓ Filled {_infer(cursor)} repositories")


def run():
    """Run migration 040.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repository creators")
        print("=" * 70)
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                # held until the transaction ends; a runner that waited finds the work done
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
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
