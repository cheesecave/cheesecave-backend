"""Tests for migrations 017 (background_task), 018 (timeline, progress, logs),
019 (worker roster), 020 (LFS garbage collection candidates) and 021 (LFS
tombstones, recent objects and GC state).

They run as one chain: a migration skips itself once any later migration is
applied, so dropping only some of these tables would make the rest skip. Every test
starts from an empty ``db_fresh`` database and runs the migrations against it.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from peewee import SqliteDatabase
import pytest

from kohakuhub.db import (
    BackgroundTask,
    BackgroundTaskEvent,
    BackgroundTaskLog,
    BackgroundWorker,
    LfsGcCandidate,
    LfsGcState,
    LfsHeadRef,
    LfsObjectTombstone,
    LfsRecentObject,
    Repository,
    User,
)
from test.kohakuhub.support.db import MODELS as ALL_MODELS

MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "db_migrations"
PATH_COLUMNS = {
    ("file", "path_in_repo"),
    ("path_commit", "path"),
    ("stagingupload", "path_in_repo"),
    ("lfsobjecthistory", "path_in_repo"),
    ("lfs_head_ref", "path_in_repo"),
}
TABLES = (
    "background_task",
    "background_task_event",
    "background_task_log",
    "background_worker",
    "lfs_gc_candidate",
    "lfs_object_tombstone",
    "lfs_recent_object",
    "lfs_head_ref",
    "lfs_gc_state",
)
MODELS = [
    BackgroundTask,
    BackgroundTaskEvent,
    BackgroundTaskLog,
    BackgroundWorker,
    LfsGcCandidate,
    LfsObjectTombstone,
    LfsRecentObject,
    LfsHeadRef,
    LfsGcState,
]
OLD_ROW = (
    'INSERT INTO "background_task" (kind, queue, payload, status, priority, run_after,'
    " attempts, max_attempts, created_at) VALUES ('old.kind', 'default', '{{}}',"
    " 'failed', 0, {now}, 1, 5, {now})"
)


def _load(filename):
    spec = importlib.util.spec_from_file_location(filename[:-3], MIGRATIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_017():
    return _load("017_background_tasks.py")


def _load_018():
    return _load("018_background_task_observability.py")


def _load_019():
    return _load("019_background_workers.py")


def _load_020():
    return _load("020_lfs_gc_candidates.py")


def _load_021():
    return _load("021_lfs_gc_tombstones.py")


def _load_031():
    return _load("031_long_repo_paths.py")


def _chain():
    # 031 widens lfs_head_ref.path_in_repo, which 021 creates, to the TEXT init_db makes
    return _load_017(), _load_018(), _load_019(), _load_020(), _load_021(), _load_031()


def _sqlite_widened(schema):
    """SQLite cannot change a column's declared type and never enforced its
    length: 031 leaves the VARCHAR(255) of a repository path as it is there."""
    widened = {}
    for table, (columns, indexes, foreign_keys) in schema.items():
        columns = {
            name: ("text", *rest) if (table, name) in PATH_COLUMNS and kind == "varchar(255)" else (kind, *rest)
            for name, (kind, *rest) in columns.items()
        }
        widened[table] = (columns, indexes, foreign_keys)
    return widened


def _schema(database):
    schema = {}
    for table in TABLES:
        columns = {
            column.name: (column.data_type.lower(), column.null, column.primary_key)
            for column in database.get_columns(table)
        }
        indexes = {
            (index.name, tuple(index.columns), index.unique)
            for index in database.get_indexes(table)
            if not index.name.endswith("_pkey")
        }
        foreign_keys = {
            (fk.column, fk.dest_table, fk.dest_column) for fk in database.get_foreign_keys(table)
        }
        schema[table] = (columns, indexes, foreign_keys)
    return schema


def _empty(database):
    """Drop every model table, then create the repository base (user, repository).

    The LFS tables reference repository, and Postgres checks that at CREATE time. Later
    migrations' tables (user_follow and the rest) stay absent: 031 detects its state from them.
    """
    database.drop_tables(ALL_MODELS, safe=True)
    database.create_tables([User, Repository], safe=True)


def _reference(database):
    """The nine tables as the models create them, built in the same database (init_db's shape)."""
    database.drop_tables(MODELS, safe=True)
    database.create_tables(MODELS)
    return _schema(database)


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


def test_migrations_017_to_021_match_init_db_on_postgres(empty_db, monkeypatch):
    expected = _reference(empty_db)  # created from the models, as init_db() does
    _empty(empty_db)
    migrations = _chain()
    _bind(monkeypatch, empty_db, *migrations)
    for migration in migrations:
        assert migration.is_applied(empty_db, migration.cfg) is False
        assert migration.run() is True
    actual = _schema(empty_db)
    if isinstance(empty_db, SqliteDatabase):  # SQLite keeps the VARCHAR(255); see _sqlite_widened
        actual = _sqlite_widened(actual)
    assert actual == expected
    for migration in migrations:
        assert migration.run() is True  # re-running is a no-op


def test_migration_018_upgrades_existing_rows_on_postgres(empty_db, monkeypatch):
    m017, m018 = _load_017(), _load_018()
    _bind(monkeypatch, empty_db, m017, m018)
    assert m017.run() is True
    empty_db.execute_sql(OLD_ROW.format(now="CURRENT_TIMESTAMP"))
    assert m018.run() is True
    row = BackgroundTask.get(BackgroundTask.kind == "old.kind")
    assert row.cancel_requested is False
    assert (row.progress_done, row.checkpoint, row.stall_seconds) == (None, None, None)


def test_migrations_017_to_021_match_init_db_on_sqlite(empty_db, monkeypatch):
    m017, *later = _chain()
    migrated = _bind(monkeypatch, empty_db, m017, *later)

    assert m017.run() is True
    migrated.execute_sql(OLD_ROW.format(now="CURRENT_TIMESTAMP"))
    for migration in later:
        assert migration.run() is True
        assert migration.run() is True

    migrated_schema = _sqlite_widened(_schema(migrated))
    assert migrated.execute_sql('SELECT cancel_requested FROM "background_task"').fetchall() == [
        (0,)
    ]
    assert migrated_schema == _reference(migrated)


def test_migration_018_resumes_a_partially_added_column_set(empty_db, monkeypatch):
    m017, *later = _chain()
    migrated = _bind(monkeypatch, empty_db, m017, *later)
    assert m017.run() is True
    migrated.execute_sql('ALTER TABLE "background_task" ADD COLUMN "stall_seconds" INTEGER')

    for migration in later:
        assert migration.run() is True
    assert _sqlite_widened(_schema(migrated)) == _reference(migrated)


@pytest.mark.parametrize("loader", [_load_017, _load_018, _load_019, _load_020, _load_021])
def test_background_task_migrations_report_failure(empty_db, monkeypatch, loader):
    migration = loader()
    _bind(monkeypatch, empty_db, migration)

    def explode():
        raise RuntimeError("disk full")

    # run() picks the backend from cfg, which _bind sets to the database under test
    monkeypatch.setattr(migration, "migrate_sqlite", explode)
    monkeypatch.setattr(migration, "migrate_postgres", explode)

    assert migration.run() is False


@pytest.mark.parametrize("loader", [_load_018, _load_019, _load_020, _load_021])
def test_migrations_skip_when_a_later_migration_is_applied(empty_db, monkeypatch, loader):
    migration = loader()
    _bind(monkeypatch, empty_db, migration)
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *a: True)

    assert migration.run() is True
