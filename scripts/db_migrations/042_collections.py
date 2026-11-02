#!/usr/bin/env python3
"""
Migration 042: collections.

Changes:
- collection: a curated list (``oid``, the 24 hex characters that end its public slug; ``owner_id``, a user or an
  organization; ``title``, ``slug`` (the title's own slug, unique per owner), ``description``, ``theme``, ``private``,
  ``position`` in the owner's list, ``upvotes_count``, ``created_at``, ``updated_at``).
- collectionitem: one entry (``oid``, the ``_id`` of the item in the API; ``collection_id``; ``item_type``;
  ``repository_id`` or ``child_id`` (another collection), exactly one of them by a CHECK; ``position``; ``note``). It leaves
  with its collection, with its repository and with the collection it refers to, by ON DELETE CASCADE.
- collectionupvote: one row per user and collection.

Tables of their own on purpose, as for 035, 037 to 041: a release from before it keeps running on the same database
and ignores the tables. Nothing is backfilled. Plain SQL, not the models: this must keep meaning what it means today.
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

MIGRATION_NUMBER = 42
TABLES = ("collection", "collectionitem", "collectionupvote")
# Two containers starting together would both CREATE TABLE IF NOT EXISTS, which PostgreSQL does not make safe
# (a unique violation on its catalog), so the runners take turns on this advisory lock
LOCK_KEY = 20261103042


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_collections_predecessor", Path(__file__).with_name("041_admin_session.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations never skip
    themselves on its account) and all its tables exist."""
    return _predecessor_applied(database, config) and all(check_table_exists(database, t) for t in TABLES)


DDL = [
    """CREATE TABLE IF NOT EXISTS "collection" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "oid" VARCHAR(24) NOT NULL,
    "owner_id" INTEGER NOT NULL,
    "title" VARCHAR(60) NOT NULL,
    "slug" VARCHAR(80) NOT NULL,
    "description" VARCHAR(150),
    "theme" VARCHAR(16) NOT NULL,
    "private" BOOLEAN NOT NULL,
    "position" INTEGER NOT NULL,
    "upvotes_count" INTEGER NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    FOREIGN KEY ("owner_id") REFERENCES "user" ("id") ON DELETE CASCADE)""",
    'CREATE UNIQUE INDEX IF NOT EXISTS "collection_oid" ON "collection" ("oid")',
    'CREATE INDEX IF NOT EXISTS "collection_owner_id" ON "collection" ("owner_id")',
    'CREATE INDEX IF NOT EXISTS "collection_slug" ON "collection" ("slug")',
    'CREATE INDEX IF NOT EXISTS "collection_private" ON "collection" ("private")',
    'CREATE INDEX IF NOT EXISTS "collection_updated_at" ON "collection" ("updated_at")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "collection_owner_id_slug" ON "collection" ("owner_id", "slug")',
    """CREATE TABLE IF NOT EXISTS "collectionitem" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "oid" VARCHAR(24) NOT NULL,
    "collection_id" INTEGER NOT NULL,
    "item_type" VARCHAR(16) NOT NULL,
    "repository_id" INTEGER,
    "child_id" INTEGER,
    "position" INTEGER NOT NULL,
    "note" VARCHAR(500),
    "created_at" TIMESTAMP NOT NULL,
    FOREIGN KEY ("collection_id") REFERENCES "collection" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("child_id") REFERENCES "collection" ("id") ON DELETE CASCADE,
    CHECK (("repository_id" IS NULL) <> ("child_id" IS NULL)))""",
    'CREATE UNIQUE INDEX IF NOT EXISTS "collectionitem_oid" ON "collectionitem" ("oid")',
    'CREATE INDEX IF NOT EXISTS "collectionitem_collection_id" ON "collectionitem" ("collection_id")',
    'CREATE INDEX IF NOT EXISTS "collectionitem_repository_id" ON "collectionitem" ("repository_id")',
    'CREATE INDEX IF NOT EXISTS "collectionitem_child_id" ON "collectionitem" ("child_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "collectionitem_collection_id_repository_id" ON "collectionitem" ("collection_id", "repository_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "collectionitem_collection_id_child_id" ON "collectionitem" ("collection_id", "child_id")',
    """CREATE TABLE IF NOT EXISTS "collectionupvote" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "collection_id" INTEGER NOT NULL,
    "user_id" INTEGER NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    FOREIGN KEY ("collection_id") REFERENCES "collection" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE)""",
    'CREATE INDEX IF NOT EXISTS "collectionupvote_collection_id" ON "collectionupvote" ("collection_id")',
    'CREATE INDEX IF NOT EXISTS "collectionupvote_user_id" ON "collectionupvote" ("user_id")',
    'CREATE INDEX IF NOT EXISTS "collectionupvote_created_at" ON "collectionupvote" ("created_at")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "collectionupvote_collection_id_user_id" ON "collectionupvote" ("collection_id", "user_id")',
]


def _migrate():
    cursor = db.cursor()
    print("Adding the collection tables...")
    for statement in DDL:
        if cfg.app.db_backend != "postgres":
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME").replace("BOOLEAN", "INTEGER")
        cursor.execute(statement)
    print(f"  ✓ Added {', '.join(TABLES)}")


def run():
    """Run migration 042.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (tables exist)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Collections")
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
