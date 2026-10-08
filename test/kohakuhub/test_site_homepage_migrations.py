"""Homepage migration preserves overrides and never hides pending older upgrades.

Each test starts from an emptied ``db_fresh`` database: 027 checks the tables before it
(built here as raw DDL), so no model table may already exist.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import SqliteDatabase
import pytest

from kohakuhub import site_homepage
from kohakuhub.db import SiteBranding, SiteHomepage
from test.kohakuhub.support.db import MODELS as ALL_MODELS

MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "db_migrations"


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


@pytest.fixture
def empty_db(db_fresh):
    """A new database for this test, with no tables (see ``_empty``)."""
    _empty(db_fresh)
    return db_fresh


@pytest.fixture
def homepage_migration(empty_db, monkeypatch):
    path = MIGRATIONS / "027_site_homepage.py"
    spec = importlib.util.spec_from_file_location("homepage_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _bind(monkeypatch, empty_db, module)
    return module, empty_db


def previous_schema(database, missing=None):
    database.execute_sql('CREATE TABLE "lfs_gc_state" ("id" INTEGER PRIMARY KEY)')
    database.execute_sql('CREATE TABLE "repository_write" ("id" INTEGER PRIMARY KEY)')
    columns = [
        f'"{name}" TEXT'
        for name in (
            "main_counted_commit",
            "operation",
            "operation_until",
            "history_root",
            "last_commits_recorded",
        )
        if name != missing
    ]
    database.execute_sql(
        'CREATE TABLE "repository" ("id" INTEGER PRIMARY KEY, ' + ", ".join(columns) + ")"
    )
    database.execute_sql('INSERT INTO "repository" ("id") VALUES (7)')
    database.execute_sql('CREATE TABLE "path_commit" ("id" INTEGER PRIMARY KEY)')
    with database.bind_ctx([SiteBranding]):
        database.create_tables([SiteBranding])
        SiteBranding.create(id=1, site_name="Preserved Hub")


def schema_signature(database):
    return [
        (column.name, column.data_type.lower(), column.null, column.primary_key, column.default)
        for column in database.get_columns("site_homepage")
    ]


def test_migration_matches_fresh_model_and_preserves_data(homepage_migration):
    module, database = homepage_migration
    previous_schema(database)
    assert not module.is_applied(database, module.cfg)
    assert module.run()
    assert module.is_applied(database, module.cfg)
    signature = schema_signature(database)
    assert database.execute_sql('SELECT * FROM "site_branding"').fetchone()[1] == "Preserved Hub"
    assert database.execute_sql('SELECT "id" FROM "repository"').fetchall() == [(7,)]
    with database.bind_ctx([SiteHomepage]):
        assert SiteHomepage.select().count() == 0
        site_homepage.update_homepage({"title": "Kept after retry", "animation_enabled": False})
        assert module.run()
        assert site_homepage.get_homepage()["title"] == "Kept after retry"
        assert site_homepage.get_homepage()["animation_enabled"] is False
        SiteHomepage.drop_table()
        database.create_tables([SiteHomepage])
        assert schema_signature(database) == signature


def test_startup_created_homepage_alone_does_not_supersede_old_migrations(homepage_migration):
    module, database = homepage_migration
    with database.bind_ctx([SiteHomepage]):
        database.create_tables([SiteHomepage])
    assert not module.is_applied(database, module.cfg)


@pytest.mark.parametrize(
    "missing",
    [
        "main_counted_commit",
        "operation",
        "operation_until",
        "history_root",
        "last_commits_recorded",
    ],
)
def test_homepage_cannot_hide_incomplete_preceding_schema(homepage_migration, missing):
    module, database = homepage_migration
    previous_schema(database, missing)
    with database.bind_ctx([SiteHomepage]):
        database.create_tables([SiteHomepage])
    assert not module.is_applied(database, module.cfg)


def test_existing_incompatible_table_is_preserved_and_fails(homepage_migration):
    module, database = homepage_migration
    previous_schema(database)
    database.execute_sql('CREATE TABLE "site_homepage" ("id" INTEGER PRIMARY KEY, "title" TEXT)')
    database.execute_sql("INSERT INTO site_homepage VALUES (1, 'Do not discard')")
    assert not module.is_applied(database, module.cfg)
    assert not module.run()
    assert database.execute_sql('SELECT * FROM "site_homepage"').fetchall() == [
        (1, "Do not discard")
    ]


def test_ddl_failure_is_reported(homepage_migration, monkeypatch):
    module, database = homepage_migration

    # Targeted mock on purpose: an outage of the DDL call itself is not reproducible with a
    # real SQL error that leaves the connection usable, so the failing call is injected here.
    def fail(*args, **kwargs):
        raise RuntimeError("DDL unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(database, "execute_sql", fail)
        assert not module.run()


def test_homepage_cannot_hide_pending_path_commit_migration(homepage_migration):
    module, database = homepage_migration
    previous_schema(database)
    database.execute_sql('DROP TABLE "path_commit"')
    with database.bind_ctx([SiteHomepage]):
        database.create_tables([SiteHomepage])
    assert not module.is_applied(database, module.cfg)
