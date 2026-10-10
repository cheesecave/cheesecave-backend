#!/usr/bin/env python3
"""Migration 032: File rows become per-branch (issue #11, expand).

EXPAND only. Adds ``file.branch`` (existing rows become ``main``; on
PostgreSQL 15 the constant default is metadata only, no rewrite) and a
unique key on ``(repository_id, branch, path_in_repo)``, built CONCURRENTLY
and attached as a constraint. The old key ``(repository_id, path_in_repo)``
is KEPT: old replicas still run ``ON CONFLICT (repository_id, path_in_repo)``
and write no branch column, so their rows land on ``main``. While the old
key exists, a non-main row for a path that main already has is refused. That
is the guard: nothing rewrites main's rows until the contract (contract/033,
gated) drops the old key.

Rollback is ``rollback()`` below. It is not run by the migration runner. It
refuses while any non-main row exists, because the pre-A schema cannot hold
one. Operators run it by hand with the application stopped.

Plain SQL, not the models: this must keep meaning what it means today.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import (
    check_column_exists,
    check_table_exists,
    should_skip_due_to_future_migrations,
)

MIGRATION_NUMBER = 32
NEW_INDEX = "file_repository_id_branch_path_in_repo"
OLD_INDEX = "file_repository_id_path_in_repo"
ROLLOUT_TABLE = "schema_rollout"


def _index_valid(database, name: str) -> bool:
    """True when the named index exists on PostgreSQL and is valid."""
    row = database.execute_sql(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s AND c.relnamespace = to_regnamespace(current_schema())",
        (name,),
    ).fetchone()
    return bool(row and row[0])


def _index_exists(database, name: str) -> bool:
    row = database.execute_sql(
        "SELECT 1 FROM pg_class c WHERE c.relname = %s "
        "AND c.relnamespace = to_regnamespace(current_schema())",
        (name,),
    ).fetchone()
    return row is not None


def _constraint_exists(database, name: str) -> bool:
    row = database.execute_sql(
        "SELECT 1 FROM pg_constraint WHERE conname = %s "
        "AND connamespace = to_regnamespace(current_schema())",
        (name,),
    ).fetchone()
    return row is not None


def is_applied(database, config) -> bool:
    """Applied once the column, the valid new key and the rollout table exist.

    Errors are treated as "not applied" by the runner's skip check. The
    predecessor check (031) is what keeps earlier migrations from skipping
    themselves on this migration's account.
    """
    if config.app.db_backend != "postgres":
        return check_column_exists(database, config, "file", "branch") and check_table_exists(
            database, ROLLOUT_TABLE
        )
    return (
        check_column_exists(database, config, "file", "branch")
        and _index_valid(database, NEW_INDEX)
        and check_table_exists(database, ROLLOUT_TABLE)
    )


def run() -> bool:
    try:
        db.connect(reuse_if_open=True)
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True
        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied")
            return True
        print(f"Migration {MIGRATION_NUMBER}: expand file.branch and the per-branch key")
        expand(db, cfg)
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed")
        return True
    except Exception as exc:
        print(f"Migration {MIGRATION_NUMBER} failed: {exc}", file=sys.stderr)
        return False


def expand(database, config) -> None:
    """Each statement commits on its own (peewee autocommits), so a crash
    part way leaves a state the next run finishes; every step checks first.

    CREATE INDEX CONCURRENTLY cannot run in a transaction, so no atomic block.
    """
    if config.app.db_backend != "postgres":
        # SQLite has no CONCURRENTLY and no constraint attachment; the index is enough.
        if not check_column_exists(database, config, "file", "branch"):
            database.execute_sql(
                "ALTER TABLE \"file\" ADD COLUMN \"branch\" VARCHAR(255) NOT NULL DEFAULT 'main'"
            )
        database.execute_sql(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{NEW_INDEX}" '
            'ON "file" ("repository_id", "branch", "path_in_repo")'
        )
        _create_rollout_table(database)
        return

    if not check_column_exists(database, config, "file", "branch"):
        # Constant default: PostgreSQL 15 records it in the catalog, no rewrite
        database.execute_sql(
            'ALTER TABLE "file" ADD COLUMN "branch" VARCHAR(255) NOT NULL DEFAULT \'main\''
        )
    if _index_exists(database, NEW_INDEX) and not _index_valid(database, NEW_INDEX):
        # An earlier run died during CONCURRENTLY: the invalid index must go first
        database.execute_sql(f'DROP INDEX CONCURRENTLY IF EXISTS "{NEW_INDEX}"')
    if not _index_exists(database, NEW_INDEX):
        database.execute_sql(
            f'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "{NEW_INDEX}" '
            'ON "file" ("repository_id", "branch", "path_in_repo")'
        )
    if not _constraint_exists(database, NEW_INDEX):
        # Attaching an existing index is a catalog change, not a rebuild
        database.execute_sql(
            f'ALTER TABLE "file" ADD CONSTRAINT "{NEW_INDEX}" UNIQUE USING INDEX "{NEW_INDEX}"'
        )
    _create_rollout_table(database)


def _create_rollout_table(database) -> None:
    """The operator's marker table; the contract reads it (see contract/033)."""
    database.execute_sql(
        f'CREATE TABLE IF NOT EXISTS "{ROLLOUT_TABLE}" '
        '("name" VARCHAR(255) NOT NULL PRIMARY KEY, "set_at" TIMESTAMP NOT NULL)'
    )


def rollback(database=None, config=None) -> None:
    """Back to the pre-A schema. Refused while any non-main row exists.

    The application must be stopped first: a writer racing the check would
    make the refusal stale. Restores the old key if the contract dropped it.
    """
    database = database or db
    config = config or cfg
    database.connect(reuse_if_open=True)
    if not check_column_exists(database, config, "file", "branch"):
        return  # nothing to roll back
    count = database.execute_sql(
        'SELECT COUNT(*) FROM "file" WHERE "branch" <> \'main\''
    ).fetchone()[0]
    if count:
        raise RuntimeError(
            f"refusing to roll back issue #11: {count} file row(s) on a non-main branch "
            "cannot exist in the pre-A schema; delete them first (see the PR rollout notes)"
        )
    if config.app.db_backend == "postgres":
        if _constraint_exists(database, NEW_INDEX):
            database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
        database.execute_sql(f'DROP INDEX CONCURRENTLY IF EXISTS "{NEW_INDEX}"')
        if not _index_exists(database, OLD_INDEX):
            database.execute_sql(
                f'CREATE UNIQUE INDEX CONCURRENTLY "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo")'
            )
    else:
        database.execute_sql(f'DROP INDEX IF EXISTS "{NEW_INDEX}"')
        database.execute_sql(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo")'
        )
    database.execute_sql('ALTER TABLE "file" DROP COLUMN "branch"')
    database.execute_sql(f'DROP TABLE IF EXISTS "{ROLLOUT_TABLE}"')


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(0 if run() else 1)
