#!/usr/bin/env python3
"""Read-only preflight for the file branch contract (issue #11, migration 033).

Run it before the marker row is inserted (runbook step 3). It checks that the expand
(032) is applied and valid, that the pre-contract key is in place, and prints the file
row counts for the record. It never writes: every check runs in one PostgreSQL
transaction that is READ ONLY.

    python scripts/db_migrations/contract/preflight_033.py

Exit status: 0 when every check passes, 1 when any check fails. Not run by the
migration runner (it is outside ``scripts/db_migrations/`` top level, like the contract).
"""

import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from kohakuhub.config import cfg
from kohakuhub.db import db

NEW_INDEX = "file_repository_id_branch_path_in_repo"
OLD_INDEX = "file_repository_id_path_in_repo"
MARKER = "file_branch_contract"

Check = namedtuple("Check", "name ok detail")
Result = namedtuple("Result", "checks notes")


def _scalar(database, sql, params=()):
    return database.execute_sql(sql, params).fetchone()[0]


def _index(database, name):
    """``(indisvalid, indisunique)`` of the named index in the current schema, or None."""
    return database.execute_sql(
        "SELECT i.indisvalid, i.indisunique FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s AND c.relnamespace = to_regnamespace(current_schema())",
        (name,),
    ).fetchone()


def _branch_column(database):
    return bool(
        _scalar(
            database,
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'file' AND column_name = 'branch'",
        )
    )


def _collect(database):
    checks, notes = [], []
    has_branch = _branch_column(database)
    checks.append(
        Check(
            "expand_032",
            has_branch,
            "file.branch is present"
            if has_branch
            else "file.branch is missing: expand 032 has not run",
        )
    )

    new = _index(database, NEW_INDEX)
    if new is None:
        checks.append(Check("new_key_valid", False, f"{NEW_INDEX} is missing: expand 032 has not built it"))
    elif not new[0]:
        checks.append(
            Check(
                "new_key_valid",
                False,
                f"{NEW_INDEX} is INVALID (an interrupted CONCURRENTLY build): run 032 again to rebuild it",
            )
        )
    else:
        checks.append(Check("new_key_valid", True, f"{NEW_INDEX} is valid"))

    old = _index(database, OLD_INDEX)
    if old is None:
        checks.append(
            Check("old_key", False, f"{OLD_INDEX} is absent: the pre-contract key is gone, or never existed")
        )
    elif not old[1]:
        checks.append(
            Check(
                "old_key",
                False,
                f"{OLD_INDEX} is not unique: the contract already ran and kept its name as a guard",
            )
        )
    else:
        checks.append(Check("old_key", True, f"{OLD_INDEX} is present and unique (pre-contract)"))
    # After the contract the name is a non-unique guard: side rows on shared paths are then normal.
    guard_only = old is not None and not old[1]

    rollout = _scalar(database, "SELECT to_regclass('schema_rollout') IS NOT NULL")
    checks.append(
        Check(
            "rollout_table",
            rollout,
            "schema_rollout exists"
            if rollout
            else "schema_rollout is missing: expand 032 has not created the marker table",
        )
    )

    if not has_branch:
        checks.append(Check("shared_path_rows", False, "not evaluated: file.branch is missing"))
        total = _scalar(database, 'SELECT COUNT(*) FROM "file"')
        notes.append(f"file rows: {total} (no branch column yet)")
    else:
        if not guard_only:
            shared = _scalar(
                database,
                'SELECT COUNT(*) FROM "file" f WHERE f."branch" <> \'main\' AND EXISTS ('
                'SELECT 1 FROM "file" m WHERE m."repository_id" = f."repository_id" '
                'AND m."path_in_repo" = f."path_in_repo" AND m."branch" = \'main\')',
            )
            checks.append(
                Check(
                    "shared_path_rows",
                    shared == 0,
                    f"{shared} non-main row(s) share a path with main; the pre-contract key cannot "
                    "hold them, so this state is not one the expand produces"
                    if shared
                    else "no non-main row shares a path with main",
                )
            )
        else:
            checks.append(
                Check("shared_path_rows", True, "not applicable: the contract already ran")
            )
        main_rows = _scalar(database, 'SELECT COUNT(*) FROM "file" WHERE "branch" = \'main\'')
        other_rows = _scalar(database, 'SELECT COUNT(*) FROM "file" WHERE "branch" <> \'main\'')
        notes.append(f"file rows on main: {main_rows}")
        notes.append(
            f"file rows on non-main branches: {other_rows} "
            "(rollback in place refuses while this is above 0)"
        )

    if rollout:
        marker = _scalar(
            database,
            'SELECT COUNT(*) FROM "schema_rollout" WHERE "name" = %s',
            (MARKER,),
        )
        notes.append(
            f"marker: set ({MARKER}); the contract is open to run"
            if marker
            else f"marker: not set ({MARKER}); insert it at runbook step 4"
        )
    else:
        notes.append(f"marker: not set ({MARKER}); the marker table is missing")
    return Result(checks, notes)


def run_checks(database, config) -> Result:
    """Run every check. Read-only: one READ ONLY PostgreSQL transaction, nothing committed."""
    if config.app.db_backend != "postgres":
        return Result(
            [Check("backend", False, "the preflight reads PostgreSQL catalogs; run it against PostgreSQL")],
            [],
        )
    database.connect(reuse_if_open=True)
    with database.atomic():
        database.execute_sql("SET TRANSACTION READ ONLY")
        return _collect(database)


def main(database=None, config=None) -> int:
    database = db if database is None else database
    config = cfg if config is None else config
    result = run_checks(database, config)
    for check in result.checks:
        print(f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    for note in result.notes:
        print(f"note: {note}")
    failed = [check for check in result.checks if not check.ok]
    if failed:
        print(f"preflight: {len(failed)} check(s) failed; do not run the contract")
        return 1
    print("preflight: all checks passed")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
