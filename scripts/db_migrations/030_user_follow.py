#!/usr/bin/env python3
"""Migration 030: local follow relationships; existing activity needs no backfill."""

import importlib.util
from pathlib import Path
import re
import sys

from peewee import PostgresqlDatabase, SqliteDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from kohakuhub.db import db
from kohakuhub.config import cfg

MIGRATION_NUMBER = 30
DDL = [
    """CREATE TABLE IF NOT EXISTS "user_follow" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "follower_id" INTEGER NOT NULL REFERENCES "user"("id") ON DELETE CASCADE,
    "followed_id" INTEGER NOT NULL REFERENCES "user"("id") ON DELETE CASCADE,
    "created_at" TIMESTAMP NOT NULL,
    CHECK (follower_id != followed_id))""",
    'CREATE UNIQUE INDEX IF NOT EXISTS "userfollow_follower_id_followed_id" ON "user_follow" ("follower_id","followed_id")',
    'CREATE INDEX IF NOT EXISTS "userfollow_follower_id_created_at_id" ON "user_follow" ("follower_id","created_at","id")',
    'CREATE INDEX IF NOT EXISTS "userfollow_followed_id_created_at_id" ON "user_follow" ("followed_id","created_at","id")',
]
INDEXES = {
    (("follower_id", "followed_id"), True),
    (("follower_id", "created_at", "id"), False),
    (("followed_id", "created_at", "id"), False),
}


def _validate_schema(database):
    columns = database.get_columns("user_follow")
    if {column.name for column in columns} != {"id", "follower_id", "followed_id", "created_at"}:
        raise RuntimeError("Incompatible user_follow columns")
    for column in columns:
        expected = (
            "integer"
            if column.name != "created_at"
            else "datetime"
            if isinstance(database, SqliteDatabase)
            else "timestamp"
        )
        actual = "".join(column.data_type.lower().split()).replace(
            "timestampwithouttimezone", "timestamp"
        )
        if actual != expected or column.null or column.primary_key != (column.name == "id"):
            raise RuntimeError(f"Incompatible user_follow.{column.name}")
    foreign = {
        (item.column, item.dest_table, item.dest_column)
        for item in database.get_foreign_keys("user_follow")
    }
    if foreign != {("follower_id", "user", "id"), ("followed_id", "user", "id")}:
        raise RuntimeError("Incompatible user_follow foreign keys")
    if isinstance(database, SqliteDatabase):
        actions = database.execute_sql('PRAGMA foreign_key_list("user_follow")').fetchall()
        valid_delete = len(actions) == 2 and all(row[6].upper() == "CASCADE" for row in actions)
        definition = database.execute_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_follow'"
        ).fetchone()[0]
    else:
        actions = database.execute_sql(
            "SELECT c.confdeltype FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE c.contype='f' AND t.relname='user_follow' AND n.nspname=current_schema()"
        ).fetchall()
        valid_delete = len(actions) == 2 and all(row[0] == "c" for row in actions)
        definitions = database.execute_sql(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE c.contype='c' AND t.relname='user_follow' AND n.nspname=current_schema()"
        ).fetchall()
        definition = " ".join(row[0] for row in definitions)
    if not valid_delete:
        raise RuntimeError("Incompatible user_follow foreign keys: expected ON DELETE CASCADE")
    if not re.search(
        r'CHECK\s*\(\s*\(?\s*"?follower_id"?\s*(?:!=|<>)\s*"?followed_id"?\s*\)?\s*\)',
        definition,
        re.IGNORECASE,
    ):
        raise RuntimeError("Incompatible user_follow self-follow constraint")
    indexes = {(tuple(item.columns), item.unique) for item in database.get_indexes("user_follow")}
    if not INDEXES.issubset(indexes):
        raise RuntimeError("Incompatible user_follow indexes")


def is_applied(database, config):
    spec = importlib.util.spec_from_file_location(
        "_follow_predecessor", Path(__file__).with_name("029_repository_discovery.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    if not predecessor.is_applied(database, config):
        return False
    try:
        if not database.table_exists("user_follow"):
            return False
        _validate_schema(database)
    except RuntimeError:
        return False
    return True


def run():
    try:
        db.connect(reuse_if_open=True)
        with db.atomic():
            # Reject incompatible existing tables before attempting any repair.
            if db.table_exists("user_follow"):
                _validate_schema(db)
            for statement in DDL:
                if isinstance(db, SqliteDatabase):
                    statement = statement.replace("SERIAL", "INTEGER").replace(
                        "TIMESTAMP", "DATETIME"
                    )
                db.execute_sql(statement)
            _validate_schema(db)
        print("Migration 030: User follow schema verified (existing rows preserved)")
        return True
    except Exception as exc:
        print(f"Migration 030 failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
