#!/usr/bin/env python3
"""Migration 033: drop the old File key (issue #11, CONTRACT, gated).

Drops ``file_repository_id_path_in_repo``. After this, non-main File rows
can be written: an old replica's ``ON CONFLICT (repository_id, path_in_repo)``
would fail immediately, so this must not run until every replica runs the
code that writes ``branch``.

Not in ``scripts/db_migrations/``: the runner runs every top-level migration
on each container start and exits non-zero when one fails. A closed gate must
not stop the deployment, so the contract is run by the operator only:

    python scripts/db_migrations/contract/033_file_branch_contract.py

The gate is the marker row ``schema_rollout.name = 'file_branch_contract'``.
The operator inserts it only after every replica runs the new code:

    INSERT INTO schema_rollout (name, set_at) VALUES ('file_branch_contract', CURRENT_TIMESTAMP);

Without the row the script refuses with ``ContractGateClosed`` and changes
nothing. It also refuses unless the expand (032) is applied and its new key
is valid, so the old key is never the only guard.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists

MIGRATION_NUMBER = 33
MARKER = "file_branch_contract"
OLD_INDEX = "file_repository_id_path_in_repo"
ROLLOUT_TABLE = "schema_rollout"


class ContractGateClosed(RuntimeError):
    """The operator has not set the marker row."""


def _expand_module():
    path = os.path.join(os.path.dirname(__file__), "..", "032_file_branch_expand.py")
    spec = importlib.util.spec_from_file_location("file_branch_expand", os.path.normpath(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gate_open(database) -> bool:
    """True when the operator's marker row exists."""
    if not check_table_exists(database, ROLLOUT_TABLE):
        return False
    row = database.execute_sql(
        f'SELECT 1 FROM "{ROLLOUT_TABLE}" WHERE "name" = {database.param}', (MARKER,)
    ).fetchone()
    return row is not None


def contract(database=None, config=None) -> None:
    """Drop the old key. Raises ``ContractGateClosed`` or ``RuntimeError``."""
    database = database or db
    config = config or cfg
    database.connect(reuse_if_open=True)
    expand = _expand_module()
    if not expand.is_applied(database, config):
        raise RuntimeError("expand (032) is not applied: run 032 before the contract")
    if not gate_open(database):
        raise ContractGateClosed(
            "contract refused: the marker row is missing. Every replica must run the "
            "new code first; then insert the row "
            f"schema_rollout(name='{MARKER}') and run this script again."
        )
    if config.app.db_backend == "postgres":
        database.execute_sql(f'DROP INDEX CONCURRENTLY IF EXISTS "{OLD_INDEX}"')
    else:
        database.execute_sql(f'DROP INDEX IF EXISTS "{OLD_INDEX}"')


def run() -> bool:
    try:
        contract()
        print(f"Migration {MIGRATION_NUMBER}: old key dropped, non-main File rows enabled")
        return True
    except ContractGateClosed as exc:
        print(f"Migration {MIGRATION_NUMBER}: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"Migration {MIGRATION_NUMBER} failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(0 if run() else 1)
