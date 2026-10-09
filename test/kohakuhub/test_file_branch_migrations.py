"""Issue #11, stage 1 (expand/contract): File rows gain a branch.

Each test runs on a new ``db_dual`` database (SQLite and PostgreSQL). The pre-A
``file`` table is rebuilt as raw DDL, matching what peewee generated before this
change, so the expand is checked against the schema production has. Nothing here
runs the migration runner: the migration modules are loaded and bound to the test
database with ``monkeypatch``.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import EXCLUDED, IntegrityError, SqliteDatabase
import pytest

from kohakuhub.db import File
from test.kohakuhub.support import factories

ROOT = Path(__file__).resolve().parents[2]
EXPAND_PATH = ROOT / "scripts/db_migrations/032_file_branch_expand.py"
CONTRACT_PATH = ROOT / "scripts/db_migrations/contract/033_file_branch_contract.py"
NEW_INDEX = "file_repository_id_branch_path_in_repo"
OLD_INDEX = "file_repository_id_path_in_repo"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _postgres(database):
    return not isinstance(database, SqliteDatabase)


def _sql(database, statement):
    """Peewee's raw SQL uses ``%s`` on PostgreSQL and ``?`` on SQLite."""
    return statement if _postgres(database) else statement.replace("%s", "?")


def _bind(monkeypatch, database, module):
    """Point a migration module at the test's database and its backend."""
    backend = "postgres" if _postgres(database) else "sqlite"
    monkeypatch.setattr(module, "db", database)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    return module


def _old_file_table(database):
    """Rebuild ``file`` as it was before issue #11: no branch, only the old key.

    ``lfsobjecthistory`` references ``file``; CASCADE removes that foreign key with
    the table, and these tests never read it.
    """
    database.execute_sql('DROP TABLE "file" CASCADE' if _postgres(database) else 'DROP TABLE "file"')
    id_column = (
        '"id" SERIAL NOT NULL PRIMARY KEY' if _postgres(database) else '"id" INTEGER NOT NULL PRIMARY KEY'
    )
    database.execute_sql(
        f'CREATE TABLE "file" ({id_column}, "repository_id" INTEGER NOT NULL, '
        '"path_in_repo" TEXT NOT NULL, "size" BIGINT NOT NULL, "sha256" VARCHAR(255) NOT NULL, '
        '"lfs" BOOLEAN NOT NULL, "is_deleted" BOOLEAN NOT NULL, "owner_id" INTEGER NOT NULL, '
        '"created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE, '
        'FOREIGN KEY ("owner_id") REFERENCES "user" ("id") ON DELETE CASCADE)'
    )
    database.execute_sql('CREATE INDEX "file_repository_id" ON "file" ("repository_id")')
    database.execute_sql(f'CREATE UNIQUE INDEX "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo")')
    database.execute_sql('DROP TABLE IF EXISTS "schema_rollout"')


def _index_names(database):
    return {index.name for index in database.get_indexes("file")}


def _columns(database):
    return {column.name for column in database.get_columns("file")}


def _old_code_upsert(database, repo, path, sha):
    """The pre-A writer's statement: conflict on (repository, path), no branch column."""
    database.execute_sql(
        _sql(
            database,
            'INSERT INTO "file" ("repository_id", "path_in_repo", "size", "sha256", "lfs", '
            '"is_deleted", "owner_id", "created_at", "updated_at") '
            "VALUES (%s, %s, 1, %s, FALSE, FALSE, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            'ON CONFLICT ("repository_id", "path_in_repo") DO UPDATE SET "sha256" = EXCLUDED."sha256"',
        ),
        (repo.id, path, sha, repo.owner_id),
    )


def _new_upsert(repo, path, sha):
    """The stage-2 writer's shape: conflict on (repository, branch, path)."""
    File.insert_many(
        [
            {
                "repository": repo,
                "branch": "main",
                "path_in_repo": path,
                "size": 1,
                "sha256": sha,
                "lfs": False,
                "is_deleted": False,
                "owner_id": repo.owner_id,
            }
        ]
    ).on_conflict(
        conflict_target=(File.repository, File.branch, File.path_in_repo),
        update={File.sha256: EXCLUDED.sha256},
    ).execute()


def _open_gate(database):
    database.execute_sql(
        _sql(database, 'INSERT INTO "schema_rollout" ("name", "set_at") VALUES (%s, CURRENT_TIMESTAMP)'),
        ("file_branch_contract",),
    )


@pytest.fixture
def pre_a(db_dual, monkeypatch):
    """The expand module bound to a pre-A database, with one repository and one main row."""
    _old_file_table(db_dual)
    module = _bind(monkeypatch, db_dual, _load("file_branch_expand", EXPAND_PATH))
    owner = factories.make_user("owner")
    repo = factories.make_repo(owner, "repo")
    _old_code_upsert(db_dual, repo, "kept.txt", "b" * 64)
    return module, db_dual, repo


@pytest.fixture
def expanded(pre_a):
    module, database, repo = pre_a
    assert module.run() is True
    return module, database, repo


@pytest.fixture
def contract(expanded, monkeypatch):
    """The contract module bound to the expanded database; its expand is the bound one."""
    expand_module, database, repo = expanded
    module = _bind(monkeypatch, database, _load("file_branch_contract", CONTRACT_PATH))
    monkeypatch.setattr(module, "_expand_module", lambda: expand_module)
    return module, database, repo


# --- fresh databases --------------------------------------------------------------


def test_fresh_database_has_the_branch_column_and_both_keys(db_dual):
    """A database built from the models is already in the expanded shape."""
    assert "branch" in _columns(db_dual)
    assert {NEW_INDEX, OLD_INDEX} <= _index_names(db_dual)


def test_fresh_rows_default_to_main(db_dual):
    repo = factories.make_repo(factories.make_user("owner"), "repo")
    row = factories.make_file(repo, "a.bin", "c" * 64)
    assert File.get_by_id(row.id).branch == "main"


# --- expand ------------------------------------------------------------------------


def test_expand_is_not_applied_before_it_runs(pre_a):
    module, database, _repo = pre_a
    assert not module.is_applied(database, module.cfg)
    assert "branch" not in _columns(database)


def test_expand_adds_the_branch_column_the_new_key_and_the_rollout_table(pre_a):
    module, database, _repo = pre_a
    assert module.run() is True
    assert module.is_applied(database, module.cfg)
    assert "branch" in _columns(database)
    assert {NEW_INDEX, OLD_INDEX} <= _index_names(database)
    assert "schema_rollout" in set(database.get_tables())
    rows = database.execute_sql('SELECT "path_in_repo", "branch" FROM "file"').fetchall()
    assert rows == [("kept.txt", "main")]  # existing rows become main


def test_expand_is_idempotent(expanded):
    module, database, _repo = expanded
    before = _index_names(database)
    assert module.run() is True
    assert _index_names(database) == before


def test_old_code_path_writes_main_during_expand(expanded):
    """Old replicas keep working: they conflict on the old key and write branch 'main'."""
    _module, database, repo = expanded
    _old_code_upsert(database, repo, "kept.txt", "d" * 64)  # conflicts on the old key
    _old_code_upsert(database, repo, "new.txt", "e" * 64)  # inserts
    rows = dict(database.execute_sql('SELECT "path_in_repo", "sha256" FROM "file"').fetchall())
    assert rows == {"kept.txt": "d" * 64, "new.txt": "e" * 64}
    branches = {row[0] for row in database.execute_sql('SELECT "branch" FROM "file"').fetchall()}
    assert branches == {"main"}


def test_new_code_path_upserts_on_the_new_key_during_expand(expanded):
    _module, database, repo = expanded
    _new_upsert(repo, "kept.txt", "1" * 64)
    _new_upsert(repo, "kept.txt", "2" * 64)  # conflicts on (repository, branch, path)
    rows = database.execute_sql('SELECT "path_in_repo", "sha256", "branch" FROM "file"').fetchall()
    assert rows == [("kept.txt", "2" * 64, "main")]


def test_non_main_row_for_a_path_main_has_is_refused_during_expand(expanded):
    """The guard: the old key still covers (repository, path), so a branch copy of a
    path main already has cannot be written while the old key exists."""
    _module, database, repo = expanded
    with pytest.raises(IntegrityError):
        with database.atomic():
            File.create(
                repository=repo,
                branch="dev",
                path_in_repo="kept.txt",
                size=1,
                sha256="3" * 64,
                owner_id=repo.owner_id,
            )
    branches = [row[0] for row in database.execute_sql('SELECT "branch" FROM "file"').fetchall()]
    assert branches == ["main"]


def test_non_main_row_for_a_path_main_lacks_is_allowed_during_expand(expanded):
    """Known limit of the expanded state: the old key does not cover a path main lacks.
    The row written is correct (branch = dev); the refusal above guards shared paths only.
    Stage 2 must therefore not write non-main rows before the contract is run."""
    _module, database, repo = expanded
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="dev-only.txt",
        size=1,
        sha256="4" * 64,
        owner_id=repo.owner_id,
    )
    assert File.select().where(File.branch == "dev").count() == 1


# --- rollback ----------------------------------------------------------------------


def test_rollback_refuses_while_a_non_main_row_exists(expanded):
    module, database, repo = expanded
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="dev-only.txt",
        size=1,
        sha256="5" * 64,
        owner_id=repo.owner_id,
    )
    with pytest.raises(RuntimeError, match="1 file row"):
        module.rollback(database, module.cfg)
    assert "branch" in _columns(database)  # nothing changed


def test_rollback_returns_to_the_pre_a_schema_when_only_main_rows_exist(expanded):
    module, database, repo = expanded
    module.rollback(database, module.cfg)
    assert "branch" not in _columns(database)
    assert OLD_INDEX in _index_names(database)
    assert NEW_INDEX not in _index_names(database)
    assert "schema_rollout" not in set(database.get_tables())
    assert not module.is_applied(database, module.cfg)
    _old_code_upsert(database, repo, "kept.txt", "6" * 64)  # the pre-A writer works again


def test_rollback_without_the_column_does_nothing(expanded):
    module, database, _repo = expanded
    module.rollback(database, module.cfg)
    module.rollback(database, module.cfg)  # second call: already rolled back
    assert "branch" not in _columns(database)


def test_rollback_restores_the_old_key_the_contract_dropped(contract):
    module, database, _repo = contract
    expand_module = module._expand_module()
    _open_gate(database)
    assert module.run() is True
    assert OLD_INDEX not in _index_names(database)
    expand_module.rollback(database, expand_module.cfg)
    assert OLD_INDEX in _index_names(database)


# --- contract gate -----------------------------------------------------------------


def test_contract_refused_without_the_marker_and_changes_nothing(contract):
    module, database, _repo = contract
    with pytest.raises(module.ContractGateClosed, match="marker row is missing"):
        module.contract()
    assert OLD_INDEX in _index_names(database)
    assert module.run() is False


def test_contract_refused_when_the_marker_table_is_missing(contract):
    module, database, _repo = contract
    database.execute_sql('DROP TABLE "schema_rollout"')
    assert module.gate_open(database) is False


def test_contract_refused_for_another_marker_name(contract):
    module, database, _repo = contract
    database.execute_sql(
        _sql(database, 'INSERT INTO "schema_rollout" ("name", "set_at") VALUES (%s, CURRENT_TIMESTAMP)'),
        ("some_other_marker",),
    )
    assert module.gate_open(database) is False


def test_contract_with_the_marker_drops_the_old_key_and_allows_branch_copies(contract):
    module, database, repo = contract
    _open_gate(database)
    assert module.run() is True
    assert OLD_INDEX not in _index_names(database)
    assert NEW_INDEX in _index_names(database)
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="kept.txt",  # a path main has: refused before the contract
        size=1,
        sha256="7" * 64,
        owner_id=repo.owner_id,
    )
    assert File.select().where(File.branch == "dev").count() == 1


def test_contract_is_idempotent_after_it_ran(contract):
    module, database, _repo = contract
    _open_gate(database)
    assert module.run() is True
    assert module.run() is True


def test_contract_refused_before_the_expand(pre_a, monkeypatch):
    """The contract never runs on a schema without the new key: that would leave no guard."""
    expand_module, database, _repo = pre_a
    module = _bind(monkeypatch, database, _load("file_branch_contract", CONTRACT_PATH))
    monkeypatch.setattr(module, "_expand_module", lambda: expand_module)
    database.execute_sql(
        'CREATE TABLE "schema_rollout" ("name" VARCHAR(255) NOT NULL PRIMARY KEY, "set_at" TIMESTAMP NOT NULL)'
    )
    _open_gate(database)
    with pytest.raises(RuntimeError, match=r"expand \(032\) is not applied"):
        module.contract()
    assert module.run() is False
    assert OLD_INDEX in _index_names(database)


# --- runner entry points -----------------------------------------------------------


def test_expand_reports_a_failure_instead_of_raising(pre_a, monkeypatch):
    module, _database, _repo = pre_a

    def explode(database, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "expand", explode)
    assert module.run() is False


def test_expand_skips_when_a_future_migration_is_applied(pre_a, monkeypatch):
    module, database, _repo = pre_a
    monkeypatch.setattr(module, "should_skip_due_to_future_migrations", lambda *args: True)
    assert module.run() is True
    assert not module.is_applied(database, module.cfg)


def test_rollback_on_sqlite_recreates_the_old_key(expanded):
    module, database, _repo = expanded
    if _postgres(database):
        pytest.skip("SQLite-only branch of rollback")
    module.rollback(database, module.cfg)
    assert OLD_INDEX in _index_names(database)


def test_rollback_on_postgres_detaches_the_constraint_first(expanded):
    module, database, _repo = expanded
    if not _postgres(database):
        pytest.skip("the constraint is PostgreSQL-only")

    def constraints():
        return database.execute_sql(
            "SELECT COUNT(*) FROM pg_constraint WHERE conname = %s", (NEW_INDEX,)
        ).fetchone()[0]

    assert constraints() == 1
    module.rollback(database, module.cfg)
    assert constraints() == 0


def test_expand_rebuilds_an_invalid_new_index(expanded):
    """A crashed CONCURRENTLY build leaves an invalid index. The next run replaces it.

    The invalid state is made for real in the catalog (indisvalid = false), which needs
    superuser; the test is skipped otherwise.
    """
    module, database, _repo = expanded
    if not _postgres(database):
        pytest.skip("CONCURRENTLY is PostgreSQL-only")
    is_super = database.execute_sql(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()[0]
    if not is_super:
        pytest.skip("marking an index invalid needs superuser")
    database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')  # drops the index too
    database.execute_sql(f'CREATE UNIQUE INDEX "{NEW_INDEX}" ON "file" ("repository_id", "branch", "path_in_repo")')
    database.execute_sql(
        "UPDATE pg_index SET indisvalid = FALSE WHERE indexrelid = "
        f"(SELECT oid FROM pg_class WHERE relname = '{NEW_INDEX}' "
        "AND relnamespace = to_regnamespace(current_schema()))"
    )
    assert not module.is_applied(database, module.cfg)
    assert module.run() is True
    assert module.is_applied(database, module.cfg)


# --- partial runs: a crashed expand is finished by the next run -------------------


def test_expand_finishes_a_run_that_stopped_after_the_index(expanded):
    """Stopped after the index and the constraint, before the rollout table: the next run
    finds the column, the index and the constraint in place and only creates the table."""
    module, database, _repo = expanded
    database.execute_sql('DROP TABLE "schema_rollout"')
    assert not module.is_applied(database, module.cfg)
    assert module.run() is True
    assert module.is_applied(database, module.cfg)
    assert NEW_INDEX in _index_names(database)


def test_expand_finishes_a_run_that_stopped_after_the_column(pre_a):
    """Stopped after the column and the index: no constraint and no rollout table yet."""
    module, database, _repo = pre_a
    database.execute_sql('ALTER TABLE "file" ADD COLUMN "branch" VARCHAR(255) NOT NULL DEFAULT \'main\'')
    database.execute_sql(
        f'CREATE UNIQUE INDEX "{NEW_INDEX}" ON "file" ("repository_id", "branch", "path_in_repo")'
    )
    assert module.run() is True
    assert module.is_applied(database, module.cfg)


def test_rollback_when_the_constraint_is_already_gone(expanded):
    """Postgres only: the constraint owns the new index, so dropping it removes the index too."""
    module, database, repo = expanded
    if not _postgres(database):
        pytest.skip("the constraint is PostgreSQL-only")
    database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
    module.rollback(database, module.cfg)
    assert "branch" not in _columns(database)
    assert OLD_INDEX in _index_names(database)


def test_the_contract_loads_the_real_expand_module(db_dual, monkeypatch):
    """The contract's own copy of the expand is loaded from its path, not stubbed."""
    module = _bind(monkeypatch, db_dual, _load("file_branch_contract", CONTRACT_PATH))
    expand_module = module._expand_module()
    assert hasattr(expand_module, "is_applied") and hasattr(expand_module, "rollback")
