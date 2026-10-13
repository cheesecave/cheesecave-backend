"""Migration 034 builds the branch commit lock table (#56), the way the model declares it.

Each test starts from an emptied ``db_dual`` database (SQLite and PostgreSQL) holding the schema
034 follows: a database that has 033 and no lock table.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase

from kohakuhub.db import BranchCommitLock, File, LfsGcState, Repository, User, utcnow
from test.kohakuhub.support.db import MODELS as ALL_MODELS, put_the_old_file_key_back

MIGRATION = (
    Path(__file__).resolve().parents[2] / "scripts/db_migrations/034_branch_commit_lock.py"
)


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("branch_lock_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_dual.drop_tables(ALL_MODELS, safe=True)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.create_tables([User, Repository, File, LfsGcState])  # 033's schema: no lock table yet
    return module


def _repository(name="repo"):
    owner = User.create(username=f"{name}-owner", normalized_name=f"{name}-owner")
    return Repository.create(
        repo_type="model",
        namespace=owner.username,
        name=name,
        full_id=f"{owner.username}/{name}",
        private=False,
        owner=owner,
    )


def test_it_creates_the_table_and_its_unique_key(migration, db_dual):
    assert not db_dual.table_exists("branch_commit_lock")

    assert migration.run() is True

    assert {c.name for c in db_dual.get_columns("branch_commit_lock")} == {
        "id", "repository_id", "branch", "token", "until",
    }
    indexes = [
        (i.name, tuple(i.columns), i.unique) for i in db_dual.get_indexes("branch_commit_lock")
    ]
    assert ("branchcommitlock_repository_id_branch", ("repository_id", "branch"), True) in indexes


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    repo = _repository()
    BranchCommitLock.create(repository=repo, branch="main", token="t1", until=utcnow())
    BranchCommitLock.create(repository=repo, branch="dev", token="t2", until=utcnow())
    with pytest.raises(IntegrityError):
        BranchCommitLock.create(repository=repo, branch="main", token="t3", until=utcnow())
    # the lease goes with its repository
    Repository.delete().where(Repository.id == repo.id).execute()
    assert BranchCommitLock.select().count() == 0


def test_init_db_builds_no_second_index(migration, db_dual):
    migration.run()
    db_dual.create_tables([BranchCommitLock], safe=True)  # what init_db() does at boot
    keys = [
        i for i in db_dual.get_indexes("branch_commit_lock") if tuple(i.columns) == ("repository_id", "branch")
    ]
    assert len(keys) == 1


def test_a_second_run_changes_nothing(migration, capsys):
    assert migration.run() is True
    capsys.readouterr()

    assert migration.run() is True

    assert "Already applied" in capsys.readouterr().out


def test_it_is_not_applied_before_the_schema_it_follows(migration, db_dual):
    """With the old File key back (the schema before 033) the table alone must not count as
    applied: every earlier migration would skip itself as superseded."""
    put_the_old_file_key_back(db_dual)
    migration._migrate(*(("TIMESTAMP", "SERIAL") if isinstance(db_dual, PostgresqlDatabase) else ("DATETIME", "INTEGER")))

    assert db_dual.table_exists("branch_commit_lock")
    assert migration.is_applied(db_dual, migration.cfg) is False


def test_it_is_applied_once_the_table_exists(migration, db_dual):
    assert migration.is_applied(db_dual, migration.cfg) is False
    migration.run()
    assert migration.is_applied(db_dual, migration.cfg) is True


def test_it_skips_when_a_later_migration_is_applied(migration, db_dual, monkeypatch, capsys):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *args: True)

    assert migration.run() is True

    assert "Skipped" in capsys.readouterr().out
    assert not db_dual.table_exists("branch_commit_lock")


def test_a_failing_migration_reports_failure(migration, db_dual, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("disk full")

    # A deliberate database failure, so the one targeted mock
    monkeypatch.setattr(migration, "_migrate", broken)

    assert migration.run() is False

    assert "failed: disk full" in capsys.readouterr().out


def _mark_reconciled():
    LfsGcState.insert(key="references_reconciled_at", value="then", updated_at=utcnow()).execute()


def _is_marked():
    return LfsGcState.select().where(LfsGcState.key == "references_reconciled_at").exists()


def test_it_makes_the_next_reconciliation_due(migration):
    """Files the old race left unlinked are linked by a reconciliation; the one that ran before
    this version cannot have seen them, so the next one is due."""
    LfsGcState.insert(key="other", value="kept", updated_at=utcnow()).execute()
    _mark_reconciled()

    assert migration.run() is True

    assert not _is_marked()
    assert LfsGcState.get(LfsGcState.key == "other").value == "kept"


def test_a_later_reconciliation_is_not_cleared_again(migration):
    assert migration.run() is True
    _mark_reconciled()  # the reconciliation that followed

    assert migration.run() is True  # a restart: already applied

    assert _is_marked()


def test_it_migrates_a_database_that_never_reconciled(migration, db_dual):
    assert not _is_marked()
    assert migration.run() is True
    assert db_dual.table_exists("branch_commit_lock")


def test_it_migrates_a_database_without_the_state_table(migration, db_dual):
    db_dual.drop_tables([LfsGcState])
    assert migration.run() is True
    assert db_dual.table_exists("branch_commit_lock")
