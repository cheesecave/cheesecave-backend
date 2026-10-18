"""Migration 038 builds the ``password_reset_token`` table, the way the model declares it.

A table of its own, not columns of ``user``: a release from before it keeps running on the same database and
ignores the table. Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the schema 037 leaves.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import PasswordResetToken, User

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/038_password_reset_tokens.py"


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("password_reset_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([PasswordResetToken], safe=True)  # the schema before 038
    return module


def _row(user, token_hash="h" * 64, **overrides):
    values = {
        "user": user,
        "token_hash": token_hash,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "created_by": "admin",
    }
    values.update(overrides)
    return PasswordResetToken.create(**values)


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("password_reset_token")

    assert migration.run() is True

    assert {c.name for c in db_dual.get_columns("password_reset_token")} == {
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "used_at",
        "created_by",
    }
    assert {c.name for c in db_dual.get_columns("password_reset_token") if c.primary_key} == {"id"}


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    user = User.create(username="reader", normalized_name="reader")

    first = _row(user)
    assert first.used_at is None
    with pytest.raises(IntegrityError):  # a hash is one link
        _row(user)
    db_dual.rollback() if not isinstance(db_dual, SqliteDatabase) else None

    User.delete().where(User.id == user.id).execute()  # the links go with their account
    assert PasswordResetToken.select().count() == 0


def test_a_second_run_is_a_no_op(migration, db_dual):
    assert migration.run() is True
    assert migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([PasswordResetToken], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("password_reset_token")


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False
