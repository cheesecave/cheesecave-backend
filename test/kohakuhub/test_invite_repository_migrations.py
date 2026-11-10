"""Migration 043 preserves old repositories and constrains new single-repository grants."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import (
    Repository, RepositoryAccessEvent, RepositoryAccessGrant, RepositoryAccessRequest,
    RepositoryGuestSession, RepositoryInvite, RepositoryInviteRedemption, User,
)
from test.kohakuhub.support.factories import make_repo, make_user

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/043_invite_repository.py"
TABLE = "repository_access_grant"
FUTURE_MODELS = [RepositoryAccessEvent, RepositoryGuestSession, RepositoryInviteRedemption, RepositoryInvite, RepositoryAccessRequest]


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("invite_repository_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    # Actual rows from the previous release: no invite column and no grant table.
    owner = make_user("old-owner")
    make_repo(owner, "public", private=False)
    make_repo(owner, "private", private=True)
    db_dual.drop_tables([*FUTURE_MODELS, RepositoryAccessGrant], safe=True)
    db_dual.execute_sql('ALTER TABLE "repository" DROP COLUMN "invite_mode"')
    return module


def test_the_old_rows_keep_their_privacy_and_start_without_invitation_mode(migration, db_dual):
    assert not migration.is_applied(db_dual, migration.cfg)

    assert migration.run() is True

    assert [(r.name, r.private, r.invite_mode, r.visibility) for r in Repository.select().order_by(Repository.id)] == [
        ("public", False, False, "public"),
        ("private", True, False, "private"),
    ]
    assert RepositoryAccessGrant.select().count() == 0
    assert migration.is_applied(db_dual, migration.cfg)


def test_the_schema_matches_the_models(migration, db_dual):
    assert migration.run() is True
    columns = {c.name: c for c in db_dual.get_columns(TABLE)}
    assert set(columns) == {
        "id", "repository_id", "user_id", "granted_by_id", "created_at", "expires_at", "revoked_at", "source",
    }
    assert all(columns[name].null for name in ("granted_by_id", "expires_at", "revoked_at"))
    assert all(not columns[name].null for name in ("repository_id", "user_id", "created_at", "source"))
    assert "repositoryaccessgrant_repository_id_user_id" in {i.name for i in db_dual.get_indexes(TABLE)}


@pytest.mark.parametrize("operation", ["insert", "update", "publish"])
def test_the_database_rejects_public_invitation_mode(migration, db_dual, operation):
    migration.run()
    private = Repository.get(Repository.name == "private")
    with pytest.raises(IntegrityError), db_dual.atomic():
        if operation == "insert":
            make_repo(private.owner, "invalid", private=False, invite_mode=True)
        elif operation == "update":
            Repository.update(invite_mode=True).where(Repository.name == "public").execute()
        else:
            Repository.update(invite_mode=True).where(Repository.id == private.id).execute()
            Repository.update(private=False).where(Repository.id == private.id).execute()


def test_a_private_repo_can_use_invite_mode(migration):
    migration.run()
    repo = Repository.get(Repository.name == "private")
    Repository.update(invite_mode=True).where(Repository.id == repo.id).execute()
    repo = Repository.get_by_id(repo.id)
    assert repo.private is True and repo.invite_mode is True and repo.visibility == "invite"


def test_grants_are_unique_per_account_and_repository_and_keep_separate_dates(migration, db_dual):
    migration.run()
    repo = Repository.get(Repository.name == "private")
    user = make_user("reader")
    expires = datetime(2030, 1, 1)
    grant = RepositoryAccessGrant.create(repository=repo, user=user, granted_by=repo.owner, expires_at=expires)
    saved = RepositoryAccessGrant.get_by_id(grant.id)
    assert saved.source == "manual" and saved.created_at.tzinfo is None
    assert saved.expires_at == expires and saved.revoked_at is None
    with pytest.raises(IntegrityError), db_dual.atomic():
        RepositoryAccessGrant.create(repository=repo, user=user)
    RepositoryAccessGrant.create(repository=Repository.get(Repository.name == "public"), user=user)
    assert RepositoryAccessGrant.select().count() == 2
    RepositoryAccessGrant.update(revoked_at=expires - timedelta(days=1)).where(RepositoryAccessGrant.id == saved.id).execute()
    assert RepositoryAccessGrant.get_by_id(saved.id).expires_at == expires


def test_grants_cascade_with_the_repo_or_grantee_but_release_the_grantor(migration):
    migration.run()
    repo = Repository.get(Repository.name == "private")
    reader, other, manager = make_user("reader"), make_user("other-reader"), make_user("manager")
    kept = RepositoryAccessGrant.create(repository=repo, user=reader, granted_by=manager)
    RepositoryAccessGrant.create(repository=repo, user=other, granted_by=manager)
    User.delete().where(User.id == manager.id).execute()
    assert RepositoryAccessGrant.get_by_id(kept.id).granted_by is None
    User.delete().where(User.id == other.id).execute()
    assert RepositoryAccessGrant.select().count() == 1
    Repository.delete().where(Repository.id == repo.id).execute()
    assert RepositoryAccessGrant.select().count() == 0


def test_a_second_run_preserves_rows_and_grants(migration):
    assert migration.run() is True
    repo = Repository.get(Repository.name == "private")
    grant = RepositoryAccessGrant.create(repository=repo, user=make_user("reader"))
    Repository.update(invite_mode=True).where(Repository.id == repo.id).execute()
    assert migration.run() is True
    assert RepositoryAccessGrant.get_by_id(grant.id).repository_id == repo.id
    assert Repository.get_by_id(repo.id).visibility == "invite"


def test_partial_application_is_repaired(migration, db_dual):
    migration.run()
    db_dual.drop_tables([*FUTURE_MODELS, RepositoryAccessGrant], safe=True)
    assert not migration.is_applied(db_dual, migration.cfg)
    assert migration.run() is True and migration.is_applied(db_dual, migration.cfg)


def test_the_schema_is_not_applied_while_a_predecessor_is_missing(migration, db_dual):
    migration.run()
    db_dual.execute_sql('DROP TABLE "collectionupvote"')
    assert not migration.is_applied(db_dual, migration.cfg)


def test_init_db_includes_the_grant_table(db_dual, monkeypatch):
    import kohakuhub.db as models

    db_dual.drop_tables([*FUTURE_MODELS, RepositoryAccessGrant], safe=True)
    monkeypatch.setattr(models, "db", db_dual)
    models.init_db()
    assert db_dual.table_exists(TABLE)
    repo = make_repo(make_user("owner"), "invite", private=True, invite_mode=True)
    assert repo.visibility == "invite"
    with pytest.raises(IntegrityError), db_dual.atomic():
        make_repo(repo.owner, "invalid", private=False, invite_mode=True)


def test_a_later_applied_migration_skips_this_work(migration, monkeypatch):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *args: True)
    assert migration.run() is True
    assert not migration.db.table_exists(TABLE)


def test_failure_is_reported_and_does_not_leave_a_partial_migration(migration, db_dual, monkeypatch):
    original = migration._migrate

    def fail_after_ddl():
        original()
        raise RuntimeError("deliberate database failure")

    monkeypatch.setattr(migration, "_migrate", fail_after_ddl)
    assert migration.run() is False
    assert "invite_mode" not in {c.name for c in db_dual.get_columns("repository")}
    assert not db_dual.table_exists(TABLE)
