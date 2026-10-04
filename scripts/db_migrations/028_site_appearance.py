#!/usr/bin/env python3
"""Migration 028: Add independent footer and theme overrides, preserving existing settings."""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.db import db
from kohakuhub.config import cfg
from _migration_utils import check_table_exists

MIGRATION_NUMBER = 28
TABLE = "site_appearance"
COLUMN_TYPES = {"id": "integer", "footer": "text", "theme": "text"}
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS "site_appearance" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "footer" TEXT,
    "theme" TEXT
)
"""


def _validate_schema(database):
    columns = {column.name: column for column in database.get_columns(TABLE)}
    if set(columns) != set(COLUMN_TYPES):
        raise RuntimeError(f"Incompatible {TABLE} columns: {sorted(columns)}")
    for name, expected_type in COLUMN_TYPES.items():
        column = columns[name]
        actual_type = "".join(column.data_type.lower().split())
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
    # Startup can create this table before the runner executes. Require the
    # preceding release signature so a new table cannot conceal older upgrades.
    predecessor_path = Path(__file__).with_name("027_site_homepage.py")
    spec = importlib.util.spec_from_file_location("_appearance_predecessor", predecessor_path)
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
                db.execute_sql(CREATE_TABLE)
            _validate_schema(db)
        if existed:
            print("Migration 028: Already applied (site_appearance schema verified)")
        else:
            print("Migration 028: Created site_appearance (no default row inserted)")
        return True
    except Exception as exc:
        print(f"Migration 028 failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
