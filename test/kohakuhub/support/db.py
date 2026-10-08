"""Shared real-database fixtures for unit tests (prototype).

Two strategies, both using the real SQL engine (no mocked database):

* ``fresh_database``: a new database file (SQLite) or schema per test, models bound for
  the test and dropped afterwards. Simplest, strongest isolation, slowest.
* ``rolled_back_database``: one schema created per module; each test runs inside a
  transaction that is always rolled back. Fast, but code under test must not commit.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from kohakuhub.db import DailyRepoStats, Repository, User

MODELS = [User, Repository, DailyRepoStats]


class _Rollback(Exception):
    """Raised inside the test scope to force the transaction back."""


def make_database(tmp_path, name="unit"):
    """SQLite file by default; a real Postgres server when PROTO_PG_URL is set.

    On Postgres every scope gets its own schema, so one scope dropping its tables cannot
    remove another scope's tables (found by the prototype: two scopes on one schema clash).
    """
    url = os.environ.get("PROTO_PG_URL")
    if url:
        host, port, user, password, dbname = url.split("|")
        schema = f"t_{name}_{os.getpid()}_{id(tmp_path) % 100000}"
        return PostgresqlDatabase(
            dbname, user=user, password=password, host=host, port=int(port),
            options=f"-c search_path={schema}",
        ), schema
    return SqliteDatabase(str(tmp_path / f"{name}.db"), pragmas={"foreign_keys": 1}), None


@contextmanager
def fresh_database(database, models=MODELS, schema=None):
    if schema:
        database.execute_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    with database.bind_ctx(models):
        database.drop_tables(models, safe=True)
        database.create_tables(models)
        try:
            yield database
        finally:
            database.drop_tables(models, safe=True)
            if schema:
                database.execute_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@contextmanager
def rolled_back(database):
    """Run the block inside a transaction that is always rolled back."""
    try:
        with database.atomic():
            yield database
            raise _Rollback()
    except _Rollback:
        pass


@pytest.fixture
def fresh_db(tmp_path):
    database, schema = make_database(tmp_path)
    with fresh_database(database, schema=schema) as db:
        yield db
    database.close()
