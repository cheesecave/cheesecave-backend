"""Migration 038 builds the ``admin_auth_client`` table, the way the model declares it.

A table of its own: a release from before it keeps running on the same database and ignores the table
(the admin throttle of a release without it counts in process memory).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import AdminAuthClient

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/038_admin_auth_client.py"


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("admin_auth_client_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([AdminAuthClient], safe=True)  # the schema before 038
    return module


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("admin_auth_client")

    assert migration.run() is True

    columns = {c.name: c for c in db_dual.get_columns("admin_auth_client")}
    assert set(columns) == {"client_key", "failures", "last_failure", "wait_until"}
    assert {name for name, c in columns.items() if c.primary_key} == {"client_key"}


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()

    AdminAuthClient.create(client_key="203.0.113.7", failures=3, last_failure=1000.5, wait_until=0)
    with pytest.raises(IntegrityError):  # one row per client
        AdminAuthClient.create(client_key="203.0.113.7", failures=1, last_failure=1.0, wait_until=0)
    db_dual.rollback() if not isinstance(db_dual, SqliteDatabase) else None

    row = AdminAuthClient.get(AdminAuthClient.client_key == "203.0.113.7")
    assert (row.failures, row.last_failure) == (3, 1000.5)  # a time of day in epoch seconds keeps its fraction
    long_key = "2001:db8:abcd:12::/64"
    AdminAuthClient.create(client_key=long_key, failures=1, last_failure=1.0, wait_until=2.0)
    assert AdminAuthClient.select().count() == 2


def test_a_second_run_is_a_no_op(migration, db_dual):
    assert migration.run() is True
    assert migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([AdminAuthClient], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("admin_auth_client")


def test_it_skips_when_a_later_migration_is_applied(migration, monkeypatch):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *a: True)

    assert migration.run() is True


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False


def test_two_runners_taking_turns_leave_one_table(migration, db_dual, monkeypatch):
    """The second runner finds the table made by the first, once it holds the lock."""
    calls = []
    original = migration.is_applied

    def applied_after_the_first_look(database, config):
        calls.append(1)
        if len(calls) == 2 and migration.cfg.app.db_backend == "postgres":
            migration._migrate()  # the other runner finished while we waited for the lock
        return original(database, config)

    monkeypatch.setattr(migration, "is_applied", applied_after_the_first_look)

    assert migration.run() is True
    assert db_dual.table_exists("admin_auth_client")
