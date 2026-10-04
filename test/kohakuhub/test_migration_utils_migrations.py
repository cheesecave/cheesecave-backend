"""Migration column probes follow the relation resolved by PostgreSQL search_path."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from peewee import PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub.db import db


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def column_probe(request):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/_migration_utils.py"
    spec = importlib.util.spec_from_file_location("migration_column_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = (
        SqliteDatabase(":memory:")
        if request.param == "sqlite"
        else PostgresqlDatabase(db.database, **db.connect_params)
    )
    database.connect()
    schemas = ["column_probe_" + uuid4().hex for _ in range(2)]
    if request.param == "postgres":
        for schema in schemas:
            database.execute_sql(f'CREATE SCHEMA "{schema}"')
        database.execute_sql(f'SET search_path TO "{schemas[0]}", "{schemas[1]}"')
    config = SimpleNamespace(app=SimpleNamespace(db_backend=request.param))
    try:
        yield module, database, config, schemas
    finally:
        if request.param == "postgres":
            for schema in schemas:
                database.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        database.close()


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
