"""The registration field accepts either invitation without trusting a client-side type."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from kohakuhub import repository_invites as invites
from kohakuhub.api import repository_access as access
from kohakuhub.api.admin.routers import invitations as admin
from kohakuhub.auth import routes
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import Invitation, RepositoryAccessGrant, RepositoryInvite, RepositoryInviteRedemption, User, UserOrganization, utcnow
from test.kohakuhub.support.factories import make_invitation, make_org, make_repo, make_user


@pytest.fixture
def signup(db_fresh, monkeypatch):
    monkeypatch.setattr(routes, "db", db_fresh)
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", False)
    monkeypatch.setattr(routes.cfg.auth, "require_email_verification", False)
    owner = make_user()
    repo = make_repo(owner, "invited", private=True, invite_mode=True)
    invite, code = invites.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=1))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.include_router(access.router, prefix="/api")
    app.include_router(admin.router, prefix="/api/admin")
    app.dependency_overrides[get_optional_user] = lambda: None
    app.dependency_overrides[admin.verify_admin_token] = lambda: True
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, repo=repo, invite=invite, code=code)


def payload(username="new-reader", **extra):
    return {"username": username, "email": f"{username}@example.com", "password": "secret-pass-1"} | extra


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("channel", ["field", "legacy-link"])
def test_admin_issued_invitation_registers_in_both_site_policies(signup, monkeypatch, closed, channel):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", closed)
    org = make_org("invited-team")
    result = signup.client.post("/api/admin/invitations/register", json={"org_id": org.id, "role": "visitor"})
    assert result.status_code == 200
    code = result.json()["token"]
    assert result.json()["invitation_link"].endswith("/register?invitation=" + code)
    kwargs = {"json": payload(invitation_code="  " + code + "  ")} if channel == "field" else {"json": payload(), "params": {"invitation_token": code}}
    result = signup.client.post("/api/auth/register", **kwargs)
    assert result.status_code == 200, result.text
    assert "repository_url" not in result.json()
    user = User.get(username="new-reader")
    invitation = Invitation.get(token=code)
    assert invitation.usage_count == 1 and invitation.used_by_id == user.id
    assert UserOrganization.get(user=user, organization=org).role == "visitor"
    assert RepositoryAccessGrant.select().count() == 0
    again = signup.client.post("/api/auth/register", json=payload("second-reader", invitation_code=code))
    assert again.status_code == 400 and User.get_or_none(username="second-reader") is None


def test_repository_code_in_unified_field_grants_and_returns_one_repository(signup):
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=signup.code))
    assert result.status_code == 200, result.text
    assert result.json()["repository_url"] == "/models/owner/invited"
    user = User.get(username="new-reader")
    assert RepositoryAccessGrant.get(user=user).repository_id == signup.repo.id
    assert RepositoryInviteRedemption.get(user=user).invite_id == signup.invite.id
    assert RepositoryInvite.get_by_id(signup.invite.id).redemption_count == 1
    assert UserOrganization.select().count() == 0


def test_actual_site_record_is_recognized_even_with_repository_like_prefix(signup, monkeypatch):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    invitation = make_invitation("repo_site_registration_fixture")
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=invitation.token))
    assert result.status_code == 200
    assert Invitation.get_by_id(invitation.id).usage_count == 1
    assert RepositoryAccessGrant.select().count() == 0


@pytest.mark.parametrize("mode", ["raw", "guest"])
@pytest.mark.parametrize("closed", [False, True])
def test_site_field_and_automatic_repository_context_are_both_consumed(signup, monkeypatch, mode, closed):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", closed)
    invitation = make_invitation("site-with-carried-invitation")
    extra = {"repo_invitation_token": signup.code}
    if mode == "guest":
        assert signup.client.post("/api/models/owner/invited/access/guest", json={"token": signup.code}).status_code == 200
        extra = {"repo_guest_repository_id": signup.repo.id}
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=invitation.token, **extra))
    assert result.status_code == 200, result.text
    assert result.json()["repository_url"] == "/models/owner/invited"
    assert Invitation.get_by_id(invitation.id).usage_count == 1
    assert RepositoryInvite.get_by_id(signup.invite.id).redemption_count == 1


@pytest.mark.parametrize("code", [None, "", "  ", "repo"])
def test_site_invitation_policy_cannot_be_bypassed_by_repository_code(signup, monkeypatch, code):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", True)
    value = signup.code if code == "repo" else code
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=value))
    assert result.status_code == 403
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryInvite.get_by_id(signup.invite.id).redemption_count == 0


@pytest.mark.parametrize("failure", ["missing", "wrong-action", "expired", "used", "malformed-parameters", "ambiguous"])
@pytest.mark.parametrize("closed", [False, True])
def test_invalid_supplied_codes_never_silently_create_accounts(signup, monkeypatch, failure, closed):
    monkeypatch.setattr(routes.cfg.auth, "invitation_only", closed)
    code = "unknown-invitation-fixture"
    if failure != "missing":
        code = signup.code if failure == "ambiguous" else "site-invalid-fixture"
        invitation = make_invitation(code)
        if failure == "wrong-action":
            invitation.action = "join_org"
        elif failure == "expired":
            invitation.expires_at = utcnow() - timedelta(seconds=1)
        elif failure == "used":
            invitation.usage_count = 1
            invitation.used_at = utcnow()
        elif failure == "malformed-parameters":
            invitation.parameters = "{"
        invitation.save()
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=code))
    assert result.status_code == 400, result.text
    assert User.get_or_none(username="new-reader") is None
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0


@pytest.mark.parametrize("kind", ["site", "repository"])
def test_duplicate_credentials_are_idempotent_inputs_but_conflicts_fail(signup, kind):
    if kind == "site":
        first, second = make_invitation("first-site"), make_invitation("second-site")
        code, other = first.token, second.token
        extra = {"params": {"invitation_token": other}}
    else:
        _, other = invites.issue_invite(signup.repo, User.get(username="owner"), expires_at=utcnow() + timedelta(days=1))
        code = signup.code
        extra = {}
    conflict_payload = payload(invitation_code=code, **({"repo_invitation_token": other} if kind == "repository" else {}))
    result = signup.client.post("/api/auth/register", json=conflict_payload, **extra)
    assert result.status_code == 400 and User.get_or_none(username="new-reader") is None
    duplicate_payload = payload(invitation_code=code, **({"repo_invitation_token": code} if kind == "repository" else {}))
    result = signup.client.post("/api/auth/register", json=duplicate_payload, params={"invitation_token": code})
    assert result.status_code == 200, result.text
    if kind == "site":
        assert Invitation.get_by_id(first.id).usage_count == 1
        assert Invitation.get_by_id(second.id).usage_count == 0
    else:
        assert RepositoryInvite.get_by_id(signup.invite.id).redemption_count == 1


def test_repository_failure_rolls_back_open_site_invitation_and_account(signup, monkeypatch):
    invitation = make_invitation("site-rollback-fixture")
    original = invites._grant
    def fail_after_grant(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected grant failure")
    monkeypatch.setattr(invites, "_grant", fail_after_grant)
    with pytest.raises(RuntimeError, match="injected grant failure"):
        signup.client.post("/api/auth/register", json=payload(invitation_code=invitation.token, repo_invitation_token=signup.code))
    assert User.get_or_none(username="new-reader") is None
    assert Invitation.get_by_id(invitation.id).usage_count == 0
    assert RepositoryInvite.get_by_id(signup.invite.id).redemption_count == 0
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0


def test_concurrent_site_invitation_signup_is_atomic(signup):
    invitation = make_invitation("one-site-account-fixture")
    ready = Barrier(2)
    def submit(index):
        with TestClient(signup.app) as client:
            ready.wait(timeout=10)
            return client.post("/api/auth/register", json=payload(f"race-reader-{index}", invitation_code=invitation.token)).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, [0, 1])) == [200, 400]
    assert User.select().where(User.username.startswith("race-reader-")).count() == 1
    assert Invitation.get_by_id(invitation.id).usage_count == 1


@pytest.mark.parametrize("max_usage", [-1, 2])
def test_reusable_admin_codes_preserve_usage_limits_and_first_account(signup, max_usage):
    invitation = make_invitation("reusable-site-fixture", max_usage=max_usage)
    for index in range(3):
        result = signup.client.post("/api/auth/register", json=payload(f"reusable-reader-{index}", invitation_code=invitation.token))
        assert result.status_code == (400 if index == 2 and max_usage == 2 else 200)
    stored = Invitation.get_by_id(invitation.id)
    assert stored.usage_count == (3 if max_usage == -1 else 2)
    assert stored.used_by_id == User.get(username="reusable-reader-0").id


def test_site_membership_write_failure_rolls_back_account_and_code(signup, monkeypatch):
    org = make_org("rollback-team")
    invitation = make_invitation("site-membership-rollback", parameters='{"org_id":' + str(org.id) + ',"role":"visitor"}')
    original = routes.create_user_organization
    def fail_after_membership(*args, **kwargs):
        # A deliberate failure after real writes checks the account transaction.
        original(*args, **kwargs)
        raise RuntimeError("injected membership failure")
    monkeypatch.setattr(routes, "create_user_organization", fail_after_membership)
    with pytest.raises(RuntimeError, match="injected membership failure"):
        signup.client.post("/api/auth/register", json=payload(invitation_code=invitation.token))
    assert User.get_or_none(username="new-reader") is None
    assert Invitation.get_by_id(invitation.id).usage_count == 0
    assert UserOrganization.select().count() == 0


@pytest.mark.parametrize("code", [None, "", "  "])
def test_open_registration_remains_available_without_a_code(signup, code):
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=code))
    assert result.status_code == 200
    assert "repository_url" not in result.json()


@pytest.mark.parametrize("code", ["x" * 257, 12, False])
def test_unified_field_rejects_invalid_shape(signup, code):
    assert signup.client.post("/api/auth/register", json=payload(invitation_code=code)).status_code == 422
    assert User.get_or_none(username="new-reader") is None


@pytest.mark.parametrize("parameters", ["[]", "null"])
def test_nonobject_invitation_parameters_cannot_be_consumed(signup, parameters):
    invitation = make_invitation("nonobject-site-fixture", parameters=parameters)
    result = signup.client.post("/api/auth/register", json=payload(invitation_code=invitation.token))
    assert result.status_code == 400
    assert User.get_or_none(username="new-reader") is None
    assert Invitation.get_by_id(invitation.id).usage_count == 0


@pytest.mark.parametrize("hint", ["", "  ", "x" * 257])
def test_invalid_automatic_repository_hint_cannot_silently_register(signup, hint):
    result = signup.client.post("/api/auth/register", json=payload(repo_invitation_token=hint))
    assert result.status_code == 400 and User.get_or_none(username="new-reader") is None


def test_oversized_legacy_query_code_cannot_silently_register(signup):
    result = signup.client.post("/api/auth/register", json=payload(), params={"invitation_token": "x" * 257})
    assert result.status_code == 400 and User.get_or_none(username="new-reader") is None
