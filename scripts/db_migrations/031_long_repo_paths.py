#!/usr/bin/env python3
"""Migration 031: every repository-path column becomes TEXT.

A path over 255 characters did not fit VARCHAR(255): its File row failed the
commit, and its path_commit row stopped the Last Commit backfill for every
repository after it (cheesecave-backend#1). PostgreSQL turns VARCHAR into
TEXT without rewriting the table or its indexes; SQLite never enforced the
length, so there is nothing to change.
"""

import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from kohakuhub.config import cfg
from kohakuhub.db import db

MIGRATION_NUMBER = 31
COLUMNS = (
    ("file", "path_in_repo"),
    ("path_commit", "path"),
    ("stagingupload", "path_in_repo"),
    ("lfsobjecthistory", "path_in_repo"),
    ("lfs_head_ref", "path_in_repo"),
)


def _predecessor_applied(database, config) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_long_paths_predecessor", Path(__file__).with_name("030_user_follow.py")
    )
    predecessor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(predecessor)
    return predecessor.is_applied(database, config)


def _narrow(database) -> list[tuple[str, str]]:
    """The path columns not yet TEXT; none on SQLite."""
    if cfg.app.db_backend != "postgres":
        return []
    rows = database.execute_sql(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type <> 'text'"
    ).fetchall()
    return [column for column in COLUMNS if column in set(rows)]


def is_applied(database, config) -> bool:
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and no path column is narrow."""
    return _predecessor_applied(database, config) and not _narrow(database)


def run() -> bool:
    try:
        db.connect(reuse_if_open=True)
        with db.atomic():
            narrow = _narrow(db)
            for table, column in narrow:
                db.execute_sql(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TEXT')
        print(f"Migration 031: {len(narrow)} repository-path column(s) widened to TEXT")
        return True
    except Exception as exc:
        print(f"Migration 031 failed: {exc}", file=sys.stderr)
        return False
