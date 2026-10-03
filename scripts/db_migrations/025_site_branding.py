#!/usr/bin/env python3
"""Migration 025: Add persistent site branding overrides.

Only creates site_branding; existing users, repositories and branding values
are left intact. No default row is inserted, so configuration defaults remain
effective until an administrator saves an override. Frozen SQL keeps this
migration independent of future application model changes.
"""

import os
import sys

from peewee import PostgresqlDatabase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists

MIGRATION_NUMBER = 25
TABLE = "site_branding"
COLUMN_TYPES = {
    "id": "integer",
    "site_name": "varchar(100)",
    "footer_description": "text",
    "header_logo": "text",
    "favicon": "text",
}
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS "site_branding" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "site_name" VARCHAR(100),
    "footer_description" TEXT,
    "header_logo" TEXT,
    "favicon" TEXT
)
"""


def _validate_schema(database):
    """Reject incompatible pre-existing tables instead of silently accepting them."""
    columns = {column.name: column for column in database.get_columns(TABLE)}
    if set(columns) != set(COLUMN_TYPES):
        missing = sorted(set(COLUMN_TYPES) - set(columns))
        unexpected = sorted(set(columns) - set(COLUMN_TYPES))
        raise RuntimeError(
            f"Incompatible {TABLE} columns: missing={missing}, unexpected={unexpected}"
        )
    for name, expected_type in COLUMN_TYPES.items():
        column = columns[name]
        actual_type = column.data_type.lower().replace("character varying", "varchar")
        actual_type = "".join(actual_type.split())
        if name == "site_name" and isinstance(database, PostgresqlDatabase):
            length = database.execute_sql(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "AND column_name = %s",
                (TABLE, name),
            ).fetchone()
            if length is None or length[0] != 100:
                raise RuntimeError(f"Incompatible {TABLE}.{name}: expected VARCHAR(100)")
            actual_type += "(100)"
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
    """Do not let this table hide pending older migrations.

    The runner skips older migrations if ANY later is_applied() returns True.
    API startup may already have created site_branding via init_db(), so require
    the preceding schema signature as well as the complete branding schema.
    """
    if not all(
        check_table_exists(database, table) for table in ("lfs_gc_state", "repository_write")
    ):
        return False
    repository_columns = {column.name for column in database.get_columns("repository")}
    if not {
        "main_counted_commit",
        "operation",
        "operation_until",
        "history_root",
    }.issubset(repository_columns):
        return False
    if not check_table_exists(database, TABLE):
        return False
    try:
        _validate_schema(database)
    except RuntimeError:
        return False
    return True


def run():
    """Create or validate the table transactionally; return False on failure."""
    try:
        db.connect(reuse_if_open=True)
        with db.atomic():
            existed = db.table_exists(TABLE)
            if existed:
                _validate_schema(db)
            else:
                db.execute_sql(CREATE_TABLE)
                _validate_schema(db)
        if existed:
            print("Migration 025: Already applied (site_branding schema verified)")
        else:
            print("Migration 025: Created site_branding (no default row inserted)")
        return True
    except Exception as exc:
        print(f"Migration 025 failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
