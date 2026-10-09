"""Frozen discovery DDL matches Peewee on both databases and retains existing rows.

Each test starts from an emptied ``db_dual`` database (SQLite and PostgreSQL): 029 is checked against the
tables before it (built here as raw DDL), so no model table may already exist.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import SqliteDatabase
import pytest

from kohakuhub.db import (
    Repository,
    RepositoryFacet,
    RepositoryMetadata,
    SiteAppearance,
    SiteBranding,
    SiteHomepage,
    User,
)
from test.kohakuhub.support.db import MODELS as ALL_MODELS


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
def empty_db(db_dual):
    """A new database for this test, with no tables (see ``_empty``)."""
    _empty(db_dual)
    return db_dual


@pytest.fixture
def migration(empty_db, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/029_repository_discovery.py"
    spec = importlib.util.spec_from_file_location("discovery_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = _bind(monkeypatch, empty_db, module)
    connection.create_tables([User, Repository, SiteBranding, SiteHomepage, SiteAppearance])
    connection.execute_sql('CREATE TABLE "lfs_gc_state" ("id" INTEGER PRIMARY KEY)')
    connection.execute_sql('CREATE TABLE "repository_write" ("id" INTEGER PRIMARY KEY)')
    connection.execute_sql('CREATE TABLE "path_commit" ("id" INTEGER PRIMARY KEY)')
    user = User.create(username="keep", normalized_name="keep", email="keep@example.com")
    repo = Repository.create(
        repo_type="model", namespace="keep", name="repo", full_id="keep/repo", owner=user
    )
    yield module, connection, repo


def signature(database):
    return {
        table: (
            [
                (column.name, column.data_type.lower(), column.null, column.primary_key)
                for column in database.get_columns(table)
            ],
            {(tuple(index.columns), index.unique) for index in database.get_indexes(table)},
            [(fk.column, fk.dest_table, fk.dest_column) for fk in database.get_foreign_keys(table)],
        )
        for table in ("repository_metadata", "repository_facet")
    }


def test_migration_matches_model_and_preserves_rows_on_retry(migration):
    module, connection, repo = migration
    assert not module.is_applied(connection, module.cfg)
    assert module.run()
    assert module.is_applied(connection, module.cfg)
    migrated = signature(connection)
    RepositoryMetadata.create(repository=repo, state="ready", metadata='{"tags":["kept"]}')
    RepositoryFacet.create(repository=repo, key="tag", value="kept")
    assert module.run()
    assert RepositoryMetadata.get_by_id(repo.id).metadata == '{"tags":["kept"]}'
    assert RepositoryFacet.select().count() == 1
    assert Repository.get_by_id(repo.id).full_id == "keep/repo"
    connection.drop_tables([RepositoryFacet, RepositoryMetadata])
    connection.create_tables([RepositoryMetadata, RepositoryFacet])
    assert signature(connection) == migrated


def test_delete_cascades_discovery_metadata_and_facets(migration):
    module, _, repo = migration
    assert module.run()
    RepositoryMetadata.create(repository=repo)
    RepositoryFacet.create(repository=repo, key="tag", value="kept")
    repo.delete_instance()
    assert RepositoryMetadata.select().count() == 0 and RepositoryFacet.select().count() == 0


@pytest.mark.parametrize("missing", ["site_appearance", "path_commit", "last_commits_recorded"])
def test_startup_index_tables_cannot_hide_pending_older_migrations(migration, missing):
    module, connection, _ = migration
    assert module.run()
    if missing == "last_commits_recorded":
        connection.execute_sql('ALTER TABLE "repository" DROP COLUMN "last_commits_recorded"')
    else:
        connection.execute_sql(f'DROP TABLE "{missing}"')
    assert not module.is_applied(connection, module.cfg)


def test_incomplete_table_fails_without_losing_existing_data(migration):
    module, connection, _ = migration
    connection.execute_sql(
        'CREATE TABLE "repository_metadata" ("repository_id" INTEGER PRIMARY KEY, "metadata" TEXT)'
    )
    connection.execute_sql("INSERT INTO repository_metadata VALUES (1, 'Keep this')")
    assert not module.run()
    assert connection.execute_sql('SELECT * FROM "repository_metadata"').fetchall() == [
        (1, "Keep this")
    ]


@pytest.mark.parametrize("table", ["repository_metadata", "repository_facet"])
@pytest.mark.parametrize("defect", ["no_cascade", "wrong_target_column"])
def test_incompatible_foreign_key_is_rejected_without_repair(migration, table, defect):
    module, connection, repo = migration
    # A compatible integer candidate key lets both databases build the wrong FK.
    connection.execute_sql('ALTER TABLE "repository" ADD COLUMN "audit_id" INTEGER')
    connection.execute_sql('CREATE UNIQUE INDEX "audit_id_unique" ON "repository"("audit_id")')
    connection.execute_sql('UPDATE "repository" SET "audit_id" = "id"')
    for statement in module.DDL:
        if statement.startswith(f'CREATE TABLE IF NOT EXISTS "{table}"'):
            if defect == "no_cascade":
                statement = statement.replace("ON DELETE CASCADE", "ON DELETE NO ACTION")
            else:
                statement = statement.replace('"repository"("id")', '"repository"("audit_id")')
        if isinstance(connection, SqliteDatabase):
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        connection.execute_sql(statement)
    RepositoryMetadata.create(repository=repo, metadata='{"tags":["kept"]}')
    RepositoryFacet.create(repository=repo, key="tag", value="kept")
    before = signature(connection)
    assert not module.is_applied(connection, module.cfg)
    assert not module.run()
    assert signature(connection) == before
    assert RepositoryMetadata.get_by_id(repo.id).metadata == '{"tags":["kept"]}'
    assert RepositoryFacet.get().value == "kept"
