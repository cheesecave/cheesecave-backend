#!/usr/bin/env python3
"""Migration 027: Add homepage overrides without changing existing application data."""

import importlib.util
import os
import sys
from pathlib import Path

from peewee import PostgresqlDatabase, SqliteDatabase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.db import db
from kohakuhub.config import cfg
from _migration_utils import check_table_exists

MIGRATION_NUMBER = 27
TABLE = "site_homepage"
COLUMN_TYPES = {
    "id": "integer",
    "enabled": "boolean",
    "eyebrow": "varchar(100)",
    "title": "varchar(200)",
    "description": "text",
    "primary_label": "varchar(80)",
    "primary_url": "varchar(2048)",
    "secondary_label": "varchar(80)",
    "secondary_url": "varchar(2048)",
    "illustration": "varchar(20)",
    "animation_enabled": "boolean",
    "show_repositories": "boolean",
}
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS "site_homepage" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "enabled" BOOLEAN,
    "eyebrow" VARCHAR(100),
    "title" VARCHAR(200),
    "description" TEXT,
    "primary_label" VARCHAR(80),
    "primary_url" VARCHAR(2048),
    "secondary_label" VARCHAR(80),
    "secondary_url" VARCHAR(2048),
    "illustration" VARCHAR(20),
    "animation_enabled" BOOLEAN,
    "show_repositories" BOOLEAN
)
"""


def _validate_schema(database):
    columns = {column.name: column for column in database.get_columns(TABLE)}
    if set(columns) != set(COLUMN_TYPES):
        raise RuntimeError(f"Incompatible {TABLE} columns: {sorted(columns)}")
    for name, expected_type in COLUMN_TYPES.items():
        if expected_type == "boolean" and isinstance(database, SqliteDatabase):
            expected_type = "integer"
        column = columns[name]
        actual_type = column.data_type.lower().replace("character varying", "varchar")
        actual_type = "".join(actual_type.split())
        if expected_type.startswith("varchar(") and isinstance(database, PostgresqlDatabase):
            length = database.execute_sql(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                (TABLE, name),
            ).fetchone()
            if length is None:
                raise RuntimeError(f"Missing {TABLE}.{name}")
            actual_type += f"({length[0]})"
        if (
            actual_type != expected_type
            or column.primary_key != (name == "id")
            or column.null != (name != "id")
            or column.default is not None
        ):
            raise RuntimeError(f"Incompatible {TABLE}.{name} definition: {column}")
    if database.get_foreign_keys(TABLE):
        raise RuntimeError(f"Incompatible {TABLE}: unexpected foreign key constraints")


def is_applied(database, config):
    # API startup can create this table before migrations. Require the complete
    # preceding release signature so the migration runner never skips older work.
    predecessor_path = Path(__file__).with_name("026_path_commits.py")
    spec = importlib.util.spec_from_file_location("_homepage_predecessor", predecessor_path)
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    if not predecessor.is_applied(database, config) or not check_table_exists(database, TABLE):
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
            existed = db.table_exists(TABLE)
            if not existed:
                statement = (
                    CREATE_TABLE.replace("BOOLEAN", "INTEGER")
                    if isinstance(db, SqliteDatabase)
                    else CREATE_TABLE
                )
                db.execute_sql(statement)
            _validate_schema(db)
        if existed:
            print("Migration 027: Already applied (site_homepage schema verified)")
        else:
            print("Migration 027: Created site_homepage (no default row inserted)")
        return True
    except Exception as exc:
        print(f"Migration 027 failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
