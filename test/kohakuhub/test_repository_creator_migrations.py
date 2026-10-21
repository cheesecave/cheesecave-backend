"""Migration 040 builds the ``repository_creator`` table and fills it for the repositories that already exist.

A table of its own, not a column of ``repository``: a release from before it keeps running on the same
database and ignores the table. The creator of an old repository is inferred from the author of its first
commit on ``main`` when that commit came within a day of the repository, and left unknown otherwise.
Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the schema 039 leaves.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import Repository, RepositoryCreator, User
from test.kohakuhub.support.factories import make_commit, make_org, make_repo, make_user

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/040_repository_creator.py"
CREATED = datetime(2025, 3, 1, 9, 0)


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("repository_creator_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([RepositoryCreator], safe=True)  # the schema before 040
    return module


def _repo(owner, name, **overrides):
    return make_repo(owner, name, created_at=CREATED, **overrides)


def _commit(repo, author, sha, after, **overrides):
    return make_commit(repo, sha, author=author, created_at=CREATED + after, **overrides)


def _creators():
    return {
        row.repository.name: (row.user.username if row.user else None, row.source)
        for row in RepositoryCreator.select()
    }


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("repository_creator")

    assert migration.run() is True

    columns = {c.name: c for c in db_dual.get_columns("repository_creator")}
    assert set(columns) == {"repository_id", "user_id", "source"}
    assert {name for name, c in columns.items() if c.primary_key} == {"repository_id"}
    assert columns["user_id"].null is True
    assert columns["source"].null is False


def test_the_first_commit_on_main_within_a_day_names_the_creator(migration):
    org = make_org("acme")
    alice, bob = make_user("alice"), make_user("bob")
    quick = _repo(org, "quick")
    _commit(quick, alice, "a1", timedelta(hours=1))
    _commit(quick, bob, "a2", timedelta(hours=2))  # a later author is not the creator
    edge = _repo(org, "edge")
    _commit(edge, bob, "b1", timedelta(hours=23, minutes=59))

    assert migration.run() is True

    assert _creators() == {"quick": ("alice", "inferred"), "edge": ("bob", "inferred")}


def test_the_earliest_commit_wins_and_ties_go_to_the_first_row(migration):
    org = make_org("acme")
    alice, bob = make_user("alice"), make_user("bob")
    later_row_first = _repo(org, "order")
    _commit(later_row_first, bob, "c2", timedelta(hours=3))
    _commit(later_row_first, alice, "c1", timedelta(hours=1))  # inserted later, made earlier
    tie = _repo(org, "tie")
    _commit(tie, alice, "t1", timedelta(hours=2))
    _commit(tie, bob, "t2", timedelta(hours=2))

    migration.run()

    assert _creators() == {"order": ("alice", "inferred"), "tie": ("alice", "inferred")}


def test_a_first_commit_after_a_day_a_missing_commit_and_other_branches_leave_it_unknown(migration):
    org = make_org("acme")
    alice, bob = make_user("alice"), make_user("bob")
    late = _repo(org, "late")
    _commit(late, alice, "l1", timedelta(days=1, minutes=1))
    _repo(org, "empty")
    side = _repo(org, "side")
    _commit(side, bob, "s1", timedelta(hours=1), branch="dev")  # only main counts
    main_too = _repo(org, "main-too")
    _commit(main_too, bob, "m1", timedelta(minutes=5), branch="dev")
    _commit(main_too, alice, "m2", timedelta(hours=2))

    migration.run()

    assert _creators() == {"main-too": ("alice", "inferred")}


def test_a_recorded_creator_is_never_overwritten(migration, db_dual):
    org = make_org("acme")
    alice, bob = make_user("alice"), make_user("bob")
    repo = _repo(org, "kept")
    _commit(repo, alice, "k1", timedelta(hours=1))
    migration.run()
    RepositoryCreator.update(user=bob, source="recorded").where(RepositoryCreator.repository == repo).execute()

    assert migration.run() is True

    assert _creators() == {"kept": ("bob", "recorded")}


def test_a_second_run_changes_nothing_and_the_table_counts_as_applied(migration, db_dual):
    org = make_org("acme")
    repo = _repo(org, "again")
    _commit(repo, make_user("alice"), "g1", timedelta(hours=1))
    assert migration.run() is True
    before = _creators()
    assert migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True

    assert _creators() == before == {"again": ("alice", "inferred")}


def test_a_run_after_the_table_exists_still_fills_what_is_missing(migration, db_dual):
    """init_db() may have built the empty table before the migration ran: the fill must not depend on that."""
    db_dual.create_tables([RepositoryCreator], safe=True)
    org = make_org("acme")
    repo = _repo(org, "prebuilt")
    _commit(repo, make_user("alice"), "p1", timedelta(minutes=30))

    assert migration.run() is True

    assert _creators() == {"prebuilt": ("alice", "inferred")}


def test_the_row_goes_with_its_repository_and_its_user_is_released(migration, db_dual):
    org = make_org("acme")
    alice = make_user("alice")
    repo = _repo(org, "gone")
    _commit(repo, alice, "d1", timedelta(hours=1))
    other = _repo(org, "stays")
    _commit(other, alice, "d2", timedelta(hours=1))
    migration.run()

    User.delete().where(User.id == alice.id).execute()  # their commits go too; the row stays, unattributed
    assert _creators() == {"gone": (None, "inferred"), "stays": (None, "inferred")}

    Repository.delete().where(Repository.id == repo.id).execute()
    assert _creators() == {"stays": (None, "inferred")}


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([RepositoryCreator], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("repository_creator")


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False
