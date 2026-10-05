"""Migration 031 widens every repository-path column to TEXT.

2026-10-05 (cheesecave-backend#1): a 265-character path did not fit
VARCHAR(255) in ``file`` or ``path_commit``.
"""

import importlib.util
from pathlib import Path
from uuid import uuid4

from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub.db import db

LONG = "_pathtest3/" + "x" * 250 + ".txt"
OLD_SCHEMA = [
    'CREATE TABLE "file" (id SERIAL PRIMARY KEY, repository_id INTEGER NOT NULL, path_in_repo VARCHAR(255) NOT NULL)',
    'CREATE UNIQUE INDEX "file_repository_id_path_in_repo" ON "file" (repository_id, path_in_repo)',
    'CREATE TABLE "path_commit" (id SERIAL PRIMARY KEY, repository_id INTEGER NOT NULL, branch VARCHAR(255) NOT NULL, path VARCHAR(255) NOT NULL)',
    'CREATE UNIQUE INDEX "pathcommit_repository_id_branch_path" ON "path_commit" (repository_id, branch, path)',
    'CREATE TABLE "stagingupload" (id SERIAL PRIMARY KEY, path_in_repo VARCHAR(255) NOT NULL)',
    'CREATE TABLE "lfsobjecthistory" (id SERIAL PRIMARY KEY, repository_id INTEGER NOT NULL, path_in_repo VARCHAR(255) NOT NULL)',
    'CREATE TABLE "lfs_head_ref" (id SERIAL PRIMARY KEY, repository_id INTEGER NOT NULL, branch VARCHAR(255) NOT NULL, path_in_repo VARCHAR(255) NOT NULL, sha256 VARCHAR(64) NOT NULL)',
]
COLUMNS = {
    ("file", "path_in_repo"),
    ("path_commit", "path"),
    ("stagingupload", "path_in_repo"),
    ("lfsobjecthistory", "path_in_repo"),
    ("lfs_head_ref", "path_in_repo"),
}


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def migration(request, tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/031_long_repo_paths.py"
    spec = importlib.util.spec_from_file_location("long_paths_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    postgres = request.param == "postgres"
    connection = (
        PostgresqlDatabase(db.database, **db.connect_params)
        if postgres
        else SqliteDatabase(str(tmp_path / "upgrade.db"))
    )
    connection.connect()
    schema = "long_paths_" + uuid4().hex
    if postgres:
        connection.execute_sql(f'CREATE SCHEMA "{schema}"')
        connection.execute_sql(f'SET search_path TO "{schema}"')
    for statement in OLD_SCHEMA:
        connection.execute_sql(statement if postgres else statement.replace("SERIAL", "INTEGER"))
    connection.execute_sql(
        "INSERT INTO \"file\" (repository_id, path_in_repo) VALUES (1, 'kept.txt')"
    )
    monkeypatch.setattr(module, "db", connection)
    monkeypatch.setattr(module.cfg.app, "db_backend", request.param)
    module.real_predecessor_applied = module._predecessor_applied
    monkeypatch.setattr(module, "_predecessor_applied", lambda database, config: True)
    module.postgres = postgres
    try:
        yield module, connection
    finally:
        if postgres:
            connection.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        connection.close()


def _types(connection):
    return {
        (table, column): data_type
        for table, column, data_type in connection.execute_sql(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
        if (table, column) in COLUMNS
    }


def _relfilenodes(connection):
    return dict(
        connection.execute_sql(
            "SELECT c.relname, c.relfilenode FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema()"
        ).fetchall()
    )


def test_the_migration_widens_every_path_column(migration):
    module, connection = migration
    if not module.postgres:
        # SQLite never enforced the length: nothing to change
        assert module.is_applied(connection, module.cfg)
        assert module.run() is True
        connection.execute_sql(
            'INSERT INTO "file" (repository_id, path_in_repo) VALUES (1, ?)', (LONG,)
        )
        return

    with pytest.raises(Exception, match="value too long for type character varying"):
        with connection.atomic():  # the production failure
            connection.execute_sql(
                "INSERT INTO \"path_commit\" (repository_id, branch, path) VALUES (1, 'main', %s)",
                (LONG,),
            )
    assert not module.is_applied(connection, module.cfg)
    before = _relfilenodes(connection)

    assert module.run() is True

    assert module.is_applied(connection, module.cfg)
    assert _types(connection) == {column: "text" for column in COLUMNS}
    assert _relfilenodes(connection) == before  # no rewrite: quick on a large table
    connection.execute_sql(
        "INSERT INTO \"path_commit\" (repository_id, branch, path) VALUES (1, 'main', %s)", (LONG,)
    )
    connection.execute_sql(
        'INSERT INTO "file" (repository_id, path_in_repo) VALUES (1, %s)', (LONG,)
    )
    assert [
        row[0] for row in connection.execute_sql('SELECT path_in_repo FROM "file" ORDER BY id')
    ] == ["kept.txt", LONG]
    with pytest.raises(IntegrityError):  # still one row per path
        with connection.atomic():
            connection.execute_sql(
                'INSERT INTO "file" (repository_id, path_in_repo) VALUES (1, %s)', (LONG,)
            )
    assert module.run() is True  # applied: nothing again
    assert _relfilenodes(connection) == before


def test_the_migration_waits_for_the_one_before(migration, monkeypatch):
    """Never applied on a schema missing what came before, so earlier
    migrations never skip themselves on its account."""
    module, connection = migration
    monkeypatch.setattr(module, "_predecessor_applied", lambda database, config: False)
    assert not module.is_applied(connection, module.cfg)


def test_the_predecessor_check_is_migration_030(migration, monkeypatch):
    module, connection = migration
    seen = []
    spec = importlib.util.spec_from_file_location
    monkeypatch.setattr(
        importlib.util,
        "spec_from_file_location",
        lambda name, path: seen.append(Path(path).name) or spec(name, path),
    )
    assert module.real_predecessor_applied(connection, module.cfg) is False  # this bare schema
    assert seen[0] == "030_user_follow.py"


def test_the_migration_reports_a_failure(migration, monkeypatch):
    module, connection = migration
    monkeypatch.setattr(
        module, "_narrow", lambda database: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert module.run() is False
