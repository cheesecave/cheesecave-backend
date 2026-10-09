"""Tests for the shared real-database fixtures in support/db.py (TDD: written first)."""

from __future__ import annotations

import os

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import Repository, User
from test.kohakuhub.support import db as dbsupport


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, monkeypatch):
    """Run the test on each engine. Postgres needs a configured server (services category)."""
    if request.param == "postgres":
        url = os.environ.get("KOHAKU_HUB_DATABASE_URL", "")
        if not url.startswith("postgresql"):
            pytest.skip("no Postgres configured in KOHAKU_HUB_DATABASE_URL")
    monkeypatch.setenv("KOHAKU_HUB_DB_BACKEND", request.param)
    return request.param


def test_sqlite_database_is_used_when_the_backend_is_not_postgres(tmp_path, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_DB_BACKEND", "sqlite")
    database, schema = dbsupport.make_database(tmp_path, name="t")
    assert isinstance(database, SqliteDatabase)
    assert schema is None
    database.close()


def test_explicit_backend_overrides_the_environment(tmp_path, monkeypatch):
    # db_dual asks for each engine in turn, whatever KOHAKU_HUB_DB_BACKEND says.
    monkeypatch.setenv("KOHAKU_HUB_DB_BACKEND", "postgres")
    database, schema = dbsupport.make_database(tmp_path, name="explicit", backend="sqlite")
    assert isinstance(database, SqliteDatabase)
    assert schema is None
    database.close()


def test_postgres_database_gets_a_schema_per_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_DB_BACKEND", "postgres")
    monkeypatch.setenv("KOHAKU_HUB_DATABASE_URL", "postgresql://hub:pw@127.0.0.1:55432/protodb")
    first, first_schema = dbsupport.make_database(tmp_path, name="a")
    second, second_schema = dbsupport.make_database(tmp_path, name="b")
    assert first_schema != second_schema
    assert first.connect_params["host"] == "127.0.0.1"
    assert first.connect_params["port"] == 55432
    assert first.connect_params["user"] == "hub"
    assert first.connect_params["password"] == "pw"
    assert f"search_path={first_schema}" in first.connect_params["options"]
    first.close()
    second.close()


def test_fresh_scope_binds_models_and_restores_previous_bindings(tmp_path, backend):
    before = {m: m._meta.database for m in dbsupport.MODELS}
    database, schema = dbsupport.make_database(tmp_path, name="fresh")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        assert User._meta.database is database
        assert User.select().count() == 0
    database.close()
    assert all(m._meta.database is before[m] for m in dbsupport.MODELS)


def test_fresh_scope_drops_its_tables_on_exit(tmp_path, backend):
    database, schema = dbsupport.make_database(tmp_path, name="drop")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        assert database.table_exists("user")
    assert not database.table_exists("user")
    database.close()


def test_rolled_back_scope_discards_writes_but_keeps_the_schema(tmp_path, backend):
    database, schema = dbsupport.make_database(tmp_path, name="rb")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        with dbsupport.rolled_back(database):
            owner = User.create(username="owner", normalized_name="owner", email="o@example.com")
            Repository.create(repo_type="model", namespace="owner", name="r", full_id="owner/r", owner=owner)
            assert Repository.select().count() == 1
        assert Repository.select().count() == 0
        assert database.table_exists("repository")
    database.close()


def test_rolled_back_scope_propagates_errors_from_the_test_body(tmp_path, backend):
    database, schema = dbsupport.make_database(tmp_path, name="err")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        with pytest.raises(ZeroDivisionError):
            with dbsupport.rolled_back(database):
                User.create(username="x", normalized_name="x", email="x@example.com")
                1 / 0
        assert User.select().count() == 0
    database.close()


def test_factories_create_rows_with_defaults_and_overrides(tmp_path, backend):
    from test.kohakuhub.support import factories

    database, schema = dbsupport.make_database(tmp_path, name="fac")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        owner = factories.make_user("alice")
        repo = factories.make_repo(owner, "demo", private=True)
        assert owner.username == "alice"
        assert repo.full_id == "alice/demo" and repo.private is True and repo.repo_type == "model"
        other = factories.make_repo(owner, "public-one")
        assert other.private is False
    database.close()


def test_daily_stats_factory_defaults_to_no_downloads_and_accepts_overrides(tmp_path, backend):
    from datetime import date

    from test.kohakuhub.support import factories

    database, schema = dbsupport.make_database(tmp_path, name="stats")
    with dbsupport.fresh_database(database, dbsupport.MODELS, schema=schema):
        repo = factories.make_repo(factories.make_user("bob"), "m")
        empty = factories.make_daily_stats(repo, date(2026, 1, 1))
        busy = factories.make_daily_stats(repo, date(2026, 1, 2), download_sessions=7, total_files=9)
        assert empty.download_sessions == 0 and empty.anonymous_downloads == 0
        assert busy.download_sessions == 7 and busy.anonymous_downloads == 7 and busy.total_files == 9
    database.close()
