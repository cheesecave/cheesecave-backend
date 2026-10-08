"""Migration column probes follow the relation resolved by PostgreSQL search_path."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from peewee import SqliteDatabase
import pytest


@pytest.fixture
def column_probe(db_fresh):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/_migration_utils.py"
    spec = importlib.util.spec_from_file_location("migration_column_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = db_fresh
    backend = "sqlite" if isinstance(database, SqliteDatabase) else "postgres"
    current = extra = None
    if backend == "postgres":
        # db_fresh owns the connection and its schema; add a second schema to the search_path.
        current = database.execute_sql("SELECT current_schema()").fetchone()[0]
        extra = "column_probe_" + uuid4().hex
        database.execute_sql(f'CREATE SCHEMA "{extra}"')
        database.execute_sql(f'SET search_path TO "{current}", "{extra}"')
    schemas = [current, extra]
    config = SimpleNamespace(app=SimpleNamespace(db_backend=backend))
    try:
        yield module, database, config, schemas
    finally:
        if backend == "postgres":
            database.execute_sql(f'SET search_path TO "{current}"')
            database.execute_sql(f'DROP SCHEMA "{extra}" CASCADE')


def test_column_lookup_ignores_same_named_relation_in_other_schema(column_probe):
    module, database, config, schemas = column_probe
    database.execute_sql('CREATE TABLE "migration_probe" ("id" INTEGER PRIMARY KEY)')
    if config.app.db_backend == "postgres":
        database.execute_sql(
            f'CREATE TABLE "{schemas[1]}"."migration_probe" ("id" INTEGER, "new_column" TEXT)'
        )
    assert module.check_column_exists(database, config, "migration_probe", "id")
    assert not module.check_column_exists(database, config, "migration_probe", "new_column")
    database.execute_sql('ALTER TABLE "migration_probe" ADD COLUMN "new_column" TEXT')
    assert module.check_column_exists(database, config, "migration_probe", "new_column")
    database.execute_sql('ALTER TABLE "migration_probe" DROP COLUMN "new_column"')
    assert not module.check_column_exists(database, config, "migration_probe", "new_column")
    assert not module.check_column_exists(database, config, "missing_table", "id")


def test_column_lookup_resolves_later_search_path_schema(column_probe):
    module, database, config, schemas = column_probe
    if config.app.db_backend == "sqlite":
        database.execute_sql('CREATE TABLE "migration_probe" ("id" INTEGER PRIMARY KEY)')
    else:
        database.execute_sql(
            f'CREATE TABLE "{schemas[1]}"."migration_probe" ("id" INTEGER PRIMARY KEY)'
        )
    assert module.check_column_exists(database, config, "migration_probe", "id")
    assert not module.check_column_exists(database, config, "migration_probe", "new_column")
