#!/usr/bin/env python3
"""Migration 043: private invitation mode and read-only account grants for one repository.

The private boolean stays authoritative for HF clients. The column CHECK forbids an
invitation repository from becoming public without also clearing invitation mode. Existing
rows default to ordinary public/private behavior; grants are not inferred. SQL is pinned to
this release so later model changes do not change what this migration does.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_column_exists, check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 43
TABLE = "repository_access_grant"
LOCK_KEY = 20261110043


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_invite_repository_predecessor", Path(__file__).with_name("042_collections.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """A complete predecessor is required so old migrations never skip missing work."""
    return (
        _predecessor_applied(database, config)
        and check_column_exists(database, config, "repository", "invite_mode")
        and check_table_exists(database, TABLE)
    )


DDL = [
    f'''CREATE TABLE IF NOT EXISTS "{TABLE}" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL,
    "user_id" INTEGER NOT NULL,
    "granted_by_id" INTEGER,
    "created_at" TIMESTAMP NOT NULL,
    "expires_at" TIMESTAMP,
    "revoked_at" TIMESTAMP,
    "source" VARCHAR(32) NOT NULL,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("granted_by_id") REFERENCES "user" ("id") ON DELETE SET NULL)''',
    f'CREATE INDEX IF NOT EXISTS "repositoryaccessgrant_repository_id" ON "{TABLE}" ("repository_id")',
    f'CREATE INDEX IF NOT EXISTS "repositoryaccessgrant_user_id" ON "{TABLE}" ("user_id")',
    f'CREATE INDEX IF NOT EXISTS "repositoryaccessgrant_granted_by_id" ON "{TABLE}" ("granted_by_id")',
    f'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryaccessgrant_repository_id_user_id" ON "{TABLE}" ("repository_id", "user_id")',
]


def _migrate():
    if not check_column_exists(db, cfg, "repository", "invite_mode"):
        db.execute_sql(
            'ALTER TABLE "repository" ADD COLUMN "invite_mode" BOOLEAN NOT NULL DEFAULT FALSE '
            'CHECK (NOT invite_mode OR private)'
        )
    for statement in DDL:
        if cfg.app.db_backend != "postgres":
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        db.execute_sql(statement)


def run():
    """Apply the schema atomically; PostgreSQL runners serialize their DDL."""
    db.connect(reuse_if_open=True)
    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True
        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied")
            return True
        with db.atomic():
            if cfg.app.db_backend == "postgres":
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            _migrate()
        print(f"Migration {MIGRATION_NUMBER}: Completed successfully")
        return True
    except Exception as error:
        print(f"Migration {MIGRATION_NUMBER} failed: {error}")
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
