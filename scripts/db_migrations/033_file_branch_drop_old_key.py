#!/usr/bin/env python3
"""Migration 033: the old File key stops being unique (issue #11).

The unique key on ``file (repository_id, path_in_repo)`` is dropped, so a side
branch can hold a row for a path ``main`` also has. The per-branch key from 032,
``(repository_id, branch, path_in_repo)``, stays. Rows are not touched.

The old key's NAME stays taken by an empty, non-unique index (``WHERE FALSE``):
a pre-A image's ``init_db()`` runs ``CREATE UNIQUE INDEX IF NOT EXISTS`` on that
name at boot and would otherwise silently put the unique key back.

Runs at container start with the other migrations. A failing migration stops
the api container, so this one can neither hang nor queue: the DDL needs a brief
exclusive lock on ``file``, taken with a short ``lock_timeout``; a held lock is
retried a few times with backoff, then the migration fails with a specific error.
The check is repeated under the lock, so two containers starting at once leave
one drop and one no-op.
"""

import os
import sys
import time

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_column_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 33
OLD_INDEX = "file_repository_id_path_in_repo"
OLD_COLUMNS = ["repository_id", "path_in_repo"]
NEW_COLUMNS = ["repository_id", "branch", "path_in_repo"]
LOCK_TIMEOUT_MS = 2000  # per attempt; writers queue behind our lock request for at most this
STATEMENT_TIMEOUT_MS = 60000  # the empty guard still scans the table once
BACKOFF_SECONDS = (1, 2, 4, 5, 5)  # between attempts: len + 1 attempts, about 30 s in all

_PG_UNIQUE_KEYS = """
SELECT ic.relname, con.conname
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
LEFT JOIN pg_constraint con
  ON con.conindid = i.indexrelid AND con.conrelid = i.indrelid AND con.contype = 'u'
WHERE i.indrelid = to_regclass('file') AND i.indisunique AND (i.indisvalid OR NOT %s)
  AND ARRAY(
        SELECT a.attname::text
        FROM unnest(i.indkey) WITH ORDINALITY AS k(num, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.num
        ORDER BY k.ord
      ) = %s::text[]
"""


class FileLockTimeout(RuntimeError):
    """The lock on ``file`` was not free in any attempt."""


def _unique_keys(database, config, columns, valid_only=False) -> list[tuple[str, str | None]]:
    """Unique indexes on exactly ``columns``: ``(index, owning constraint or None)``."""
    if config.app.db_backend == "postgres":
        return database.execute_sql(_PG_UNIQUE_KEYS, (valid_only, columns)).fetchall()
    keys = []
    for row in database.execute_sql('PRAGMA index_list("file")').fetchall():
        name, unique = row[1], row[2]
        found = [c[2] for c in database.execute_sql(f'PRAGMA index_info("{name}")').fetchall()]
        if unique and found == columns:
            keys.append((name, None))
    return keys


def _old_keys(database, config):
    return _unique_keys(database, config, OLD_COLUMNS)


def is_applied(database, config) -> bool:
    """Applied once no unique index covers (repository_id, path_in_repo).

    A database from ``init_db()`` never had one. The branch column (032) must exist: without
    it the database is pre-A and the earlier migrations must not skip themselves.
    """
    return check_column_exists(database, config, "file", "branch") and not _old_keys(
        database, config
    )


def _is_lock_timeout(error: Exception) -> bool:
    """PostgreSQL's lock_timeout (55P03) or SQLite's busy timeout."""
    return getattr(error.__context__, "pgcode", None) == "55P03" or "database is locked" in str(error)


def drop_old_key(database, config) -> bool:
    """One attempt. True when it dropped the key, False when another run already had."""
    postgres = config.app.db_backend == "postgres"
    with database.atomic() if postgres else database.atomic("IMMEDIATE"):
        if postgres:
            database.execute_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
            database.execute_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
            database.execute_sql('LOCK TABLE "file" IN ACCESS EXCLUSIVE MODE')
        keys = _old_keys(database, config)  # under the lock: another run may have finished
        if not keys:
            return False
        if not _unique_keys(database, config, NEW_COLUMNS, valid_only=True):
            # Never leave the table without a unique key. The runner goes on after a failed migration.
            raise RuntimeError(
                "the per-branch unique key (repository_id, branch, path_in_repo) from migration 032 "
                "is missing or invalid: 033 does not drop the old key without it"
            )
        for index, constraint in keys:
            if constraint:
                database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{constraint}"')
            else:
                database.execute_sql(f'DROP INDEX "{index}"')
        database.execute_sql(
            f'CREATE INDEX IF NOT EXISTS "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo") WHERE FALSE'
        )
        return True


def run() -> bool:
    try:
        db.connect(reuse_if_open=True)
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True
        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied")
            return True
        print(f"Migration {MIGRATION_NUMBER}: the old File key (repository, path) stops being unique")
        delays = iter(BACKOFF_SECONDS)
        while True:
            try:
                dropped = drop_old_key(db, cfg)
                break
            except Exception as exc:
                if not _is_lock_timeout(exc):
                    raise
                delay = next(delays, None)
                if delay is None:
                    raise FileLockTimeout(
                        f"the lock on table file was not free in {len(BACKOFF_SECONDS) + 1} attempts "
                        f"of {LOCK_TIMEOUT_MS} ms: a long transaction holds it. End it (on PostgreSQL see "
                        "pg_stat_activity) and restart the container; nothing was changed"
                    ) from exc
                print(f"Migration {MIGRATION_NUMBER}: file is locked, retrying in {delay} s")
                time.sleep(delay)
        print(f"Migration {MIGRATION_NUMBER}: ✓ {'Completed' if dropped else 'Already applied (by another run)'}")
        return True
    except Exception as exc:
        print(f"Migration {MIGRATION_NUMBER} failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":  # pragma: no cover - the runner runs this module
    sys.exit(0 if run() else 1)
