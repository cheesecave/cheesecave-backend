"""Account creation and repository exchange commit together without bypassing site policy."""

from datetime import timedelta
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi import HTTPException
import pytest

from kohakuhub import repository_invites as invites
from kohakuhub.auth import routes
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.db import Invitation, RepositoryAccessGrant, RepositoryInvite, RepositoryInviteRedemption, User, UserOrganization, utcnow
from test.kohakuhub.support.factories import make_invitation, make_repo, make_user


@pytest.fixture
def registration(db_fresh, monkeypatch):
    monkeypatch.setattr(routes, "db", db_fresh)
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", False)
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", False)
    owner = make_user()
    repo = make_repo(owner, "restricted", private=True)
    row, token = invites.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=1))
    return repo, row, token


def request(token=None, username="new-reader"):
    return routes.RegisterRequest(username=username, email=f"{username}@example.com", password="secret-pass-1", repo_invitation_token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_query", [False, True])
async def test_open_registration_always_processes_repository_code(registration, in_query):
    repo, row, token = registration
    result = await routes.register(request(None if in_query else token), invitation_token=token if in_query else None)
    assert result["success"]
    user = User.get(username="new-reader")
    assert check_repo_read_permission(repo, user)
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    assert RepositoryInviteRedemption.get(invite=row, user=user).grant.user_id == user.id
    assert UserOrganization.select().where(UserOrganization.user == user).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", ["unknown", "expired", "revoked", "exhausted"])
async def test_failed_exchange_rolls_back_account_and_accounting(registration, bad_state):
    repo, row, token = registration
    if bad_state == "unknown":
        token = "repo_unknown"
    else:
        updates = {"expired": {"expires_at": utcnow() - timedelta(seconds=1)}, "revoked": {"revoked_at": utcnow()}, "exhausted": {"redemption_count": 1}}
        RepositoryInvite.update(**updates[bad_state]).where(RepositoryInvite.id == row.id).execute()
    with pytest.raises(HTTPException):
        await routes.register(request(token))
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == 0
    assert RepositoryInviteRedemption.select().count() == 0


@pytest.mark.asyncio
async def test_repo_code_never_satisfies_invitation_only_site_policy(registration, monkeypatch):
    repo, row, token = registration
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    for query_token in (None, token):
        with pytest.raises(HTTPException) as denied:
            await routes.register(request(token), invitation_token=query_token)
        assert denied.value.status_code in (400, 403)
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 0
    site_invite = make_invitation("site-code")
    await routes.register(request(token), invitation_token=site_invite.token)
    user = User.get(username="new-reader")
    assert check_repo_read_permission(repo, user)
    assert Invitation.get_by_id(site_invite.id).usage_count == 1


@pytest.mark.asyncio
async def test_repo_failure_also_rolls_back_site_invitation_usage(registration, monkeypatch):
    _, row, token = registration
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    site_invite = make_invitation("site-code")
    RepositoryInvite.update(revoked_at=utcnow()).where(RepositoryInvite.id == row.id).execute()
    with pytest.raises(HTTPException):
        await routes.register(request(token), invitation_token=site_invite.token)
    assert User.get_or_none(username="new-reader") is None
    assert Invitation.get_by_id(site_invite.id).usage_count == 0


@pytest.mark.parametrize("same_username", [False, True])
def test_concurrent_registration_consumes_one_code_and_leaves_no_orphan_account(registration, same_username):
    repo, row, token = registration
    ready = Barrier(2)
    def register_one(index):
        ready.wait(timeout=10)
        try:
            asyncio.run(routes.register(request(token, "concurrent" if same_username else f"concurrent-{index}")))
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register_one, [0, 1]))
    assert sorted(results) == [200, 400]
    assert User.select().where(User.username.startswith("concurrent")).count() == 1
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    assert RepositoryInviteRedemption.select().count() == 1
    assert RepositoryAccessGrant.select().count() == 1


@pytest.mark.asyncio
async def test_repository_binding_route_name_cannot_be_registered(registration):
    with pytest.raises(HTTPException) as denied:
        await routes.register(request(username="repo-access"))
    assert denied.value.status_code == 400
    assert "reserved" in denied.value.detail
