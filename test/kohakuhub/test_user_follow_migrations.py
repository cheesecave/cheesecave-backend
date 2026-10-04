"""Frozen follow schema is idempotent and rejects incompatible tables without repair."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from peewee import PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub.db import (
    Repository,
    RepositoryFacet,
    RepositoryMetadata,
    SiteAppearance,
    SiteBranding,
    SiteHomepage,
    User,
    UserFollow,
    db,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def migration(request, tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/030_user_follow.py"
    spec = importlib.util.spec_from_file_location("follow_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = (
        SqliteDatabase(str(tmp_path / "upgrade.db"), pragmas={"foreign_keys": 1})
        if request.param == "sqlite"
        else PostgresqlDatabase(db.database, **db.connect_params)
    )
    connection.connect()
    schema = "follow_upgrade_" + uuid4().hex
    if request.param == "postgres":
        connection.execute_sql(f'CREATE SCHEMA "{schema}"')
        connection.execute_sql(f'SET search_path TO "{schema}"')
    monkeypatch.setattr(module, "db", connection)
    monkeypatch.setattr(module.cfg.app, "db_backend", request.param)
    models = [
        User,
        UserFollow,
        Repository,
        RepositoryMetadata,
        RepositoryFacet,
        SiteBranding,
        SiteHomepage,
        SiteAppearance,
    ]
    try:
        with connection.bind_ctx(models):
            connection.create_tables([model for model in models if model != UserFollow])
            connection.execute_sql('CREATE TABLE "lfs_gc_state" ("id" INTEGER PRIMARY KEY)')
            connection.execute_sql('CREATE TABLE "repository_write" ("id" INTEGER PRIMARY KEY)')
            connection.execute_sql('CREATE TABLE "path_commit" ("id" INTEGER PRIMARY KEY)')
            follower = User.create(username="keep", normalized_name="keep")
            followed = User.create(username="target", normalized_name="target")
            SiteHomepage.create(id=1, title="Keep homepage")
            SiteAppearance.create(id=1, theme='{"primary_light":"#abcdef"}')
            yield module, connection, follower, followed
    finally:
        if request.param == "postgres":
            connection.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        connection.close()


def signature(database):
    return (
        [
            (column.name, column.data_type.lower(), column.null, column.primary_key)
            for column in database.get_columns("user_follow")
        ],
        {(tuple(item.columns), item.unique) for item in database.get_indexes("user_follow")},
        {
            (item.column, item.dest_table, item.dest_column)
            for item in database.get_foreign_keys("user_follow")
        },
    )


def test_model_parity_retry_preserves_settings_relationships_and_users(migration):
    module, connection, follower, followed = migration
    assert not module.is_applied(connection, module.cfg)
    assert module.run()
    assert module.is_applied(connection, module.cfg)
    migrated = signature(connection)
    UserFollow.create(follower=follower, followed=followed)
    assert module.run()
    assert UserFollow.select().count() == 1 and User.select().count() == 2
    assert SiteHomepage.get_by_id(1).title == "Keep homepage"
    assert SiteAppearance.get_by_id(1).theme == '{"primary_light":"#abcdef"}'
    connection.drop_tables([UserFollow])
    connection.create_tables([UserFollow])
    assert signature(connection) == migrated


@pytest.mark.parametrize("missing", ["site_appearance", "path_commit", "last_commits_recorded"])
def test_predecessor_cannot_be_hidden_by_fresh_table(migration, missing):
    module, connection, _, _ = migration
    assert module.run()
    if missing == "last_commits_recorded":
        connection.execute_sql('ALTER TABLE "repository" DROP COLUMN "last_commits_recorded"')
    else:
        connection.execute_sql(f'DROP TABLE "{missing}"')
    assert not module.is_applied(connection, module.cfg)


@pytest.mark.parametrize(
    "defect",
    ["no_cascade", "wrong_target", "no_check", "weak_check", "missing_column", "missing_unique"],
)
def test_incompatible_schema_fails_preserving_existing_data(migration, defect):
    module, connection, follower, followed = migration
    connection.execute_sql('ALTER TABLE "user" ADD COLUMN "audit_id" INTEGER')
    connection.execute_sql('CREATE UNIQUE INDEX "user_audit_id" ON "user"("audit_id")')
    connection.execute_sql('UPDATE "user" SET "audit_id"="id"')
    for statement in module.DDL:
        if statement.startswith("CREATE TABLE"):
            if defect == "no_cascade":
                statement = statement.replace("ON DELETE CASCADE", "ON DELETE NO ACTION")
            elif defect == "wrong_target":
                statement = statement.replace('"user"("id")', '"user"("audit_id")')
            elif defect == "no_check":
                statement = statement.replace(",\n    CHECK (follower_id != followed_id)", "")
            elif defect == "weak_check":
                statement = statement.replace(
                    "CHECK (follower_id != followed_id)",
                    "CHECK (follower_id != followed_id OR TRUE)",
                )
            elif defect == "missing_column":
                statement = statement.replace('"created_at" TIMESTAMP NOT NULL,', "")
        elif defect in ("missing_column", "missing_unique"):
            continue
        if isinstance(connection, SqliteDatabase):
            statement = statement.replace("SERIAL", "INTEGER").replace("TIMESTAMP", "DATETIME")
        connection.execute_sql(statement)
    if defect == "missing_column":
        connection.execute_sql(
            (
                'INSERT INTO "user_follow" (follower_id,followed_id) VALUES (%s,%s)'
                if isinstance(connection, PostgresqlDatabase)
                else 'INSERT INTO "user_follow" (follower_id,followed_id) VALUES (?,?)'
            ),
            (follower.id, followed.id),
        )
    else:
        UserFollow.create(follower=follower, followed=followed)
    before = signature(connection)
    assert not module.is_applied(connection, module.cfg)
    assert not module.run()
    assert signature(connection) == before
    assert connection.execute_sql('SELECT COUNT(*) FROM "user_follow"').fetchone()[0] == 1
