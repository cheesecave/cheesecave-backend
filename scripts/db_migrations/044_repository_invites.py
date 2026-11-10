#!/usr/bin/env python3
"""Migration 044: applications, hashed repository invitations, exchanges, guests and audit.

Each lifecycle has its own row and expiration. No existing account authorization changes.
Pinned SQL keeps this migration independent of future model changes.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 44
TABLES = (
    "repository_access_request", "repository_invite", "repository_invite_redemption",
    "repository_guest_session", "repository_access_event",
)
LOCK_KEY = 20261110044


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_repository_invites_predecessor", Path(__file__).with_name("043_invite_repository.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def is_applied(database, config) -> bool:
    return _predecessor_applied(database, config) and all(check_table_exists(database, table) for table in TABLES)


DDL = [
    '''CREATE TABLE IF NOT EXISTS "repository_access_request" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL,
    "user_id" INTEGER,
    "email" VARCHAR(320),
    "message" TEXT NOT NULL,
    "status" VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    "receipt_hash" VARCHAR(64),
    "created_at" TIMESTAMP NOT NULL,
    "reviewed_at" TIMESTAMP,
    "reviewed_by_id" INTEGER,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("reviewed_by_id") REFERENCES "user" ("id") ON DELETE SET NULL,
    CHECK (user_id IS NOT NULL OR email IS NOT NULL))''',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessrequest_repository_id" ON "repository_access_request" ("repository_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessrequest_user_id" ON "repository_access_request" ("user_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessrequest_reviewed_by_id" ON "repository_access_request" ("reviewed_by_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryaccessrequest_receipt_hash" ON "repository_access_request" ("receipt_hash")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryaccessrequest_repository_id_user_id" ON "repository_access_request" ("repository_id", "user_id")',
    '''CREATE TABLE IF NOT EXISTS "repository_invite" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL,
    "token_hash" VARCHAR(64) NOT NULL,
    "label" VARCHAR(100) NOT NULL,
    "created_by_id" INTEGER,
    "created_at" TIMESTAMP NOT NULL,
    "expires_at" TIMESTAMP NOT NULL,
    "revoked_at" TIMESTAMP,
    "max_redemptions" INTEGER NOT NULL CHECK (max_redemptions >= 1),
    "redemption_count" INTEGER NOT NULL CHECK (redemption_count >= 0 AND redemption_count <= max_redemptions),
    "max_guest_sessions" INTEGER NOT NULL CHECK (max_guest_sessions >= 0),
    "guest_session_count" INTEGER NOT NULL CHECK (guest_session_count >= 0 AND guest_session_count <= max_guest_sessions),
    "guest_ttl_seconds" INTEGER NOT NULL CHECK (guest_ttl_seconds >= 1 AND guest_ttl_seconds <= 604800),
    "grant_expires_at" TIMESTAMP,
    "access_request_id" INTEGER,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("created_by_id") REFERENCES "user" ("id") ON DELETE SET NULL,
    FOREIGN KEY ("access_request_id") REFERENCES "repository_access_request" ("id") ON DELETE SET NULL)''',
    'CREATE INDEX IF NOT EXISTS "repositoryinvite_repository_id" ON "repository_invite" ("repository_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryinvite_created_by_id" ON "repository_invite" ("created_by_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryinvite_access_request_id" ON "repository_invite" ("access_request_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryinvite_token_hash" ON "repository_invite" ("token_hash")',
    '''CREATE TABLE IF NOT EXISTS "repository_invite_redemption" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "invite_id" INTEGER NOT NULL,
    "user_id" INTEGER NOT NULL,
    "grant_id" INTEGER,
    "created_at" TIMESTAMP NOT NULL,
    FOREIGN KEY ("invite_id") REFERENCES "repository_invite" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("grant_id") REFERENCES "repository_access_grant" ("id") ON DELETE SET NULL)''',
    'CREATE INDEX IF NOT EXISTS "repositoryinviteredemption_invite_id" ON "repository_invite_redemption" ("invite_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryinviteredemption_user_id" ON "repository_invite_redemption" ("user_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryinviteredemption_grant_id" ON "repository_invite_redemption" ("grant_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryinviteredemption_invite_id_user_id" ON "repository_invite_redemption" ("invite_id", "user_id")',
    '''CREATE TABLE IF NOT EXISTS "repository_guest_session" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL,
    "invite_id" INTEGER NOT NULL,
    "token_hash" VARCHAR(64) NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "expires_at" TIMESTAMP NOT NULL,
    "revoked_at" TIMESTAMP,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("invite_id") REFERENCES "repository_invite" ("id") ON DELETE CASCADE)''',
    'CREATE INDEX IF NOT EXISTS "repositoryguestsession_repository_id" ON "repository_guest_session" ("repository_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryguestsession_invite_id" ON "repository_guest_session" ("invite_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryguestsession_token_hash" ON "repository_guest_session" ("token_hash")',
    '''CREATE TABLE IF NOT EXISTS "repository_access_event" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL,
    "actor_id" INTEGER,
    "subject_user_id" INTEGER,
    "invite_id" INTEGER,
    "access_request_id" INTEGER,
    "action" VARCHAR(32) NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "detail" TEXT NOT NULL,
    FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE,
    FOREIGN KEY ("actor_id") REFERENCES "user" ("id") ON DELETE SET NULL,
    FOREIGN KEY ("subject_user_id") REFERENCES "user" ("id") ON DELETE SET NULL,
    FOREIGN KEY ("invite_id") REFERENCES "repository_invite" ("id") ON DELETE SET NULL,
    FOREIGN KEY ("access_request_id") REFERENCES "repository_access_request" ("id") ON DELETE SET NULL)''',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessevent_repository_id" ON "repository_access_event" ("repository_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessevent_actor_id" ON "repository_access_event" ("actor_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessevent_subject_user_id" ON "repository_access_event" ("subject_user_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessevent_invite_id" ON "repository_access_event" ("invite_id")',
    'CREATE INDEX IF NOT EXISTS "repositoryaccessevent_access_request_id" ON "repository_access_event" ("access_request_id")',
]


def _migrate():
    for statement in DDL:
        if cfg.app.db_backend != "postgres":
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        db.execute_sql(statement)


def run():
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
