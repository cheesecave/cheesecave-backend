"""Keep main's LFS migration ahead of invitation schema upgrades, including old PR databases."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from kohakuhub.db import (
    LFSObjectHistory,
    Repository,
    RepositoryAccessEvent,
    RepositoryAccessGrant,
    RepositoryAccessRequest,
    RepositoryGuestSession,
    RepositoryInvite,
    RepositoryInviteRedemption,
    utcnow,
)
from scripts.run_migrations import discover_migrations, load_migration_module
from test.kohakuhub.support.factories import make_repo, make_user

CHAIN = (
    "043_lfs_history_window_index",
    "044_invite_repository",
    "045_repository_invites",
)
INVITE_MODELS = [
    RepositoryAccessEvent,
    RepositoryGuestSession,
    RepositoryInviteRedemption,
    RepositoryInvite,
    RepositoryAccessRequest,
    RepositoryAccessGrant,
]
NEW_INDEX = "lfsobjecthistory_repository_id_path_in_repo_created_at_id"
OLD_INDEX = "lfsobjecthistory_repository_id_path_in_repo"


@pytest.fixture
def chain(db_dual, monkeypatch):
    config = SimpleNamespace(app=SimpleNamespace(
        db_backend="sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres",
    ))
    discovered = dict(discover_migrations())
    modules = []
    for name in CHAIN:
        module = load_migration_module(name, discovered[name])
        assert module is not None
        monkeypatch.setattr(module, "db", db_dual)
        monkeypatch.setattr(module, "cfg", config)
        modules.append(module)
    return modules


def restore_old_history_index(database):
    database.execute_sql(f'DROP INDEX "{NEW_INDEX}"')
    database.execute_sql(
        f'CREATE INDEX "{OLD_INDEX}" ON "lfsobjecthistory" ("repository_id", "path_in_repo")'
    )


def seed_invitation_rows(repo):
    reader = make_user("reader")
    request = RepositoryAccessRequest.create(repository=repo, user=reader, status="approved")
    grant = RepositoryAccessGrant.create(repository=repo, user=reader, source="invite")
    invite = RepositoryInvite.create(
        repository=repo, token_hash="a" * 64, created_by=repo.owner, access_request=request,
        expires_at=utcnow() + timedelta(days=1),
    )
    RepositoryInviteRedemption.create(invite=invite, user=reader, grant=grant)
    RepositoryGuestSession.create(
        repository=repo, invite=invite, token_hash="b" * 64,
        expires_at=utcnow() + timedelta(hours=1),
    )
    RepositoryAccessEvent.create(
        repository=repo, actor=repo.owner, subject_user=reader, invite=invite,
        access_request=request, action="invite_issued",
    )


def snapshot():
    return {
        model._meta.table_name: list(model.select().order_by(model.id).dicts())
        for model in [Repository, LFSObjectHistory, *INVITE_MODELS]
    }


def test_migration_numbers_are_unique_and_invites_follow_main():
    migrations = discover_migrations()
    numbers = [int(name.split("_", 1)[0]) for name, _ in migrations]
    assert len(numbers) == len(set(numbers)), "Migration numbers must be unique"
    assert [name for name, _ in migrations if 43 <= int(name.split("_", 1)[0]) <= 45] == list(CHAIN)
    for name, path in migrations:
        if name in CHAIN:
            module = load_migration_module(name, path)
            assert module.MIGRATION_NUMBER == int(name.split("_", 1)[0])


def test_042_upgrades_in_order_and_a_repeat_preserves_all_rows(chain, db_dual):
    owner = make_user("owner")
    make_repo(owner, "public", private=False)
    repo = make_repo(owner, "private", private=True)
    LFSObjectHistory.create(
        repository=repo, path_in_repo="weights.bin", sha256="c" * 64, size=42, commit_id="old",
    )
    db_dual.drop_tables(INVITE_MODELS)
    db_dual.execute_sql('ALTER TABLE "repository" DROP COLUMN "invite_mode"')
    restore_old_history_index(db_dual)

    for module in chain:
        assert not module.is_applied(db_dual, module.cfg)
        assert module.run() is True
        assert module.is_applied(db_dual, module.cfg)

    assert [(row.name, row.visibility) for row in Repository.select().order_by(Repository.id)] == [
        ("public", "public"), ("private", "private"),
    ]
    seed_invitation_rows(repo)
    before = snapshot()
    for module in chain:
        assert module.run() is True
    assert snapshot() == before


def test_old_invite_schema_does_not_skip_main_index_or_lose_invitation_rows(chain, db_dual):
    repo = make_repo(make_user("owner"), "invite", private=True, invite_mode=True)
    seed_invitation_rows(repo)
    LFSObjectHistory.create(
        repository=repo, path_in_repo="weights.bin", sha256="c" * 64, size=42, commit_id="old",
    )
    restore_old_history_index(db_dual)
    before = snapshot()

    lfs, grants, invitations = chain
    assert not grants.is_applied(db_dual, grants.cfg)
    assert not invitations.is_applied(db_dual, invitations.cfg)
    assert not lfs.should_skip_due_to_future_migrations(lfs.MIGRATION_NUMBER, db_dual, lfs.cfg)
    for module in chain:
        assert module.run() is True
        assert module.is_applied(db_dual, module.cfg)
    indexes = {index.name for index in db_dual.get_indexes("lfsobjecthistory")}
    assert NEW_INDEX in indexes and OLD_INDEX not in indexes
    assert snapshot() == before

    for module in chain:
        assert module.run() is True
    assert snapshot() == before
