"""Migration 041 builds the ``admin_session`` table, the way the model declares it.

A table of its own, not columns of anything: a release from before it keeps running on the same database and ignores
the table. Nothing is backfilled. Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the schema 040 leaves.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import AdminSession, User

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/041_admin_session.py"


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("admin_session_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([AdminSession], safe=True)  # the schema before 041
    return module


def _row(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "token_hash": "h" * 64,
        "secret_fingerprint": "f" * 64,
        "expires_at": now + timedelta(minutes=30),
        "absolute_expires_at": now + timedelta(hours=12),
    }
    values.update(overrides)
    return AdminSession.create(**values)


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("admin_session")

    assert migration.run() is True

    columns = {c.name: c for c in db_dual.get_columns("admin_session")}
    assert set(columns) == {
        "id",
        "token_hash",
        "principal_type",
        "user_id",
        "secret_fingerprint",
        "created_at",
        "last_used_at",
        "expires_at",
        "absolute_expires_at",
        "ip",
        "user_agent",
        "revoked_at",
    }
    assert {name for name, c in columns.items() if c.primary_key} == {"id"}
    assert columns["user_id"].null and columns["revoked_at"].null and columns["secret_fingerprint"].null
    assert not columns["token_hash"].null and not columns["expires_at"].null


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    first = _row()
    assert first.principal_type == "token" and first.user_id is None and first.revoked_at is None
    with pytest.raises(IntegrityError):  # a hash is one session
        _row()
    if not isinstance(db_dual, SqliteDatabase):
        db_dual.rollback()

    user = User.create(username="admin-person", normalized_name="admin-person")
    _row(token_hash="g" * 64, principal_type="user", user=user)
    User.delete().where(User.id == user.id).execute()  # the sessions go with their account
    assert AdminSession.select().count() == 1


def test_it_builds_the_indexes_the_model_declares(migration, db_dual):
    migration.run()

    names = {index.name for index in db_dual.get_indexes("admin_session")}

    assert {"adminsession_token_hash", "adminsession_expires_at"} <= names


def test_a_second_run_is_a_no_op(migration, db_dual):
    assert migration.run() is True
    assert migration.is_applied(db_dual, migration.cfg)
    _row()

    assert migration.run() is True
    assert AdminSession.select().count() == 1


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([AdminSession], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("admin_session")


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False
