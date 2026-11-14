"""A live repository visitor can register without recovering the original invitation secret."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from kohakuhub import repository_invites as invites
from kohakuhub.api import repository_access as access
from kohakuhub.auth import routes
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.db import (
    EmailVerification, Invitation, Repository, RepositoryAccessGrant, RepositoryGuestSession,
    RepositoryInvite, RepositoryInviteRedemption, User, UserOrganization, utcnow,
)
from test.kohakuhub.support.factories import make_invitation, make_repo, make_user


@pytest.fixture
def visitor(db_fresh, monkeypatch):
    monkeypatch.setattr(routes, "db", db_fresh)
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", False)
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", False)
    owner = make_user()
    repo = make_repo(owner, "invited", private=True, invite_mode=True)
    row, token = invites.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=1))
    app = FastAPI()
    app.include_router(access.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: None
    with TestClient(app) as client:
        client.cookies.set("session_id", "unrelated-existing-login-cookie")
        result = client.post("/api/models/owner/invited/access/guest", json={"token": token})
        assert result.status_code == 200
        session = RepositoryGuestSession.get(invite=row)
        yield SimpleNamespace(client=client, repo=repo, row=row, token=token, session=session,
                              owner=owner, cookie_name=access.guest_cookie_name(repo),
                              base="/api/models/owner/invited/access")


def signup(visitor, username="new-reader", **changes):
    return dict(username=username, email=f"{username}@example.com", password="secret-pass-1",
                repo_guest_repository_id=visitor.repo.id) | changes


@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
@pytest.mark.parametrize("invite_mode", [True, False])
def test_live_guest_registration_grants_one_repository_and_returns_to_it(visitor, kind, invite_mode):
    Repository.update(repo_type=kind, invite_mode=invite_mode).where(Repository.id == visitor.repo.id).execute()
    state = visitor.client.get(f"/api/{kind}s/owner/invited/access")
    assert state.status_code == 200
    assert state.json()["registration_repository_id"] == visitor.repo.id
    assert state.headers["cache-control"] == "no-store"
    assert visitor.token not in state.text
    guest_secret = visitor.client.cookies.get(visitor.cookie_name)
    result = visitor.client.post("/api/auth/register", json=signup(visitor))
    assert result.status_code == 200, result.text
    assert result.json()["repository_url"] == f"/{kind}s/owner/invited"
    user = User.get(username="new-reader")
    assert check_repo_read_permission(visitor.repo, user)
    assert RepositoryAccessGrant.get(user=user).repository_id == visitor.repo.id
    assert RepositoryInviteRedemption.get(user=user).invite_id == visitor.row.id
    assert UserOrganization.select().count() == 0
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == 1
    assert visitor.client.cookies.get("session_id") == "unrelated-existing-login-cookie"
    assert visitor.client.cookies.get(visitor.cookie_name) == guest_secret
    assert invites.get_guest_session(visitor.repo, guest_secret).id == visitor.session.id
    state = visitor.client.get(f"/api/{kind}s/owner/invited/access").json()
    assert state["access"] == "guest" and "registration_repository_id" not in state
    again = visitor.client.post("/api/auth/register", json=signup(visitor, username="second-reader"))
    assert again.status_code == 400
    assert User.get_or_none(username="second-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1


@pytest.mark.parametrize("failure", ["code-expired", "code-revoked", "code-bound", "grant-expired",
                                      "guest-expired", "guest-revoked", "public-repo", "missing-repo",
                                      "missing-cookie", "wrong-cookie", "other-repo"])
def test_unavailable_guest_signup_rolls_back_account_and_redemption(visitor, failure):
    before_grants = 0
    match failure:
        case "code-expired":
            RepositoryInvite.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryInvite.id == visitor.row.id).execute()
        case "code-revoked":
            invites.revoke_invite(visitor.repo, visitor.row.id, visitor.owner)
        case "code-bound":
            invites.redeem_account(visitor.repo, visitor.token, make_user("already-bound"))
            before_grants = 1
        case "grant-expired":
            RepositoryInvite.update(grant_expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryInvite.id == visitor.row.id).execute()
        case "guest-expired":
            RepositoryGuestSession.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryGuestSession.id == visitor.session.id).execute()
        case "guest-revoked":
            invites.revoke_guest_session(visitor.repo, visitor.session.id, visitor.owner)
        case "public-repo":
            Repository.update(private=False, invite_mode=False).where(Repository.id == visitor.repo.id).execute()
        case "missing-repo":
            visitor.repo.id += 100
        case "missing-cookie":
            visitor.client.cookies.clear()
        case "wrong-cookie":
            visitor.client.cookies.clear()
            visitor.client.cookies.set(visitor.cookie_name, "guest_wrong_fixture")
        case "other-repo":
            secret = visitor.client.cookies.get(visitor.cookie_name)
            other = make_repo(visitor.owner, "other", private=True, invite_mode=True)
            visitor.client.cookies.set(access.guest_cookie_name(other), secret)
            visitor.repo = other
    result = visitor.client.post("/api/auth/register", json=signup(visitor))
    assert result.status_code == 400, result.text
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == before_grants
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == before_grants


@pytest.mark.parametrize("failure", ["code-expired", "code-bound", "grant-expired"])
def test_preview_outlives_exchange_but_does_not_offer_registration(visitor, failure):
    if failure == "code-bound":
        invites.redeem_account(visitor.repo, visitor.token, make_user("already-bound"))
    else:
        field = "expires_at" if failure == "code-expired" else "grant_expires_at"
        RepositoryInvite.update(**{field: utcnow() - timedelta(seconds=1)}).where(RepositoryInvite.id == visitor.row.id).execute()
    result = visitor.client.get(visitor.base)
    assert result.status_code == 200
    assert result.json()["access"] == "guest"
    assert "registration_repository_id" not in result.json()


@pytest.mark.parametrize("site_code", [False, True])
def test_visitor_registration_respects_separate_site_invitation(visitor, monkeypatch, site_code):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    invitation = make_invitation("site-visitor-registration")
    result = visitor.client.post("/api/auth/register", json=signup(visitor),
                                 params={"invitation_token": invitation.token} if site_code else {})
    assert result.status_code == (200 if site_code else 403)
    assert (User.get_or_none(username="new-reader") is not None) is site_code
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == int(site_code)
    assert Invitation.get_by_id(invitation.id).usage_count == int(site_code)


def test_guest_failure_rolls_back_site_usage_account_and_grant(visitor, monkeypatch):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    invitation = make_invitation("site-visitor-rollback")
    grant = invites._grant

    def fail_after_grant(*args, **kwargs):
        # A deliberate post-write failure exercises the real enclosing SQL transaction.
        grant(*args, **kwargs)
        raise RuntimeError("failure after repository grant")

    monkeypatch.setattr(invites, "_grant", fail_after_grant)
    with pytest.raises(RuntimeError, match="after repository grant"):
        visitor.client.post("/api/auth/register", json=signup(visitor), params={"invitation_token": invitation.token})
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == 0
    assert Invitation.get_by_id(invitation.id).usage_count == 0


def test_guest_registration_and_code_cannot_select_conflicting_targets(visitor):
    result = visitor.client.post("/api/auth/register", json=signup(visitor, repo_invitation_token=visitor.token))
    assert result.status_code == 400
    assert User.get_or_none(username="new-reader") is None


@pytest.mark.asyncio
async def test_direct_registration_cannot_use_a_repository_id_without_the_http_cookie(visitor):
    with pytest.raises(HTTPException) as denied:
        await routes.register(routes.RegisterRequest(**signup(visitor)))
    assert denied.value.status_code == 400
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == 0


@pytest.mark.parametrize("sent", [False, True])
def test_guest_signup_keeps_required_email_verification_and_return(visitor, monkeypatch, sent):
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", True)
    messages = []

    def send_email(email, username, token, return_to=None):
        messages.append((email, username, token, return_to))
        return sent

    monkeypatch.setattr(routes, "send_verification_email", send_email)
    result = visitor.client.post("/api/auth/register", json=signup(visitor))
    assert result.status_code == 200
    assert result.json()["email_verified"] is False
    assert result.json()["repository_url"] == "/models/owner/invited"
    user = User.get(username="new-reader")
    assert user.email_verified is False
    assert RepositoryAccessGrant.get(user=user).repository_id == visitor.repo.id
    verification = EmailVerification.get(user=user)
    assert messages == [(user.email, user.username, verification.token, "/models/owner/invited")]
    assert visitor.token not in str(messages)


@pytest.mark.parametrize("value", [0, -1, "1", True, 2**63])
def test_guest_repository_selector_requires_a_positive_integer(visitor, value):
    result = visitor.client.post("/api/auth/register", json=signup(visitor, repo_guest_repository_id=value))
    assert result.status_code == 422
    assert User.get_or_none(username="new-reader") is None


def test_concurrent_guest_registration_consumes_one_code_without_an_orphan(visitor):
    ready = Barrier(2)
    first_secret = visitor.client.cookies.get(visitor.cookie_name)
    assert visitor.client.post(visitor.base + "/guest", json={"token": visitor.token}).status_code == 200
    secrets = [first_secret, visitor.client.cookies.get(visitor.cookie_name)]
    assert secrets[0] != secrets[1]

    def submit(index):
        with TestClient(visitor.client.app) as client:
            client.cookies.set(visitor.cookie_name, secrets[index])
            ready.wait(timeout=10)
            return client.post("/api/auth/register", json=signup(visitor, username=f"concurrent-reader-{index}")).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, [0, 1])) == [200, 400]
    assert User.select().where(User.username.startswith("concurrent-reader-")).count() == 1
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    assert RepositoryInvite.get_by_id(visitor.row.id).redemption_count == 1
    assert all(invites.get_guest_session(visitor.repo, secret) for secret in secrets)
