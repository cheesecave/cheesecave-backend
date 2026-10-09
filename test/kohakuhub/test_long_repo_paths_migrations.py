"""Migration 031 widens every repository-path column to TEXT.

2026-10-05 (cheesecave-backend#1): a 265-character path did not fit
VARCHAR(255) in ``file`` or ``path_commit``.

Each test runs on an emptied ``db_dual`` database (SQLite and PostgreSQL): 031 is checked against the
schema before it (built here as raw DDL), so no model table may already exist.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import IntegrityError, SqliteDatabase
import pytest

from test.kohakuhub.support.db import MODELS as ALL_MODELS

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
    path = Path(__file__).resolve().parents[2] / "scripts/db_migrations/031_long_repo_paths.py"
    spec = importlib.util.spec_from_file_location("long_paths_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = empty_db
    postgres = not isinstance(connection, SqliteDatabase)
    for statement in OLD_SCHEMA:
        connection.execute_sql(statement if postgres else statement.replace("SERIAL", "INTEGER"))
    connection.execute_sql(
        "INSERT INTO \"file\" (repository_id, path_in_repo) VALUES (1, 'kept.txt')"
    )
    _bind(monkeypatch, connection, module)
    module.real_predecessor_applied = module._predecessor_applied
    monkeypatch.setattr(module, "_predecessor_applied", lambda database, config: True)
    yield module, connection


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
    if isinstance(connection, SqliteDatabase):
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
