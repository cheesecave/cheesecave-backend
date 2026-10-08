"""Shared real-database fixtures for unit tests.

Two scopes, both running real SQL on the backend the test profile selects:

* ``fresh_database``: models bound to a new SQLite file, or a new Postgres schema, for
  the length of the block; tables created on entry and dropped on exit. Use it for code
  that commits, for migrations, and for DDL checks.
* ``rolled_back``: runs a block inside a transaction that is always rolled back, so
  the writes of one test never reach the next. Use it for query-shaped tests inside a
  scope created once per module.

The backend comes from ``KOHAKU_HUB_DB_BACKEND``: ``postgres`` reads the URL from
``KOHAKU_HUB_DATABASE_URL`` (the services CI category sets both); anything else uses a
SQLite file in the test's temporary directory.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

from peewee import PostgresqlDatabase, SqliteDatabase

from kohakuhub.db import BaseModel

# Every table model, so one scope can serve any test. create_tables orders them by foreign key.
MODELS = [model for model in BaseModel.__subclasses__()]


class _Rollback(Exception):
    """Raised inside the scope to undo the transaction."""


def make_database(tmp_path, name="unit"):
    """Return ``(database, schema)``. ``schema`` is None for SQLite."""
    if os.environ.get("KOHAKU_HUB_DB_BACKEND", "").lower() == "postgres":
        url = urlparse(os.environ["KOHAKU_HUB_DATABASE_URL"])
        # One schema per scope: a scope that drops its tables must not remove another scope's.
        schema = f"t_{name}_{uuid.uuid4().hex[:12]}"
        database = PostgresqlDatabase(
            url.path.lstrip("/"),
            user=unquote(url.username or ""),
            password=unquote(url.password or ""),
            host=url.hostname,
            port=url.port or 5432,
            options=f"-c search_path={schema}",
        )
        return database, schema
    return SqliteDatabase(str(tmp_path / f"{name}.db"), pragmas={"foreign_keys": 1}), None


@contextmanager
def fresh_database(database, models=MODELS, schema=None):
    """Bind ``models`` to ``database`` for the block with empty tables."""
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


def clear_tables(database, models=MODELS):
    """Empty every table in ``models`` and reset identity sequences.

    Postgres uses one TRUNCATE (fast); SQLite deletes table by table in reverse dependency order.
    """
    if isinstance(database, PostgresqlDatabase):
        names = ", ".join(f'"{model._meta.table_name}"' for model in models)
        database.execute_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
        return
    for model in reversed(models):
        model.delete().execute()


@contextmanager
def rolled_back(database):
    """Run the block inside a transaction that is always rolled back.

    Errors from the block are re-raised after the rollback.
    """
    try:
        with database.atomic():
            yield database
            raise _Rollback()
    except _Rollback:
        pass


@contextmanager
def table_missing(database, model):
    """Make ``model``'s table disappear for the block, so queries against it fail for real.

    Use this to test error envelopes of code that hits the database. The DROP sits in a
    savepoint that is rolled back on exit, so the table is back for the next statement.
    """
    try:
        with database.atomic():
            # CASCADE drops inbound foreign keys too (Postgres refuses a plain DROP
            # of a referenced table); the savepoint restores both on exit.
            cascade = " CASCADE" if isinstance(database, PostgresqlDatabase) else ""
            database.execute_sql(f'DROP TABLE "{model._meta.table_name}"{cascade}')
            yield
            raise _Rollback()
    except _Rollback:
        pass


def peer_connection(database):
    """A second, independent connection to the same Postgres database and schema.

    For tests that race two sessions, such as a concurrent delete while a read runs.
    """
    return PostgresqlDatabase(database.database, **database.connect_params)
