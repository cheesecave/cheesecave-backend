"""Migration 037 builds the ``user_preference`` table, the way the model declares it.

A table of its own, not a column of ``user``: a release from before it must keep running on the
same database (it ignores the table), which a changed ``user`` could not promise.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import User, UserPreference

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/037_user_preferences.py"


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("user_preferences_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([UserPreference], safe=True)  # the schema before 037
    return module


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("user_preference")

    assert migration.run() is True

    assert {c.name for c in db_dual.get_columns("user_preference")} == {"user_id", "data"}
    assert {c.name for c in db_dual.get_columns("user_preference") if c.primary_key} == {"user_id"}


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    user = User.create(username="reader", normalized_name="reader")

    UserPreference.create(user=user, data='{"markdown_style": "mdn"}')
    with pytest.raises(IntegrityError):  # one row per account
        UserPreference.create(user=user, data="{}")

    db_dual.rollback() if not isinstance(db_dual, SqliteDatabase) else None
    User.delete().where(User.id == user.id).execute()  # the row goes with its account
    assert UserPreference.select().count() == 0


def test_a_second_run_is_a_no_op(migration, db_dual):
    assert migration.run() is True
    assert migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([UserPreference], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("user_preference")


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False
