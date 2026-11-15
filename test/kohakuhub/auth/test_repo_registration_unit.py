"""Account creation and repository exchange commit together without bypassing site policy."""

from datetime import timedelta
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
import pytest

from kohakuhub import repository_invites as invites
from kohakuhub.auth import routes
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.db import EmailVerification, Invitation, Repository, RepositoryAccessGrant, RepositoryInvite, RepositoryInviteRedemption, Session, User, UserOrganization, utcnow
from test.kohakuhub.support.factories import make_email_verification, make_invitation, make_repo, make_user


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
    assert result["repository_url"] == "/models/owner/restricted"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("invitation_only", [False, True])
async def test_guest_code_registers_one_account_and_then_rejects_another(registration, monkeypatch, invitation_only):
    repo, row, token = registration
    prior_sessions = [invites.redeem_guest(repo, token) for _ in range(2)]
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", invitation_only)
    site_invite = make_invitation("first-site-registration-code") if invitation_only else None
    result = await routes.register(request(token), invitation_token=site_invite.token if site_invite else None)
    assert result["success"]
    user = User.get(username="new-reader")
    assert check_repo_read_permission(repo, user)
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    persisted = RepositoryInvite.get_by_id(row.id)
    assert (persisted.redemption_count, persisted.guest_session_count) == (1, 2)
    for session, guest_secret in prior_sessions:
        assert invites.get_guest_session(repo, guest_secret).id == session.id
    with pytest.raises(invites.RepositoryInviteError):
        invites.redeem_guest(repo, token)
    if site_invite:
        assert Invitation.get_by_id(site_invite.id).usage_count == 1
    another_site_invite = make_invitation("second-site-registration-code") if invitation_only else None
    with pytest.raises(HTTPException) as denied:
        await routes.register(request(token, "another-reader"), invitation_token=another_site_invite.token if another_site_invite else None)
    assert denied.value.status_code == 400
    assert User.get_or_none(username="another-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    if another_site_invite:
        assert Invitation.get_by_id(another_site_invite.id).usage_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invitation_only", [False, True])
async def test_failed_registration_rolls_back_binding_but_preserves_prior_guest_sessions(registration, monkeypatch, invitation_only):
    repo, row, token = registration
    prior_sessions = [invites.redeem_guest(repo, token) for _ in range(2)]
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", invitation_only)
    site_invite = make_invitation("rollback-site-registration-code") if invitation_only else None
    redeem = invites.redeem_account
    def fail_after_binding(*args, **kwargs):
        # Real SQL has already consumed the code and created its grant; the injected
        # later failure proves registration rolls all of those writes back together.
        redeem(*args, **kwargs)
        raise RuntimeError("registration failed after repository binding")
    monkeypatch.setattr(invites, "redeem_account", fail_after_binding)
    with pytest.raises(RuntimeError, match="failed after repository binding"):
        await routes.register(request(token), invitation_token=site_invite.token if site_invite else None)
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0
    persisted = RepositoryInvite.get_by_id(row.id)
    assert (persisted.redemption_count, persisted.guest_session_count) == (0, 2)
    for session, guest_secret in prior_sessions:
        assert invites.get_guest_session(repo, guest_secret).id == session.id
    if site_invite:
        assert Invitation.get_by_id(site_invite.id).usage_count == 0
    monkeypatch.setattr(invites, "redeem_account", redeem)
    assert (await routes.register(request(token), invitation_token=site_invite.token if site_invite else None))["success"]


@pytest.mark.asyncio
async def test_prior_guest_sessions_do_not_bypass_invitation_only_registration_policy(registration, monkeypatch):
    repo, row, token = registration
    session, guest_secret = invites.redeem_guest(repo, token)
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    with pytest.raises(HTTPException) as denied:
        await routes.register(request(token))
    assert denied.value.status_code in (400, 403)
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0
    persisted = RepositoryInvite.get_by_id(row.id)
    assert (persisted.redemption_count, persisted.guest_session_count) == (0, 1)
    assert invites.get_guest_session(repo, guest_secret).id == session.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
async def test_repository_registration_returns_the_encoded_authoritative_repository(registration, kind):
    repo, row, token = registration
    Repository.update(repo_type=kind, namespace="group team", name="模型 #1").where(Repository.id == repo.id).execute()
    result = await routes.register(request(token))
    assert result["repository_url"] == f"/{kind}s/{quote('group team', safe='')}/{quote('模型 #1', safe='')}"
    user = User.get(username="new-reader")
    assert RepositoryAccessGrant.get(repository=repo, user=user).user_id == user.id
    assert token not in result["repository_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mail_sent", [False, True])
@pytest.mark.parametrize("invitation_only", [False, True])
async def test_required_verification_keeps_the_grant_and_repository_return(registration, monkeypatch, mail_sent, invitation_only):
    repo, row, token = registration
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", True)
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", invitation_only)
    site_invite = make_invitation("verified-registration-site-code") if invitation_only else None
    sent = []

    def mail(email, username, verification_token, return_to=None):
        sent.append((email, username, verification_token, return_to))
        return mail_sent

    monkeypatch.setattr(routes, "send_verification_email", mail)
    result = await routes.register(request(token), invitation_token=site_invite.token if site_invite else None)
    assert result["success"] and result["email_verified"] is False
    assert result["repository_url"] == "/models/owner/restricted"
    user = User.get(username="new-reader")
    assert user.email_verified is False
    assert RepositoryAccessGrant.get(repository=repo, user=user).user_id == user.id
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 1
    verification = EmailVerification.get(user=user)
    assert sent == [(str(user.email), user.username, verification.token, result["repository_url"])]
    assert token not in sent[0][2:]
    if site_invite:
        assert Invitation.get_by_id(site_invite.id).usage_count == 1
    response = await routes.verify_email(verification.token, Response(), return_to=result["repository_url"])
    assert response.headers["location"] == "/models/owner/restricted"
    assert User.get_by_id(user.id).email_verified is True
    assert EmailVerification.get_or_none(id=verification.id) is None
    assert Session.get(user=user).user_id == user.id


@pytest.mark.asyncio
async def test_registration_without_repository_code_keeps_the_original_response_and_mail_signature(registration, monkeypatch):
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", True)
    # Existing ordinary callers/mocks still need only the original three arguments.
    monkeypatch.setattr(routes, "send_verification_email", lambda email, username, token: True)
    result = await routes.register(request())
    assert result == {"success": True, "message": "User created. Please check your email to verify your account.", "email_verified": False}
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0


@pytest.mark.asyncio
async def test_unknown_repository_kind_cannot_create_a_return_target_or_leave_partial_registration(registration):
    repo, row, token = registration
    Repository.update(repo_type="unknown").where(Repository.id == repo.id).execute()
    with pytest.raises(HTTPException):
        await routes.register(request(token))
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0
    assert RepositoryInvite.get_by_id(row.id).redemption_count == 0


@pytest.mark.parametrize("target,expected", [
    ("/datasets/team/data", "/datasets/team/data"),
    ("//evil.test/models/a/b", "/http-reader"),
    ("/models/owner/%2F%2Fevil.test", "/http-reader"),
    ("/models/owner/%250d%250aLocation%253Aevil", "/http-reader"),
])
def test_verification_query_is_parsed_then_safely_redirected(registration, target, expected):
    user = make_user("http-reader", email_verified=False)
    verification = make_email_verification(user, "http-return-verification")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/auth/verify-email", params={"token": verification.token, "return_to": target})
    assert response.status_code == 302
    assert response.headers["location"] == expected
    assert "session_id=" in response.headers["set-cookie"]
    assert User.get_by_id(user.id).email_verified is True
    assert Session.get(user=user).user_id == user.id
