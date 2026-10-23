#!/usr/bin/env python3
"""
Migration 041: browser sessions of the admin panel.

Changes:
- admin_session: one row per session (``token_hash``, the hash of the session id and never the id; ``secret_fingerprint``,
  a keyed hash of the shared admin token the session was made with, so changing that token ends every session;
  ``created_at``, ``last_used_at``, ``expires_at`` (idle end) and ``absolute_expires_at``; ``ip`` and ``user_agent`` for
  the list of sessions; ``revoked_at``). ``principal_type`` and ``user_id`` are for the phase that adds admin accounts:
  a session of the shared token has no user.

A table of its own on purpose, as for 035, 037, 038, 039 and 040: a release from before it keeps running on the same
database and ignores the table. Nothing is backfilled. Plain SQL, not the models: this must keep meaning what it means
today.
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

MIGRATION_NUMBER = 41
TABLE = "admin_session"
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261023041


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_admin_session_predecessor", Path(__file__).with_name("040_repository_creator.py")
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
    "token_hash" VARCHAR(64) NOT NULL,
    "principal_type" VARCHAR(16) NOT NULL,
    "user_id" INTEGER,
    "secret_fingerprint" VARCHAR(64),
    "created_at" TIMESTAMP NOT NULL,
    "last_used_at" TIMESTAMP NOT NULL,
    "expires_at" TIMESTAMP NOT NULL,
    "absolute_expires_at" TIMESTAMP NOT NULL,
    "ip" VARCHAR(64),
    "user_agent" VARCHAR(200),
    "revoked_at" TIMESTAMP,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE)""",
    f'CREATE UNIQUE INDEX IF NOT EXISTS "adminsession_token_hash" ON "{TABLE}" ("token_hash")',
    f'CREATE INDEX IF NOT EXISTS "adminsession_expires_at" ON "{TABLE}" ("expires_at")',
]


def _migrate():
    cursor = db.cursor()
    print("Adding the admin session table...")
    for statement in DDL:
        if cfg.app.db_backend != "postgres":
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        cursor.execute(statement)
    print(f"  ✓ Added {TABLE}")


def run():
    """Run migration 041.

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
        print(f"Migration {MIGRATION_NUMBER}: Admin sessions")
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
