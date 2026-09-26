"""Tests for migrations 017 (background_task), 018 (timeline, progress, logs),
019 (worker roster) and 020 (LFS garbage collection candidates).

They run as one chain: a migration skips itself once any later migration is
applied, so dropping only some of these tables would make the rest skip.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import (
    BackgroundTask,
    BackgroundTaskEvent,
    BackgroundTaskLog,
    BackgroundWorker,
    LfsGcCandidate,
    db,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "db_migrations"
TABLES = (
    "background_task",
    "background_task_event",
    "background_task_log",
    "background_worker",
    "lfs_gc_candidate",
)
MODELS = [BackgroundTask, BackgroundTaskEvent, BackgroundTaskLog, BackgroundWorker, LfsGcCandidate]
DROP_ALL = (
    'DROP TABLE IF EXISTS "lfs_gc_candidate", "background_worker", "background_task_log", '
    '"background_task_event", "background_task"'
)
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


def _chain():
    return _load_017(), _load_018(), _load_019(), _load_020()


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


def _sqlite(monkeypatch, *modules, path):
    database = SqliteDatabase(str(path), pragmas={"foreign_keys": 1})
    for module in modules:
        monkeypatch.setattr(module, "db", database)
        monkeypatch.setattr(
            module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend="sqlite"))
        )
    return database


def _sqlite_reference(path):
    reference = SqliteDatabase(str(path))
    with reference.bind_ctx(MODELS):
        reference.create_tables(MODELS)
    return _schema(reference)


def test_migrations_017_to_020_match_init_db_on_postgres(prepared_backend_test_state):
    expected = _schema(db)  # created by init_db()
    db.execute_sql(DROP_ALL)
    try:
        migrations = _chain()
        for migration in migrations:
            assert migration.is_applied(db, migration.cfg) is False
            assert migration.run() is True
        assert _schema(db) == expected
        for migration in migrations:
            assert migration.run() is True  # re-running is a no-op
    finally:
        db.create_tables(MODELS, safe=True)


def test_migration_018_upgrades_existing_rows_on_postgres(prepared_backend_test_state):
    db.execute_sql(DROP_ALL)
    try:
        assert _load_017().run() is True
        db.execute_sql(OLD_ROW.format(now="now()"))
        assert _load_018().run() is True
        row = BackgroundTask.get(BackgroundTask.kind == "old.kind")
        assert row.cancel_requested is False
        assert (row.progress_done, row.checkpoint, row.stall_seconds) == (None, None, None)
    finally:
        db.execute_sql(DROP_ALL)
        db.create_tables(MODELS, safe=True)


def test_migrations_017_to_020_match_init_db_on_sqlite(tmp_path, monkeypatch):
    m017, *later = _chain()
    migrated = _sqlite(monkeypatch, m017, *later, path=tmp_path / "migrated.db")

    assert m017.run() is True
    migrated.execute_sql(OLD_ROW.format(now="'2026-01-01'"))
    for migration in later:
        assert migration.run() is True
        assert migration.run() is True

    assert _schema(migrated) == _sqlite_reference(tmp_path / "reference.db")
    assert migrated.execute_sql('SELECT cancel_requested FROM "background_task"').fetchall() == [
        (0,)
    ]


def test_migration_018_resumes_a_partially_added_column_set(tmp_path, monkeypatch):
    m017, *later = _chain()
    migrated = _sqlite(monkeypatch, m017, *later, path=tmp_path / "partial.db")
    assert m017.run() is True
    migrated.execute_sql('ALTER TABLE "background_task" ADD COLUMN "stall_seconds" INTEGER')

    for migration in later:
        assert migration.run() is True
    assert _schema(migrated) == _sqlite_reference(tmp_path / "reference.db")


@pytest.mark.parametrize("loader", [_load_017, _load_018, _load_019, _load_020])
def test_background_task_migrations_report_failure(tmp_path, monkeypatch, loader):
    migration = loader()
    _sqlite(monkeypatch, migration, path=tmp_path / "broken.db")

    def explode():
        raise RuntimeError("disk full")

    monkeypatch.setattr(migration, "migrate_sqlite", explode)

    assert migration.run() is False


@pytest.mark.parametrize("loader", [_load_018, _load_019, _load_020])
def test_migrations_skip_when_a_later_migration_is_applied(tmp_path, monkeypatch, loader):
    migration = loader()
    _sqlite(monkeypatch, migration, path=tmp_path / "later.db")
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *a: True)

    assert migration.run() is True
