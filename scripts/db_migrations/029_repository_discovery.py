#!/usr/bin/env python3
"""Migration 029: Create independent README discovery indexes without changing repositories."""

import importlib.util
import os
from pathlib import Path
import sys

from peewee import PostgresqlDatabase, SqliteDatabase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.db import db
from kohakuhub.config import cfg

MIGRATION_NUMBER = 29
DDL = [
    """CREATE TABLE IF NOT EXISTS "repository_metadata" (
    "repository_id" INTEGER NOT NULL PRIMARY KEY REFERENCES "repository"("id") ON DELETE CASCADE,
    "main_sha" VARCHAR(64), "source_repo" VARCHAR(255), "metadata" TEXT NOT NULL,
    "state" VARCHAR(16) NOT NULL, "checked_at" TIMESTAMP, "retry_at" TIMESTAMP,
    "lease_token" VARCHAR(36), "lease_until" TIMESTAMP, "generation" INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS "repository_facet" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "repository_id" INTEGER NOT NULL REFERENCES "repository"("id") ON DELETE CASCADE,
    "key" VARCHAR(16) NOT NULL, "value" VARCHAR(200) NOT NULL)""",
    'CREATE INDEX IF NOT EXISTS "repositorymetadata_state" ON "repository_metadata"("state")',
    'CREATE INDEX IF NOT EXISTS "repositoryfacet_repository_id" ON "repository_facet"("repository_id")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "repositoryfacet_repository_id_key_value" ON "repository_facet"("repository_id","key","value")',
    'CREATE INDEX IF NOT EXISTS "repositoryfacet_key_value_repository_id" ON "repository_facet"("key","value","repository_id")',
]
SCHEMA = {
    "repository_metadata": {
        "repository_id",
        "main_sha",
        "source_repo",
        "metadata",
        "state",
        "checked_at",
        "retry_at",
        "lease_token",
        "lease_until",
        "generation",
    },
    "repository_facet": {"id", "repository_id", "key", "value"},
}
TYPES = {
    "repository_id": "integer",
    "id": "integer",
    "main_sha": "varchar(64)",
    "source_repo": "varchar(255)",
    "metadata": "text",
    "state": "varchar(16)",
    "checked_at": "timestamp",
    "retry_at": "timestamp",
    "lease_token": "varchar(36)",
    "lease_until": "timestamp",
    "generation": "integer",
    "key": "varchar(16)",
    "value": "varchar(200)",
}
NULLABLE = {"main_sha", "source_repo", "checked_at", "retry_at", "lease_token", "lease_until"}
INDEXES = {
    "repository_metadata": {(("state",), False)},
    "repository_facet": {
        (("repository_id",), False),
        (("repository_id", "key", "value"), True),
        (("key", "value", "repository_id"), False),
    },
}


def _validate_schema(database):
    for table, expected in SCHEMA.items():
        if (
            not database.table_exists(table)
            or {column.name for column in database.get_columns(table)} != expected
        ):
            raise RuntimeError(f"Incompatible {table} columns")
        for column in database.get_columns(table):
            expected_type = TYPES[column.name]
            actual_type = "".join(
                column.data_type.lower().replace("character varying", "varchar").split()
            ).replace("timestampwithouttimezone", "timestamp")
            if isinstance(database, SqliteDatabase) and expected_type == "timestamp":
                expected_type = "datetime"
            if isinstance(database, PostgresqlDatabase) and expected_type.startswith("varchar("):
                length = database.execute_sql(
                    "SELECT character_maximum_length FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                    (table, column.name),
                ).fetchone()
                actual_type += f"({length[0]})"
            primary = column.name == ("repository_id" if table == "repository_metadata" else "id")
            if (
                actual_type != expected_type
                or column.null != (column.name in NULLABLE)
                or column.primary_key != primary
            ):
                raise RuntimeError(f"Incompatible {table}.{column.name}")
        foreign = database.get_foreign_keys(table)
        if (
            len(foreign) != 1
            or foreign[0].column != "repository_id"
            or foreign[0].dest_table != "repository"
            or foreign[0].dest_column != "id"
        ):
            raise RuntimeError(f"Incompatible {table} repository foreign key")
        if isinstance(database, SqliteDatabase):
            actions = [
                row[6].upper()
                for row in database.execute_sql(f'PRAGMA foreign_key_list("{table}")').fetchall()
            ]
            valid_delete = actions == ["CASCADE"]
        else:
            actions = database.execute_sql(
                "SELECT con.confdeltype FROM pg_constraint con "
                "JOIN pg_class tbl ON tbl.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = tbl.relnamespace "
                "WHERE con.contype = 'f' AND tbl.relname = %s AND ns.nspname = current_schema()",
                (table,),
            ).fetchall()
            valid_delete = actions == [("c",)]
        if not valid_delete:
            raise RuntimeError(
                f"Incompatible {table} repository foreign key: expected ON DELETE CASCADE"
            )
        indexes = {(tuple(index.columns), index.unique) for index in database.get_indexes(table)}
        if not INDEXES[table].issubset(indexes):
            raise RuntimeError(f"Incompatible {table} indexes")


def is_applied(database, config):
    predecessor_path = Path(__file__).with_name("028_site_appearance.py")
    spec = importlib.util.spec_from_file_location("_discovery_predecessor", predecessor_path)
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    if not predecessor.is_applied(database, config):
        return False
    try:
        _validate_schema(database)
    except RuntimeError:
        return False
    return True


def run():
    try:
        db.connect(reuse_if_open=True)
        with db.atomic():
            for table, expected in SCHEMA.items():
                if (
                    db.table_exists(table)
                    and {column.name for column in db.get_columns(table)} != expected
                ):
                    raise RuntimeError(f"Incompatible {table} columns")
            for statement in DDL:
                if isinstance(db, SqliteDatabase):
                    statement = statement.replace("SERIAL", "INTEGER").replace(
                        "TIMESTAMP", "DATETIME"
                    )
                db.execute_sql(statement)
            _validate_schema(db)
        print("Migration 029: Repository discovery indexes verified (existing rows preserved)")
        return True
    except Exception as exc:
        print(f"Migration 029 failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
