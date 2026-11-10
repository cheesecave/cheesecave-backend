"""Migration 044 declares the invitation lifecycle without storing exchange secrets."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import (
    Repository, RepositoryAccessEvent, RepositoryAccessGrant, RepositoryAccessRequest,
    RepositoryGuestSession, RepositoryInvite, RepositoryInviteRedemption, User, utcnow,
)
from test.kohakuhub.support.factories import make_repo, make_user

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/044_repository_invites.py"
MODELS = [RepositoryAccessEvent, RepositoryGuestSession, RepositoryInviteRedemption, RepositoryInvite, RepositoryAccessRequest]


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("repository_invites_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables(MODELS)
    return module


def rows():
    owner, user = make_user("owner"), make_user("reader")
    repo = make_repo(owner, "invite", private=True, invite_mode=True)
    request = RepositoryAccessRequest.create(repository=repo, user=user, status="approved")
    invite = RepositoryInvite.create(repository=repo, token_hash="a" * 64, created_by=owner,
                                     expires_at=utcnow() + timedelta(days=1), access_request=request)
    return owner, user, repo, request, invite


def test_schema_fields_indexes_and_second_run_preserves_existing_grants(migration, db_dual):
    owner, reader = make_user("old-owner"), make_user("old-reader")
    repo = make_repo(owner, "old-private", private=True)
    grant = RepositoryAccessGrant.create(repository=repo, user=reader)
    assert not migration.is_applied(db_dual, migration.cfg)
    assert migration.run() is True
    for model in MODELS:
        assert {c.name for c in db_dual.get_columns(model._meta.table_name)} == {
            field.column_name for field in model._meta.fields.values()
        }
        assert {i.name for i in db_dual.get_indexes(model._meta.table_name)} >= {
            index._name for index in model._meta.fields_to_index()
        }
    rows()
    assert migration.run() is True and migration.is_applied(db_dual, migration.cfg)
    assert RepositoryInvite.select().count() == 1
    assert RepositoryAccessGrant.get_by_id(grant.id).user_id == reader.id


@pytest.mark.parametrize("values", [
    {"max_redemptions": 0}, {"max_guest_sessions": -1}, {"guest_ttl_seconds": 0},
    {"guest_ttl_seconds": 604801}, {"redemption_count": -1}, {"redemption_count": 2},
    {"guest_session_count": -1}, {"guest_session_count": 2},
])
def test_database_rejects_invalid_invitation_counters_and_ttl(migration, db_dual, values):
    migration.run()
    _, _, repo, _, _ = rows()
    with pytest.raises(IntegrityError), db_dual.atomic():
        RepositoryInvite.create(repository=repo, token_hash="b" * 64, expires_at=utcnow() + timedelta(days=1), **values)


def test_uniqueness_and_request_shape_are_database_constraints(migration, db_dual):
    migration.run()
    _, user, repo, _, invite = rows()
    RepositoryInviteRedemption.create(invite=invite, user=user)
    for model, values in [
        (RepositoryAccessRequest, {"repository": repo, "user": user}),
        (RepositoryAccessRequest, {"repository": repo}),
        (RepositoryAccessRequest, {"repository": repo, "email": "reader@example.com", "status": "other"}),
        (RepositoryInviteRedemption, {"invite": invite, "user": user}),
        (RepositoryInvite, {"repository": repo, "token_hash": "a" * 64, "expires_at": utcnow()}),
    ]:
        with pytest.raises(IntegrityError), db_dual.atomic():
            model.create(**values)
    # Independent anonymous submissions have no account key to collide on.
    for _ in range(2):
        RepositoryAccessRequest.create(repository=repo, email="anonymous@example.com")
    assert RepositoryAccessRequest.select().count() == 3


def test_cascade_and_audit_references_are_preserved_as_declared(migration):
    migration.run()
    owner, user, repo, request, invite = rows()
    reviewer = make_user("reviewer")
    invite.created_by = reviewer
    invite.save()
    request.reviewed_by = reviewer
    request.save()
    grant = RepositoryAccessGrant.create(repository=repo, user=user)
    redemption = RepositoryInviteRedemption.create(invite=invite, user=user, grant=grant)
    event = RepositoryAccessEvent.create(repository=repo, actor=reviewer, subject_user=user, invite=invite,
                                         access_request=request, action="invite_issued")
    RepositoryGuestSession.create(repository=repo, invite=invite, token_hash="s" * 64,
                                  expires_at=utcnow() + timedelta(hours=1))
    User.delete().where(User.id == reviewer.id).execute()
    assert RepositoryInvite.get_by_id(invite.id).created_by is None
    assert RepositoryAccessRequest.get_by_id(request.id).reviewed_by is None
    assert RepositoryAccessEvent.get_by_id(event.id).actor is None
    RepositoryAccessGrant.delete().where(RepositoryAccessGrant.id == grant.id).execute()
    assert RepositoryInviteRedemption.get_by_id(redemption.id).grant is None
    RepositoryAccessRequest.delete().where(RepositoryAccessRequest.id == request.id).execute()
    assert RepositoryInvite.get_by_id(invite.id).access_request is None
    assert RepositoryAccessEvent.get_by_id(event.id).access_request is None
    RepositoryInvite.delete().where(RepositoryInvite.id == invite.id).execute()
    assert RepositoryInviteRedemption.select().count() == RepositoryGuestSession.select().count() == 0
    assert RepositoryAccessEvent.get_by_id(event.id).invite is None
    User.delete().where(User.id == user.id).execute()
    assert RepositoryAccessEvent.get_by_id(event.id).subject_user is None
    Repository.delete().where(Repository.id == repo.id).execute()
    assert RepositoryAccessEvent.select().count() == 0


def test_partial_migration_is_repaired_and_missing_predecessors_are_not_masked(migration, db_dual):
    migration.run()
    db_dual.drop_tables([RepositoryAccessEvent])
    assert not migration.is_applied(db_dual, migration.cfg)
    assert migration.run() is True and migration.is_applied(db_dual, migration.cfg)
    db_dual.execute_sql('DROP TABLE "collectionupvote"')
    assert not migration.is_applied(db_dual, migration.cfg)


def test_migration_failure_rolls_back_all_tables(migration, db_dual, monkeypatch):
    original = migration._migrate

    def fail():
        original()
        raise RuntimeError("deliberate migration failure")

    monkeypatch.setattr(migration, "_migrate", fail)
    assert migration.run() is False
    assert not any(db_dual.table_exists(model._meta.table_name) for model in MODELS)
