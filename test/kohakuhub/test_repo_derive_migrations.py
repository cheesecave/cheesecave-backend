"""Migration 036 adds ``info`` and ``version`` to ``repository_card`` (#63): what is derived from the
files, and which version of the rules derived it.

Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the table as 035 made it.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import Repository, RepositoryCard, RepositoryMetadata, User
from test.kohakuhub.support.db import put_the_old_file_key_back

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/036_repository_derived_info.py"
MIGRATION_029 = Path(__file__).resolve().parents[2] / "scripts/db_migrations/029_repository_discovery.py"
COLUMNS_035 = {"repository_id", "main_sha", "card", "derived"}


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("repository_derived_info_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([RepositoryCard], safe=True)
    db_dual.execute_sql(  # the table as 035 created it
        'CREATE TABLE "repository_card" ("repository_id" INTEGER NOT NULL PRIMARY KEY, "main_sha" VARCHAR(64), '
        '"card" TEXT NOT NULL, "derived" TEXT NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE)'
    )
    return module


def _repository(name="repo"):
    owner = User.create(username=f"{name}-owner", normalized_name=f"{name}-owner")
    return Repository.create(
        repo_type="model", namespace=owner.username, name=name, full_id=f"{owner.username}/{name}", private=False, owner=owner
    )


def _columns(db):
    return {c.name for c in db.get_columns("repository_card")}


def test_it_adds_the_two_columns(migration, db_dual):
    assert _columns(db_dual) == COLUMNS_035

    assert migration.run() is True

    assert _columns(db_dual) == COLUMNS_035 | {"info", "version"}


def test_rows_of_035_keep_their_data_and_count_as_derived_by_no_version(migration, db_dual):
    """A row written before this migration was derived without the files: version 0 is what makes
    the next index (and the next read) derive it again."""
    repo = _repository()
    mark = "?" if isinstance(db_dual, SqliteDatabase) else "%s"
    db_dual.execute_sql(
        'INSERT INTO "repository_card" ("repository_id", "main_sha", "card", "derived") '
        f"VALUES ({mark}, {mark}, {mark}, {mark})",
        (repo.id, "a" * 64, '{"license": "mit"}', '{"tags": ["x"]}'),
    )

    assert migration.run() is True

    row = RepositoryCard.get_by_id(repo.id)
    assert (row.card, row.derived, row.main_sha) == ('{"license": "mit"}', '{"tags": ["x"]}', "a" * 64)
    assert (row.info, row.version) == ("{}", 0)


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    repo = _repository()

    RepositoryCard.create(repository=repo, main_sha="b" * 64, card="{}", derived="{}", info='{"config": {}}', version=1)

    row = RepositoryCard.get_by_id(repo.id)
    assert (row.info, row.version) == ('{"config": {}}', 1)
    Repository.delete().where(Repository.id == repo.id).execute()  # the row goes with its repository
    assert RepositoryCard.select().count() == 0


def test_a_row_made_without_the_new_fields_gets_their_defaults(migration, db_dual):
    migration.run()
    repo = _repository()

    RepositoryCard.insert(repository=repo.id, card="{}", derived="{}").execute()

    row = RepositoryCard.get_by_id(repo.id)
    assert (row.info, row.version) == ("{}", 0)


def test_init_db_adds_nothing_more(migration, db_dual):
    migration.run()

    db_dual.create_tables([RepositoryCard], safe=True)  # what init_db() does at boot

    assert _columns(db_dual) == COLUMNS_035 | {"info", "version"}


def test_it_leaves_repository_metadata_exactly_as_029_checks_it(migration, db_dual):
    before = {c.name for c in db_dual.get_columns("repository_metadata")}

    migration.run()

    assert {c.name for c in db_dual.get_columns("repository_metadata")} == before
    spec = importlib.util.spec_from_file_location("discovery_029", MIGRATION_029)
    m029 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m029)
    m029._validate_schema(db_dual)  # no RuntimeError


def test_rows_of_the_index_are_untouched(migration, db_dual):
    repo = _repository()
    RepositoryMetadata.insert(repository=repo.id, state="ready", main_sha="a" * 64, metadata='{"tags": ["x"]}').execute()

    migration.run()

    row = RepositoryMetadata.get_by_id(repo.id)
    assert row.state == "ready" and row.metadata == '{"tags": ["x"]}'


def test_a_second_run_changes_nothing(migration, capsys):
    assert migration.run() is True
    capsys.readouterr()

    assert migration.run() is True

    assert "Already applied" in capsys.readouterr().out


def test_a_half_done_run_is_finished(migration, db_dual):
    """One column there (a run that stopped between the two on a database without transactional
    DDL) must not make the next run fail on a duplicate column."""
    db_dual.execute_sql('ALTER TABLE "repository_card" ADD COLUMN "info" TEXT NOT NULL DEFAULT \'{}\'')

    assert migration.run() is True

    assert _columns(db_dual) == COLUMNS_035 | {"info", "version"}


def test_it_is_applied_once_both_columns_exist(migration, db_dual):
    assert migration.is_applied(db_dual, migration.cfg) is False
    migration.run()
    assert migration.is_applied(db_dual, migration.cfg) is True


def test_it_is_not_applied_before_the_schema_it_follows(migration, db_dual):
    migration.run()
    put_the_old_file_key_back(db_dual)

    assert migration.is_applied(db_dual, migration.cfg) is False


def test_it_skips_when_a_later_migration_is_applied(migration, db_dual, monkeypatch, capsys):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *args: True)

    assert migration.run() is True

    assert "Skipped" in capsys.readouterr().out
    assert _columns(db_dual) == COLUMNS_035


def test_a_failing_migration_reports_failure(migration, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("disk full")

    # A deliberate database failure, so the one targeted mock
    monkeypatch.setattr(migration, "_migrate", broken)

    assert migration.run() is False

    assert "failed: disk full" in capsys.readouterr().out
