"""Issue #11: the operator's read-only preflight for the contract (033).

Each test builds its state on a real database: the pre-A ``file`` table, then the real
expand (032) or the real contract (033). Nothing is mocked. The preflight is PostgreSQL
only (it reads the catalogs), so the state tests skip on SQLite; the backend refusal
is checked on both.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuhub.db import File
from test.kohakuhub.support import factories
from test.kohakuhub.test_file_branch_migrations import (
    CONTRACT_PATH,
    EXPAND_PATH,
    NEW_INDEX,
    OLD_INDEX,
    _load,
    _old_code_upsert,
    _old_file_table,
    _open_gate,
    _postgres,
    _sql,
)

PREFLIGHT_PATH = Path(__file__).resolve().parents[2] / "scripts/db_migrations/contract/preflight_033.py"
preflight = _load("preflight_033", PREFLIGHT_PATH)


def _config(backend):
    return SimpleNamespace(app=SimpleNamespace(db_backend=backend))


@pytest.fixture
def pg(db_fresh):
    if not _postgres(db_fresh):
        pytest.skip("the preflight reads PostgreSQL catalogs")
    return db_fresh


@pytest.fixture
def pre_contract(pg):
    """The pre-A table with one repository and one main row, then the real expand (032)."""
    _old_file_table(pg)
    repo = factories.make_repo(factories.make_user("owner"), "repo")
    _old_code_upsert(pg, repo, "kept.txt", "b" * 64)
    _load("file_branch_expand", EXPAND_PATH).expand(pg, _config("postgres"))
    return SimpleNamespace(database=pg, repo=repo, config=_config("postgres"))


@pytest.fixture
def post_contract(pre_contract):
    """The marker row, then the real contract (033)."""
    _open_gate(pre_contract.database)
    _load("file_branch_contract", CONTRACT_PATH).contract(pre_contract.database, pre_contract.config)
    return pre_contract


def _checks(state):
    result = preflight.run_checks(state.database, state.config)
    return {check.name: check for check in result.checks}, result


def _tables(database):
    return set(database.get_tables())


# --- the pre-contract state passes ------------------------------------------------


def test_preflight_passes_on_the_pre_contract_state(pre_contract, capsys):
    checks, _result = _checks(pre_contract)
    assert all(check.ok for check in checks.values()), checks
    assert preflight.main(pre_contract.database, pre_contract.config) == 0
    out = capsys.readouterr().out
    assert "PASS new_key_valid" in out
    assert "file rows on main: 1" in out
    assert "non-main branches: 0" in out
    assert "marker: not set" in out


def test_preflight_reports_non_main_rows_and_a_set_marker_without_failing(pre_contract, capsys):
    database = pre_contract.database
    File.create(
        repository=pre_contract.repo,
        branch="dev",
        path_in_repo="dev-only.txt",  # not on main: a legitimate side-branch row
        size=1,
        sha256="4" * 64,
        owner_id=pre_contract.repo.owner_id,
    )
    _open_gate(database)
    assert preflight.main(database, pre_contract.config) == 0
    out = capsys.readouterr().out
    assert "non-main branches: 1" in out
    assert "marker: set" in out


# --- each check fails with its reason -----------------------------------------------


def test_preflight_fails_when_expand_032_has_not_run(pg, capsys):
    _old_file_table(pg)  # the pre-A table, no branch column, no rollout table
    checks, _result = _checks(SimpleNamespace(database=pg, config=_config("postgres")))
    assert not checks["expand_032"].ok
    assert "expand 032" in checks["expand_032"].detail
    assert checks["old_key"].ok  # the pre-A key is in place
    assert not checks["rollout_table"].ok
    assert not checks["new_key_valid"].ok and "missing" in checks["new_key_valid"].detail
    assert not checks["shared_path_rows"].ok and "not evaluated" in checks["shared_path_rows"].detail
    assert preflight.main(pg, _config("postgres")) == 1
    out = capsys.readouterr().out
    assert "FAIL expand_032" in out
    assert "no branch column yet" in out
    assert "do not run the contract" in out
    assert "schema_rollout" not in _tables(pg)  # read-only: the table is not created


def test_preflight_fails_when_the_new_key_is_invalid(pre_contract):
    """A CONCURRENTLY build that dies leaves an INVALID index. Made for real: the second
    build meets a duplicate row, so PostgreSQL leaves the index in the catalog, invalid."""
    database = pre_contract.database
    database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
    database.execute_sql(f'DROP INDEX IF EXISTS "{OLD_INDEX}"')
    database.execute_sql(
        'INSERT INTO "file" ("repository_id", "path_in_repo", "size", "sha256", "lfs", '
        '"is_deleted", "owner_id", "created_at", "updated_at", "branch") '
        'SELECT "repository_id", "path_in_repo", "size", "sha256", "lfs", "is_deleted", '
        '"owner_id", "created_at", "updated_at", "branch" FROM "file"'
    )
    with pytest.raises(Exception, match="could not create unique index"):
        database.execute_sql(
            f'CREATE UNIQUE INDEX CONCURRENTLY "{NEW_INDEX}" '
            'ON "file" ("repository_id", "branch", "path_in_repo")'
        )
    checks, _result = _checks(pre_contract)
    assert not checks["new_key_valid"].ok
    assert "INVALID" in checks["new_key_valid"].detail
    assert preflight.main(database, pre_contract.config) == 1


def test_preflight_fails_when_the_new_key_is_missing(pre_contract):
    pre_contract.database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
    checks, _result = _checks(pre_contract)
    assert not checks["new_key_valid"].ok
    assert "missing" in checks["new_key_valid"].detail


def test_preflight_fails_when_the_rollout_table_is_missing(pre_contract):
    database = pre_contract.database
    database.execute_sql('DROP TABLE "schema_rollout"')
    checks, result = _checks(pre_contract)
    assert not checks["rollout_table"].ok
    assert any("table is missing" in note for note in result.notes)  # the marker is not set
    assert "schema_rollout" not in _tables(database)  # read-only: not created


def test_preflight_fails_when_the_pre_contract_key_is_absent(pre_contract):
    pre_contract.database.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
    checks, _result = _checks(pre_contract)
    assert not checks["old_key"].ok
    assert "is absent" in checks["old_key"].detail


def test_preflight_fails_after_the_contract_because_the_key_is_a_guard_now(post_contract, capsys):
    """After 033 the name is a non-unique guard: the pre-contract check must say so."""
    checks, _result = _checks(post_contract)
    assert not checks["old_key"].ok
    assert "not unique" in checks["old_key"].detail
    assert checks["shared_path_rows"].ok and "not applicable" in checks["shared_path_rows"].detail
    assert preflight.main(post_contract.database, post_contract.config) == 1
    assert "FAIL old_key" in capsys.readouterr().out


def test_preflight_fails_on_a_side_row_that_shares_a_path_with_main(pre_contract):
    """The pre-A key refuses such a row, so it can only exist if the key is gone: a broken state."""
    database = pre_contract.database
    database.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
    File.create(
        repository=pre_contract.repo,
        branch="dev",
        path_in_repo="kept.txt",
        size=1,
        sha256="3" * 64,
        owner_id=pre_contract.repo.owner_id,
    )
    checks, _result = _checks(pre_contract)
    assert not checks["shared_path_rows"].ok
    assert "1 non-main row(s) share a path with main" in checks["shared_path_rows"].detail


def test_preflight_refuses_a_non_postgres_configuration_without_touching_the_database(db_fresh, capsys):
    checks, result = _checks(SimpleNamespace(database=db_fresh, config=_config("sqlite")))
    assert list(checks) == ["backend"]
    assert not checks["backend"].ok
    assert result.notes == []
    assert preflight.main(db_fresh, _config("sqlite")) == 1
    assert "PostgreSQL" in capsys.readouterr().out


# --- the contract's own state after it ran is a read-only snapshot ---------------


def test_preflight_leaves_the_database_as_it_found_it(pre_contract):
    database = pre_contract.database

    def snapshot():
        rows = database.execute_sql('SELECT COUNT(*) FROM "file"').fetchone()[0]
        names = sorted(index.name for index in database.get_indexes("file"))
        marker = database.execute_sql(
            _sql(database, 'SELECT COUNT(*) FROM "schema_rollout" WHERE "name" = %s'),
            ("file_branch_contract",),
        ).fetchone()[0]
        return rows, names, marker

    before = snapshot()
    preflight.run_checks(database, pre_contract.config)
    assert snapshot() == before
