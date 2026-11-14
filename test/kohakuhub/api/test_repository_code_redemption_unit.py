"""A signed-in account can exchange a repository code without knowing its address."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from kohakuhub import repository_invites as invites
from kohakuhub.api import repository_access as access
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.auth.utils import hash_token
from kohakuhub.db import (
    Repository, RepositoryAccessGrant, RepositoryGuestSession, RepositoryInvite,
    RepositoryInviteRedemption, Session, Token, User, UserOrganization, utcnow,
)
from test.kohakuhub.support.factories import make_invitation, make_repo, make_session, make_token, make_user


@pytest.fixture
def exchange(db_fresh):
    owner, reader = make_user(), make_user("reader")
    repo = make_repo(owner, "invited", private=True, invite_mode=True)
    row, token = invites.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=1))
    login = make_session(reader, "reader-session", expires_at=datetime.now(timezone.utc) + timedelta(days=30))
    app = FastAPI()
    app.include_router(access.router, prefix="/api")
    with TestClient(app) as client:
        client.cookies.set("session_id", login.session_id)
        yield SimpleNamespace(client=client, app=app, owner=owner, reader=reader, repo=repo,
                              row=row, token=token, login=login, endpoint="/api/repo-access/redeem")


def redeem(exchange, **changes):
    return exchange.client.post(exchange.endpoint, json={"token": exchange.token} | changes)


@pytest.mark.parametrize("credential", ["anonymous", "guest", "inactive-account", "expired-login"])
def test_code_resolution_requires_a_normal_active_account_before_lookup(exchange, monkeypatch, credential):
    if credential in ("anonymous", "guest"):
        exchange.client.cookies.clear()
        if credential == "guest":
            _, visitor_secret = invites.redeem_guest(exchange.repo, exchange.token)
            exchange.client.cookies.set(access.guest_cookie_name(exchange.repo), visitor_secret)
    elif credential == "inactive-account":
        User.update(is_active=False).where(User.id == exchange.reader.id).execute()
    else:
        Session.update(expires_at=utcnow() - timedelta(seconds=1)).where(Session.id == exchange.login.id).execute()

    def forbidden_lookup(_):
        raise AssertionError("Anonymous callers must not resolve invitation metadata")
    monkeypatch.setattr(invites, "find_invite", forbidden_lookup)
    result = redeem(exchange)
    assert result.status_code == 401
    assert "repository_url" not in result.text and exchange.token not in result.text
    assert RepositoryInvite.get_by_id(exchange.row.id).redemption_count == 0
    assert RepositoryAccessGrant.select().count() == 0


@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
@pytest.mark.parametrize("invite_mode", [False, True])
def test_code_alone_grants_one_repository_and_returns_its_address(exchange, kind, invite_mode):
    Repository.update(repo_type=kind, invite_mode=invite_mode).where(Repository.id == exchange.repo.id).execute()
    before_sessions = list(Session.select().dicts())
    result = redeem(exchange, token=" " + exchange.token + " ")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["success"] is True
    assert body["repository_url"] == f"/{kind}s/owner/invited"
    assert body["grant"]["user_id"] == exchange.reader.id
    assert body["grant"]["revoked_at"] is None and body["grant"]["expires_at"] is None
    assert result.headers["cache-control"] == "no-store"
    assert exchange.token not in result.text and "token_hash" not in result.text
    assert check_repo_read_permission(Repository.get_by_id(exchange.repo.id), exchange.reader)
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    assert RepositoryInvite.get_by_id(exchange.row.id).redemption_count == 1
    assert UserOrganization.select().count() == RepositoryGuestSession.select().count() == Token.select().count() == 0
    assert list(Session.select().dicts()) == before_sessions
    assert exchange.client.cookies.get("session_id") == exchange.login.session_id


def test_existing_normal_personal_token_can_redeem_without_creating_a_new_token(exchange):
    token = "hf_existing_test_fixture"
    make_token(exchange.reader, hash_token(token))
    exchange.client.cookies.clear()
    result = exchange.client.post(exchange.endpoint, json={"token": exchange.token}, headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert Token.select().count() == 1
    assert result.json()["grant"]["user_id"] == exchange.reader.id


def test_return_path_uses_canonical_encoded_repository_components(exchange):
    Repository.update(namespace="owner team", name="测试#1").where(Repository.id == exchange.repo.id).execute()
    result = redeem(exchange)
    assert result.status_code == 200
    assert result.json()["repository_url"] == "/models/owner%20team/%E6%B5%8B%E8%AF%95%231"


@pytest.mark.parametrize("changes", [{"repo_type": "invalid"}, {"name": "../other"}, {"namespace": "owner/other"}, {"name": "%2fsecret"}])
def test_invalid_repository_target_does_not_consume_or_grant(exchange, changes):
    Repository.update(**changes).where(Repository.id == exchange.repo.id).execute()
    result = redeem(exchange)
    assert result.status_code == 400
    assert "repository_url" not in result.text
    assert RepositoryInvite.get_by_id(exchange.row.id).redemption_count == 0
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 0


@pytest.mark.parametrize("failure", ["unknown", "site-code", "expired", "revoked", "used-by-other", "grant-expired", "public-repository", "deleted-repository"])
def test_unavailable_code_does_not_create_a_grant(exchange, failure):
    code = exchange.token
    if failure == "unknown":
        code = "repo_unknown_fixture"
    elif failure == "site-code":
        code = "site_registration_fixture"
        make_invitation(code)
    elif failure == "expired":
        RepositoryInvite.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryInvite.id == exchange.row.id).execute()
    elif failure == "revoked":
        invites.revoke_invite(exchange.repo, exchange.row.id, exchange.owner)
    elif failure == "used-by-other":
        invites.redeem_account(exchange.repo, exchange.token, make_user("another"))
    elif failure == "grant-expired":
        RepositoryInvite.update(grant_expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryInvite.id == exchange.row.id).execute()
    elif failure == "public-repository":
        Repository.update(private=False, invite_mode=False).where(Repository.id == exchange.repo.id).execute()
    else:
        exchange.repo.delete_instance(recursive=True)
    before_count = RepositoryAccessGrant.select().count()
    result = redeem(exchange, token=code)
    assert result.status_code == 400
    assert result.json()["detail"]["error"] == "Invalid or unavailable invitation code"
    assert "repository_url" not in result.text and code not in result.text
    assert RepositoryAccessGrant.select().count() == before_count
    assert RepositoryAccessGrant.get_or_none(user=exchange.reader) is None


@pytest.mark.parametrize("inactive", [None, "revoked", "expired", "deleted"])
def test_same_account_repeat_never_consumes_again_or_renews_an_inactive_grant(exchange, inactive):
    first = redeem(exchange)
    assert first.status_code == 200
    grant = RepositoryAccessGrant.get(user=exchange.reader)
    if inactive == "revoked":
        invites.revoke_grant(exchange.repo, grant.id, exchange.owner)
    elif inactive == "expired":
        RepositoryAccessGrant.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryAccessGrant.id == grant.id).execute()
    elif inactive == "deleted":
        grant.delete_instance()
    second = redeem(exchange)
    assert second.status_code == (400 if inactive == "deleted" else 200)
    if inactive is None:
        assert second.json() == first.json()
    elif inactive != "deleted":
        assert second.json()["grant"]["revoked_at" if inactive == "revoked" else "expires_at"] is not None
    assert RepositoryInvite.get_by_id(exchange.row.id).redemption_count == 1
    assert RepositoryInviteRedemption.select().count() == 1
    assert RepositoryAccessGrant.select().count() == (0 if inactive == "deleted" else 1)


def test_two_accounts_racing_one_code_produce_one_grant(exchange):
    second_reader = make_user("second-reader")
    second_login = make_session(second_reader, "second-reader-session", expires_at=datetime.now(timezone.utc) + timedelta(days=30))
    barrier = Barrier(2)

    def exchange_code(session_id):
        with TestClient(exchange.app) as client:
            client.cookies.set("session_id", session_id)
            barrier.wait(timeout=10)
            return client.post(exchange.endpoint, json={"token": exchange.token}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(exchange_code, [exchange.login.session_id, second_login.session_id])) == [200, 400]
    assert RepositoryAccessGrant.select().count() == RepositoryInviteRedemption.select().count() == 1
    assert RepositoryInvite.get_by_id(exchange.row.id).redemption_count == 1
