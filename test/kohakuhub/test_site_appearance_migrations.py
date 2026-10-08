"""Appearance upgrades are lossless and serialize concurrent nested edits on both databases.

Each test starts from an emptied ``db_fresh`` database: 028 is checked against the
tables before it (built here as raw DDL), so no model table may already exist.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

from peewee import SqliteDatabase
import pytest

from kohakuhub import db as db_module, site_appearance
from kohakuhub.db import SiteAppearance, SiteBranding, SiteHomepage
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
    return database


@pytest.fixture
def empty_db(db_fresh):
    """A new database for this test, with no tables (see ``_empty``)."""
    _empty(db_fresh)
    return db_fresh


@pytest.fixture
def appearance_migration(empty_db, monkeypatch):
    path = MIGRATIONS / "028_site_appearance.py"
    spec = importlib.util.spec_from_file_location("appearance_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _bind(monkeypatch, empty_db, module)
    yield module, empty_db


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
    with database.bind_ctx([SiteBranding, SiteHomepage]):
        database.create_tables([SiteBranding, SiteHomepage])
        SiteBranding.create(
            id=1, site_name="Preserved Hub", footer_description="Preserved introduction"
        )
        SiteHomepage.create(id=1, title="Preserved homepage", animation_enabled=False)


def schema_signature(database):
    return [
        (column.name, column.data_type.lower(), column.null, column.primary_key, column.default)
        for column in database.get_columns("site_appearance")
    ]


def test_upgrade_is_idempotent_preserves_settings_and_matches_fresh_model(appearance_migration):
    module, database = appearance_migration
    previous_schema(database)
    assert not module.is_applied(database, module.cfg)
    assert module.run()
    assert module.is_applied(database, module.cfg)
    signature = schema_signature(database)
    with database.bind_ctx([SiteAppearance, SiteBranding, SiteHomepage]):
        assert SiteAppearance.select().count() == 0
        assert SiteBranding.get_by_id(1).site_name == "Preserved Hub"
        assert SiteBranding.get_by_id(1).footer_description == "Preserved introduction"
        assert SiteHomepage.get_by_id(1).title == "Preserved homepage"
        assert SiteHomepage.get_by_id(1).animation_enabled is False
        site_appearance.update_appearance(
            {"footer": {"show_build_info": False}, "theme": {"primary_dark": "#abcdef"}}
        )
        assert module.run()
        assert site_appearance.get_appearance()["footer"]["show_build_info"] is False
        assert site_appearance.get_appearance()["theme"]["primary_dark"] == "#abcdef"
        SiteAppearance.drop_table()
        database.create_tables([SiteAppearance])
        assert schema_signature(database) == signature
    assert database.execute_sql('SELECT "id" FROM "repository"').fetchall() == [(7,)]


def test_startup_created_appearance_table_does_not_conceal_older_migrations(appearance_migration):
    module, database = appearance_migration
    with database.bind_ctx([SiteAppearance]):
        database.create_tables([SiteAppearance])
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
def test_appearance_cannot_hide_incomplete_previous_schema(appearance_migration, missing):
    module, database = appearance_migration
    previous_schema(database, missing)
    assert module.run()
    assert not module.is_applied(database, module.cfg)


def test_incompatible_existing_table_fails_without_losing_data(appearance_migration):
    module, database = appearance_migration
    previous_schema(database)
    database.execute_sql('CREATE TABLE "site_appearance" ("id" INTEGER PRIMARY KEY, "footer" TEXT)')
    database.execute_sql("INSERT INTO site_appearance VALUES (1, 'Keep this')")
    assert not module.is_applied(database, module.cfg)
    assert not module.run()
    assert database.execute_sql('SELECT * FROM "site_appearance"').fetchall() == [(1, "Keep this")]


def test_failed_ddl_is_reported(appearance_migration, monkeypatch):
    module, database = appearance_migration

    # Targeted mock on purpose: an outage of the DDL call itself is not reproducible with a
    # real SQL error that leaves the connection usable, so the failing call is injected here.
    def fail(*args, **kwargs):
        raise RuntimeError("DDL unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(database, "execute_sql", fail)
        assert not module.run()


def test_concurrent_first_writes_and_nested_edits_do_not_lose_fields(appearance_migration):
    module, database = appearance_migration
    assert module.run()
    patches = [
        {"footer": {"groups": [{"title": "Writer 1", "links": []}]}},
        {"footer": {"show_build_info": False}},
        {"theme": {"default_mode": "dark"}},
        {"theme": {"primary_dark": "#654321"}},
    ]
    barrier = Barrier(len(patches))

    def save(patch):
        with database.connection_context():
            barrier.wait(timeout=10)
            return site_appearance.update_appearance(patch)

    with database.bind_ctx([SiteAppearance]):
        with ThreadPoolExecutor(max_workers=len(patches)) as pool:
            list(pool.map(save, patches))
        result = site_appearance.get_appearance()
        assert result["footer"]["groups"] == [{"title": "Writer 1", "links": []}]
        assert result["footer"]["show_build_info"] is False
        assert result["theme"]["default_mode"] == "dark"
        assert result["theme"]["primary_dark"] == "#654321"
        assert SiteAppearance.select().count() == 1


def test_full_runner_upgrades_previous_release_and_preserves_overrides_on_retry(db_fresh):
    # The runner subprocess reaches the database through a SQLite URL, so it needs the file
    # path of db_fresh; a Postgres schema cannot be named that way.
    if not isinstance(db_fresh, SqliteDatabase):
        pytest.skip("SQLite file path for the subprocess")
    _empty(db_fresh)
    path = Path(db_fresh.database)
    models = [
        model
        for model in vars(db_module).values()
        if isinstance(model, type)
        and issubclass(model, db_module.BaseModel)
        and model not in (db_module.BaseModel, SiteAppearance)
    ]
    with db_fresh.bind_ctx(models):
        db_fresh.create_tables(models)
        SiteBranding.create(id=1, site_name="Keep name", footer_description="Keep description")
        SiteHomepage.create(id=1, title="Keep title")
    db_fresh.close()
    env = os.environ.copy()
    env.update(KOHAKU_HUB_DB_BACKEND="sqlite", KOHAKU_HUB_DATABASE_URL=f"sqlite:///{path}")
    command = [sys.executable, str(MIGRATIONS.parent / "run_migrations.py")]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 028: Created site_appearance" in result.stdout
    with db_fresh.bind_ctx([SiteAppearance]):
        site_appearance.update_appearance(
            {"footer": {"groups": []}, "theme": {"default_mode": "light"}}
        )
    db_fresh.close()
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 028: Already applied" in result.stdout
    with db_fresh.bind_ctx([SiteAppearance, SiteBranding, SiteHomepage]):
        assert site_appearance.get_appearance()["footer"]["groups"] == []
        assert site_appearance.get_appearance()["theme"]["default_mode"] == "light"
        assert SiteBranding.get_by_id(1).footer_description == "Keep description"
        assert SiteHomepage.get_by_id(1).title == "Keep title"
        assert json.loads(SiteAppearance.get_by_id(1).theme) == {"default_mode": "light"}
