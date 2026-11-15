"""Migration 043 gives the LFS history the index the retention decision walks (#153).

``(repository_id, path_in_repo, created_at, id)`` lets the newest rows of a path be read without sorting the whole history
of the path; it replaces the ``(repository_id, path_in_repo)`` index, which it covers. Each test starts from ``db_dual``
(SQLite and PostgreSQL) holding the schema 042 leaves.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import LFSObjectHistory
from test.kohakuhub.support.factories import make_repo, make_user

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/043_lfs_history_window_index.py"
NEW = "lfsobjecthistory_repository_id_path_in_repo_created_at_id"
OLD = "lfsobjecthistory_repository_id_path_in_repo"


def _indexes(db):
    return {index.name: index.columns for index in db.get_indexes("lfsobjecthistory")}


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("history_index_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    # the schema before 043
    db_dual.execute_sql(f'DROP INDEX IF EXISTS "{NEW}"')
    db_dual.execute_sql(f'CREATE INDEX "{OLD}" ON "lfsobjecthistory" ("repository_id", "path_in_repo")')
    return module


def test_the_model_declares_the_index(db_dual):
    indexes = _indexes(db_dual)
    assert indexes[NEW] == ["repository_id", "path_in_repo", "created_at", "id"]
    assert OLD not in indexes


def test_it_replaces_the_path_index_with_the_window_index(migration, db_dual, capsys):
    repo = make_repo(make_user("owner"), "m")
    LFSObjectHistory.create(repository=repo, path_in_repo="a.bin", sha256="a" * 64, size=1, commit_id="c")
    assert not migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True

    indexes = _indexes(db_dual)
    assert indexes[NEW] == ["repository_id", "path_in_repo", "created_at", "id"]
    assert OLD not in indexes
    assert migration.is_applied(db_dual, migration.cfg)
    assert LFSObjectHistory.select().count() == 1  # rows untouched

    assert migration.run() is True
    assert "Already applied" in capsys.readouterr().out


def test_the_newest_rows_of_a_path_come_from_the_index(migration, db_dual):
    migration.run()
    sql, params = (
        LFSObjectHistory.select(LFSObjectHistory.sha256)
        .where((LFSObjectHistory.repository == 1) & (LFSObjectHistory.path_in_repo == "p"))
        .order_by(LFSObjectHistory.created_at.desc(), LFSObjectHistory.id.desc())
        .limit(64)
        .sql()
    )
    explain = "EXPLAIN QUERY PLAN " if isinstance(db_dual, SqliteDatabase) else "EXPLAIN "
    plan = " ".join(str(row) for row in db_dual.execute_sql(explain + sql, params).fetchall())
    assert NEW in plan
    assert "TEMP B-TREE" not in plan.upper() and "Sort" not in plan  # no sort of the whole path


def test_it_waits_for_the_previous_migration(migration, db_dual, monkeypatch):
    monkeypatch.setattr(migration, "_predecessor_applied", lambda database, config: False)
    assert not migration.is_applied(db_dual, migration.cfg)


def test_a_newer_migration_makes_it_skip(migration, monkeypatch, capsys):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *a: True)
    assert migration.run() is True
    assert "Skipped" in capsys.readouterr().out


def test_a_failure_is_reported_not_raised(migration, monkeypatch, capsys):
    def broken():
        raise RuntimeError("disk full")

    monkeypatch.setattr(migration, "_migrate", broken)
    assert migration.run() is False
    assert "disk full" in capsys.readouterr().out
