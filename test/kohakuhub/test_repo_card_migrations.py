"""Migration 035 builds the ``repository_card`` table (#62), the way the model declares it.

It is a table of its own, not columns of ``repository_metadata``: migration 029 checks that table's
columns exactly, so any column added to it would fail every later run of the migrations (and the
run of an older release after a rollback).

Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the schema 034 leaves: no card table.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase

from kohakuhub.db import Repository, RepositoryCard, RepositoryMetadata, User
from test.kohakuhub.support.db import MODELS as ALL_MODELS, put_the_old_file_key_back

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/035_repository_card.py"
MIGRATION_029 = Path(__file__).resolve().parents[2] / "scripts/db_migrations/029_repository_discovery.py"


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("repository_card_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([RepositoryCard], safe=True)  # the schema before 035
    return module


def _repository(name="repo"):
    owner = User.create(username=f"{name}-owner", normalized_name=f"{name}-owner")
    return Repository.create(
        repo_type="model", namespace=owner.username, name=name, full_id=f"{owner.username}/{name}", private=False, owner=owner
    )


def test_it_creates_the_table_the_model_declares(migration, db_dual):
    assert not db_dual.table_exists("repository_card")

    assert migration.run() is True

    assert {c.name for c in db_dual.get_columns("repository_card")} == {"repository_id", "main_sha", "card", "derived"}
    assert {c.name for c in db_dual.get_columns("repository_card") if c.primary_key} == {"repository_id"}


def test_the_table_does_what_the_model_needs(migration, db_dual):
    migration.run()
    repo = _repository()

    RepositoryCard.create(repository=repo, main_sha="a" * 64, card='{"license": "mit"}', derived='{"tags": []}')
    with pytest.raises(IntegrityError):  # one card per repository
        RepositoryCard.insert(repository=repo.id, main_sha="b", card="{}", derived="{}").execute()

    row = RepositoryCard.get_by_id(repo.id)
    assert row.card == '{"license": "mit"}' and row.main_sha == "a" * 64
    Repository.delete().where(Repository.id == repo.id).execute()  # the card goes with its repository
    assert RepositoryCard.select().count() == 0


def test_init_db_builds_no_second_table(migration, db_dual):
    migration.run()

    db_dual.create_tables([RepositoryCard], safe=True)  # what init_db() does at boot

    assert db_dual.table_exists("repository_card")


def test_it_leaves_repository_metadata_exactly_as_029_checks_it(migration, db_dual):
    """029 compares that table's columns exactly; a column added to it would fail every later run."""
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


def test_it_is_applied_once_the_table_exists(migration, db_dual):
    assert migration.is_applied(db_dual, migration.cfg) is False
    migration.run()
    assert migration.is_applied(db_dual, migration.cfg) is True


def test_it_is_not_applied_before_the_schema_it_follows(migration, db_dual):
    """The table alone must not count as applied on top of the schema before 033: every earlier
    migration would skip itself as superseded."""
    migration.run()
    put_the_old_file_key_back(db_dual)

    assert migration.is_applied(db_dual, migration.cfg) is False


def test_it_skips_when_a_later_migration_is_applied(migration, db_dual, monkeypatch, capsys):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *args: True)

    assert migration.run() is True

    assert "Skipped" in capsys.readouterr().out
    assert not db_dual.table_exists("repository_card")


def test_a_failing_migration_reports_failure(migration, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("disk full")

    # A deliberate database failure, so the one targeted mock
    monkeypatch.setattr(migration, "_migrate", broken)

    assert migration.run() is False

    assert "failed: disk full" in capsys.readouterr().out
