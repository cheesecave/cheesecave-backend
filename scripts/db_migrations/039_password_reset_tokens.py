#!/usr/bin/env python3
"""
Migration 039: one-time password reset links.

Changes:
- password_reset_token: one row per link an administrator made (``token_hash``, the hash of the token, never the token;
  ``expires_at``; ``used_at``, set when the link is used and kept as history; ``created_by``), gone with the account.

A table of its own on purpose, as for 035, 037 and 038: a release from before it keeps running on the same database and
ignores the table. Nothing is backfilled. Plain SQL, not the models: this must keep meaning what it means today.
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

MIGRATION_NUMBER = 39
TABLE = "password_reset_token"
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261019039


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_password_reset_predecessor", Path(__file__).with_name("038_admin_auth_client.py")
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
    "id" SERIAL NOT NULL PRIMARY KEY,
    "user_id" INTEGER NOT NULL,
    "token_hash" VARCHAR(255) NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "expires_at" TIMESTAMP NOT NULL,
    "used_at" TIMESTAMP,
    "created_by" VARCHAR(255) NOT NULL,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE)""",
    f'CREATE UNIQUE INDEX IF NOT EXISTS "passwordresettoken_token_hash" ON "{TABLE}" ("token_hash")',
    f'CREATE INDEX IF NOT EXISTS "passwordresettoken_user_id" ON "{TABLE}" ("user_id")',
]


def _migrate():
    cursor = db.cursor()
    print("Adding the password reset token table...")
    for statement in DDL:
        if cfg.app.db_backend != "postgres":
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        cursor.execute(statement)
    print(f"  ✓ Added {TABLE}")


def run():
    """Run migration 039.

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
        print(f"Migration {MIGRATION_NUMBER}: Password reset tokens")
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
