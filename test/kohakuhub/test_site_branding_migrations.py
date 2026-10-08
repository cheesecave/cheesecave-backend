"""Branding upgrades preserve data and match the model on both supported databases.

Each test starts from an emptied ``db_fresh`` database: 025 checks for the tables before it
(built here as raw DDL), so no model table may already exist.
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from peewee import SqliteDatabase
import pytest

from kohakuhub.db import SiteBranding
from kohakuhub import db as db_module, site_branding
from test.kohakuhub.support.db import MODELS as ALL_MODELS


MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "db_migrations"
LOCK_COLUMNS = {
    "main_counted_commit": "VARCHAR(64)",
    "history_root": "VARCHAR(64)",
    "operation": "VARCHAR(64)",
    "operation_until": "TIMESTAMP",
}


def load_migration():
    path = MIGRATIONS / "025_site_branding.py"
    spec = importlib.util.spec_from_file_location("branding_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _empty(database):
    """Drop every model table: migration history starts from no tables at all."""
    database.drop_tables(ALL_MODELS, safe=True)


def _bind(monkeypatch, database, *modules):
    """Point the migration modules at the test's database and its backend."""
    backend = "sqlite" if isinstance(database, SqliteDatabase) else "postgres"
    for module in modules:
        monkeypatch.setattr(module, "db", database)
        monkeypatch.setattr(
            module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend))
        )
    return database


@pytest.fixture
def empty_db(db_fresh):
    """A new database for this test, with no tables (see ``_empty``)."""
    _empty(db_fresh)
    return db_fresh


@pytest.fixture
def migration(empty_db, monkeypatch):
    module = load_migration()
    _bind(monkeypatch, empty_db, module)
    return module, empty_db


def schema_signature(connection):
    return (
        {
            column.name: (column.data_type.lower(), column.null, column.primary_key, column.default)
            for column in connection.get_columns("site_branding")
        },
        connection.get_foreign_keys("site_branding"),
    )


def previous_schema(connection, missing=None):
    connection.execute_sql('CREATE TABLE "lfs_gc_state" ("id" INTEGER PRIMARY KEY)')
    connection.execute_sql('CREATE TABLE "repository_write" ("id" INTEGER PRIMARY KEY)')
    columns = [f'"{name}" {kind}' for name, kind in LOCK_COLUMNS.items() if name != missing]
    connection.execute_sql(
        'CREATE TABLE "repository" ("id" INTEGER PRIMARY KEY, ' + ", ".join(columns) + ")"
    )
    if missing == "history_root":
        connection.execute_sql('INSERT INTO "repository" ("id") VALUES (7)')
    else:
        connection.execute_sql(
            'INSERT INTO "repository" ("id", "history_root") VALUES (7, \'keep-me\')'
        )


def test_upgrade_matches_model_and_preserves_existing_data(migration):
    module, connection = migration
    previous_schema(connection)
    assert module.is_applied(connection, module.cfg) is False
    assert module.run() is True
    assert module.is_applied(connection, module.cfg) is True
    actual = schema_signature(connection)
    assert connection.execute_sql('SELECT COUNT(*) FROM "site_branding"').fetchone() == (0,)
    assert connection.execute_sql('SELECT "id", "history_root" FROM "repository"').fetchall() == [
        (7, "keep-me")
    ]

    with connection.bind_ctx([SiteBranding]):
        # Explicitly preserve both inline vector and animation bytes across retries.
        values = {
            "site_name": "Existing Hub",
            "footer_description": "An existing introduction",
            "header_logo": "data:image/svg+xml;base64,PHN2Zy8+",
            "favicon": "data:image/gif;base64,R0lGODlh",
        }
        site_branding.update_branding(values)
        assert module.run() is True
        record = SiteBranding.get_by_id(1)
        assert {name: getattr(record, name) for name in values} == values
        SiteBranding.drop_table()
        connection.create_tables([SiteBranding])
        assert schema_signature(connection) == actual


def test_existing_model_table_is_accepted_without_seeding(migration):
    module, connection = migration
    previous_schema(connection)
    with connection.bind_ctx([SiteBranding]):
        connection.create_tables([SiteBranding])
    assert module.is_applied(connection, module.cfg) is True
    assert module.run() is True
    assert connection.execute_sql('SELECT COUNT(*) FROM "site_branding"').fetchone() == (0,)


@pytest.mark.parametrize("missing", list(LOCK_COLUMNS))
def test_branding_table_cannot_hide_pending_historical_migrations(migration, missing):
    module, connection = migration
    previous_schema(connection, missing=missing)
    with connection.bind_ctx([SiteBranding]):
        connection.create_tables([SiteBranding])
    assert module.is_applied(connection, module.cfg) is False


def test_branding_table_alone_does_not_supersede_old_migrations(migration):
    module, connection = migration
    with connection.bind_ctx([SiteBranding]):
        connection.create_tables([SiteBranding])
    assert module.is_applied(connection, module.cfg) is False


def test_incomplete_table_fails_without_discarding_data(migration):
    module, connection = migration
    previous_schema(connection)
    connection.execute_sql(
        'CREATE TABLE "site_branding" ("id" INTEGER PRIMARY KEY, "site_name" TEXT)'
    )
    connection.execute_sql("INSERT INTO \"site_branding\" VALUES (1, 'Keep this name')")
    assert module.is_applied(connection, module.cfg) is False
    assert module.run() is False
    assert connection.execute_sql('SELECT * FROM "site_branding"').fetchall() == [
        (1, "Keep this name")
    ]


def test_ddl_failure_is_reported(migration, monkeypatch):
    module, connection = migration

    # Targeted mock on purpose: an outage of the DDL call itself is not reproducible with a
    # real SQL error that leaves the connection usable, so the failing call is injected here.
    def fail(*args, **kwargs):
        raise RuntimeError("DDL unavailable")

    monkeypatch.setattr(connection, "execute_sql", fail)
    assert module.run() is False


def test_schema_validation_accepts_legacy_peewee_metadata(migration, monkeypatch):
    module, connection = migration
    previous_schema(connection)
    with connection.bind_ctx([SiteBranding]):
        connection.create_tables([SiteBranding])
    get_columns = connection.get_columns

    # Reshape the real metadata into the older peewee column objects (only the five attributes
    # the validator reads); the rows and types still come from the database.
    def legacy_columns(table, *args, **kwargs):
        return [
            SimpleNamespace(
                name=column.name,
                data_type=column.data_type,
                null=column.null,
                primary_key=column.primary_key,
                default=column.default,
            )
            for column in get_columns(table, *args, **kwargs)
        ]

    monkeypatch.setattr(connection, "get_columns", legacy_columns)
    assert module.is_applied(connection, module.cfg) is True
    assert module.run() is True


def test_full_runner_upgrades_previous_schema_and_retries(db_fresh):
    # The runner subprocess reaches the database through a SQLite URL, so it needs the file
    # path of db_fresh; a Postgres schema cannot be named that way.
    if not isinstance(db_fresh, SqliteDatabase):
        pytest.skip("SQLite file path for the subprocess")
    _empty(db_fresh)
    path = Path(db_fresh.database)
    connection = db_fresh
    models = [
        model
        for model in vars(db_module).values()
        if isinstance(model, type)
        and issubclass(model, db_module.BaseModel)
        and model not in (db_module.BaseModel, SiteBranding)
    ]
    with connection.bind_ctx(models):
        connection.create_tables(models)
    connection.close()
    env = os.environ.copy()
    env.update(KOHAKU_HUB_DB_BACKEND="sqlite", KOHAKU_HUB_DATABASE_URL=f"sqlite:///{path}")
    command = [sys.executable, str(MIGRATIONS.parent / "run_migrations.py")]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 025: Created site_branding" in result.stdout
    with connection.bind_ctx([SiteBranding]):
        site_branding.update_branding({"site_name": "Preserved on retry"})
    connection.close()
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 025: Already applied" in result.stdout
    with connection.bind_ctx([SiteBranding]):
        assert site_branding.get_branding()["site_name"] == "Preserved on retry"
    connection.close()
