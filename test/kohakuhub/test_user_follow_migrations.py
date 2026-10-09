"""Frozen follow schema is idempotent and rejects incompatible tables without repair.

Each test starts from an emptied ``db_dual`` database (SQLite and PostgreSQL): 030 checks its predecessors, so
no model table may already exist when the migration history starts.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub.db import (
    LfsGcState,
    PathCommit,
    Repository,
    RepositoryFacet,
    RepositoryMetadata,
    RepositoryWrite,
    SiteAppearance,
    SiteBranding,
    SiteHomepage,
    User,
    UserFollow,
)
from test.kohakuhub.support.db import MODELS as ALL_MODELS

# The predecessor tables. PathCommit, LfsGcState and RepositoryWrite are the real models
# here (the file used to create one-column stand-ins for them).
REFERENCE_MODELS = [
    User,
    UserFollow,
    Repository,
    RepositoryMetadata,
    RepositoryFacet,
    SiteBranding,
    SiteHomepage,
    SiteAppearance,
    PathCommit,
    LfsGcState,
    RepositoryWrite,
]


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
def migration(db_dual, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/030_user_follow.py"
    spec = importlib.util.spec_from_file_location("follow_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _empty(db_dual)
    _bind(monkeypatch, db_dual, module)
    db_dual.create_tables([model for model in REFERENCE_MODELS if model != UserFollow])
    follower = User.create(username="keep", normalized_name="keep")
    followed = User.create(username="target", normalized_name="target")
    SiteHomepage.create(id=1, title="Keep homepage")
    SiteAppearance.create(id=1, theme='{"primary_light":"#abcdef"}')
    return module, db_dual, follower, followed


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
