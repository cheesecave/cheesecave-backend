"""Appearance upgrades are lossless and serialize concurrent nested edits on both databases."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

from peewee import PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub import db as db_module, site_appearance
from kohakuhub.db import SiteAppearance, SiteBranding, SiteHomepage, db

MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "db_migrations"


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def appearance_migration(request, tmp_path, monkeypatch):
    path = MIGRATIONS / "028_site_appearance.py"
    spec = importlib.util.spec_from_file_location("appearance_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "appearance_upgrade_" + uuid4().hex
    if request.param == "sqlite":
        database = SqliteDatabase(str(tmp_path / "upgrade.db"), timeout=10)
    else:
        parameters = dict(db.connect_params)
        parameters["options"] = parameters.get("options", "") + f" -csearch_path={schema}"
        database = PostgresqlDatabase(db.database, **parameters)
    database.connect()
    if request.param == "postgres":
        database.execute_sql(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(module, "db", database)
    monkeypatch.setattr(module.cfg.app, "db_backend", request.param)
    try:
        yield module, database
    finally:
        if request.param == "postgres":
            database.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        database.close()


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


def test_full_runner_upgrades_previous_release_and_preserves_overrides_on_retry(tmp_path):
    path = tmp_path / "previous-release.db"
    database = SqliteDatabase(str(path), pragmas={"foreign_keys": 1})
    models = [
        model
        for model in vars(db_module).values()
        if isinstance(model, type)
        and issubclass(model, db_module.BaseModel)
        and model not in (db_module.BaseModel, SiteAppearance)
    ]
    with database.bind_ctx(models):
        database.create_tables(models)
        SiteBranding.create(id=1, site_name="Keep name", footer_description="Keep description")
        SiteHomepage.create(id=1, title="Keep title")
    database.close()
    env = os.environ.copy()
    env.update(KOHAKU_HUB_DB_BACKEND="sqlite", KOHAKU_HUB_DATABASE_URL=f"sqlite:///{path}")
    command = [sys.executable, str(MIGRATIONS.parent / "run_migrations.py")]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 028: Created site_appearance" in result.stdout
    with database.bind_ctx([SiteAppearance]):
        site_appearance.update_appearance(
            {"footer": {"groups": []}, "theme": {"default_mode": "light"}}
        )
    database.close()
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Migration 028: Already applied" in result.stdout
    with database.bind_ctx([SiteAppearance, SiteBranding, SiteHomepage]):
        assert site_appearance.get_appearance()["footer"]["groups"] == []
        assert site_appearance.get_appearance()["theme"]["default_mode"] == "light"
        assert SiteBranding.get_by_id(1).footer_description == "Keep description"
        assert SiteHomepage.get_by_id(1).title == "Keep title"
        assert json.loads(SiteAppearance.get_by_id(1).theme) == {"default_mode": "light"}
    database.close()
