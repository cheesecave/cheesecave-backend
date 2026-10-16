"""Shared real-database fixtures for unit tests.

Two scopes, both running real SQL on the backend the test profile selects:

* ``fresh_database``: models bound to a new SQLite file, or a new Postgres schema, for
  the length of the block; tables created on entry and dropped on exit. Use it for code
  that commits, for migrations, and for DDL checks.
* ``rolled_back``: runs a block inside a transaction that is always rolled back, so
  the writes of one test never reach the next. Use it for query-shaped tests inside a
  scope created once per module.

The backend comes from ``CHEESE_CAVE_DB_BACKEND`` (or the older ``KOHAKU_HUB_DB_BACKEND``):
``postgres`` reads the URL from ``CHEESE_CAVE_DATABASE_URL`` (or ``KOHAKU_HUB_DATABASE_URL``;
the services CI category sets both); anything else uses a SQLite file in the test's
temporary directory.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from kohakuhub.db import BaseModel
from kohakuhub.envnames import env_or

# Every table model, so one scope can serve any test. create_tables orders them by foreign key.
MODELS = [model for model in BaseModel.__subclasses__()]


def put_the_old_file_key_back(database):
    """The schema before migration 033: the unique key on file (repository_id, path_in_repo).

    A database from ``init_db()`` has no such key, so 033 counts as applied and every earlier
    migration skips itself as superseded. A test that runs an earlier migration for real
    calls this first, inside a transaction it rolls back (the non-main rows other tests
    leave behind cannot sit under that key, so they go inside the same transaction).
    """
    database.execute_sql("DELETE FROM \"file\" WHERE \"branch\" <> 'main'")
    database.execute_sql(
        'CREATE UNIQUE INDEX "file_repository_id_path_in_repo" ON "file" ("repository_id", "path_in_repo")'
    )


class _Rollback(Exception):
    """Raised inside the scope to undo the transaction."""


def postgres_configured():
    """True when the database URL (``CHEESE_CAVE_DATABASE_URL`` or ``KOHAKU_HUB_DATABASE_URL``) names a PostgreSQL server."""
    return env_or("DATABASE_URL", "").startswith("postgresql")


# Revert, reset and squash rewrite history. The API enables them only on PostgreSQL and
# refuses them on any other backend, by design (api/operation_capabilities.py), so the
# tests that drive them run only when the database URL is PostgreSQL.
history_operations_need_postgres = pytest.mark.skipif(
    not postgres_configured(),
    reason="revert, reset and squash are enabled only on PostgreSQL; SQLite refuses them by design",
)


# Real client threads hitting one live server write the database from several server threads at
# once (FastAPI runs its synchronous dependencies in a thread pool, each thread with its own
# connection: token usage, usage counters, commits). SQLite has one writer at a time and
# answers "database is locked" under that load; production runs PostgreSQL.
concurrent_writers_need_postgres = pytest.mark.skipif(
    not postgres_configured(),
    reason="several client threads write through the server at once; SQLite allows one writer",
)


def make_database(tmp_path, name="unit", backend=None):
    """Return ``(database, schema)``. ``schema`` is None for SQLite.

    ``backend`` is ``"sqlite"`` or ``"postgres"``; None reads ``DB_BACKEND`` from the environment.
    """
    if backend is None:
        backend = env_or("DB_BACKEND", "")
    if backend.lower() == "postgres":
        url = urlparse(env_or("DATABASE_URL", ""))
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
