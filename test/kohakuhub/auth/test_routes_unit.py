"""Unit tests for auth routes, on real user, session, token, invitation and verification rows."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import kohakuhub.auth.routes as auth_routes
from kohakuhub.auth.utils import hash_password, hash_token
from kohakuhub.db import EmailVerification, Session, Token, UserOrganization
from kohakuhub.utils.names import normalize_name
from test.kohakuhub.support.factories import (
    make_email_verification,
    make_invitation,
    make_org,
    make_session,
    make_token,
    make_user,
)

pytestmark = pytest.mark.usefixtures("db_scope")


def _count_transactions(monkeypatch, db_scope, seen):
    """Count the transactions the routes open; the writes inside stay real on ``db_scope``."""

    @contextmanager
    def counted():
        seen["entered"] = seen.get("entered", 0) + 1
        try:
            with db_scope.atomic():
                yield
        finally:
            seen["exited"] = seen.get("exited", 0) + 1

    monkeypatch.setattr(auth_routes, "db", SimpleNamespace(atomic=counted))


@pytest.mark.asyncio
async def test_register_covers_invitation_reserved_and_conflict_paths(monkeypatch, db_scope):
    atomic_state = {}
    _count_transactions(monkeypatch, db_scope, atomic_state)
    monkeypatch.setattr(auth_routes.cfg.auth, "invitation_only", True)
    monkeypatch.setattr(auth_routes.cfg.auth, "require_email_verification", False)

    def request(username="new-user", email="new@example.com"):
        return auth_routes.RegisterRequest(username=username, email=email, password="secret")

    with pytest.raises(HTTPException) as missing_token:
        await auth_routes.register(request())
    assert missing_token.value.status_code == 403

    with pytest.raises(HTTPException) as invalid_token:
        await auth_routes.register(request(), invitation_token="bad-token")
    assert invalid_token.value.status_code == 400

    make_invitation("wrong-type", action="join_org")
    with pytest.raises(HTTPException) as invalid_type:
        await auth_routes.register(request(), invitation_token="wrong-type")
    assert invalid_type.value.status_code == 400

    make_invitation("expired", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    with pytest.raises(HTTPException) as unavailable:
        await auth_routes.register(request(), invitation_token="expired")
    assert unavailable.value.detail == "Invitation has expired"

    monkeypatch.setattr(auth_routes.cfg.auth, "invitation_only", False)
    with pytest.raises(HTTPException) as reserved:
        await auth_routes.register(request(username="api"))
    assert reserved.value.status_code == 400

    make_user("taken")
    with pytest.raises(HTTPException) as username_exists:
        await auth_routes.register(request(username="taken"))
    assert username_exists.value.detail == "Username already exists"

    make_user("emailowner", email="new@example.com")
    with pytest.raises(HTTPException) as email_exists:
        await auth_routes.register(request())
    assert email_exists.value.detail == "Email already exists"

    make_org("Taken-User", normalized_name=normalize_name("Taken-User"))
    with pytest.raises(HTTPException) as normalized_conflict:
        await auth_routes.register(request(username="Taken_User", email="conflict@example.com"))
    assert "organization: Taken-User" in normalized_conflict.value.detail
    assert atomic_state["entered"] >= 3


@pytest.mark.asyncio
async def test_register_covers_invitation_processing_and_email_verification(monkeypatch, db_scope):
    atomic_state = {}
    _count_transactions(monkeypatch, db_scope, atomic_state)
    org = make_org("org-team")
    make_invitation(
        "invite-1",
        parameters=json.dumps({"org_id": org.id, "org_name": "org-team", "role": "admin"}),
    )
    make_invitation("invite-2", parameters="{not-json")

    async def _fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(auth_routes.cfg.auth, "invitation_only", True)
    monkeypatch.setattr(auth_routes.cfg.auth, "require_email_verification", True)
    verify_tokens = iter(["verify-token", "verify-token-2"])
    monkeypatch.setattr(auth_routes, "generate_token", lambda: next(verify_tokens))
    monkeypatch.setattr(auth_routes.asyncio, "to_thread", _fake_to_thread)
    # SMTP stays mocked; the verification row it belongs to is real.
    monkeypatch.setattr(auth_routes, "send_verification_email", lambda email, username, token: False)

    first = await auth_routes.register(
        auth_routes.RegisterRequest(
            username="fresh-user",
            email="fresh@example.com",
            password="secret",
        ),
        invitation_token="invite-1",
    )
    assert first == {
        "success": True,
        "message": "User created but failed to send verification email",
        "email_verified": False,
    }
    fresh = auth_routes.get_user_by_username("fresh-user")
    db_invitation = auth_routes.get_invitation("invite-1")
    assert db_invitation.used_by_id == fresh.id
    assert db_invitation.usage_count == 1
    membership = UserOrganization.get_or_none(
        (UserOrganization.user == fresh) & (UserOrganization.organization == org)
    )
    assert membership is not None and membership.role == "admin"
    assert EmailVerification.get(EmailVerification.user == fresh).token == "verify-token"

    monkeypatch.setattr(auth_routes, "send_verification_email", lambda email, username, token: True)
    second = await auth_routes.register(
        auth_routes.RegisterRequest(
            username="second-user",
            email="second@example.com",
            password="secret",
        ),
        invitation_token="invite-2",
    )
    assert second == {
        "success": True,
        "message": "User created. Please check your email to verify your account.",
        "email_verified": False,
    }
    second_user = auth_routes.get_user_by_username("second-user")
    assert EmailVerification.get(EmailVerification.user == second_user).token == "verify-token-2"
    # The invalid invitation JSON is logged and skipped, so it is not marked used.
    assert auth_routes.get_invitation("invite-2").usage_count == 0


@pytest.mark.asyncio
async def test_verify_login_logout_and_token_routes_cover_remaining_paths(monkeypatch, db_scope):
    now = datetime.now(timezone.utc)
    atomic_state = {}
    _count_transactions(monkeypatch, db_scope, atomic_state)
    password_hash = hash_password("secret")

    active_user = make_user(
        "alice",
        email="alice@example.com",
        email_verified=True,
        password_hash=password_hash,
    )
    make_user(
        "disabled",
        email="disabled@example.com",
        email_verified=True,
        password_hash=password_hash,
        is_active=False,
    )
    make_user(
        "unverified",
        email="unverified@example.com",
        email_verified=False,
        password_hash=password_hash,
    )
    # Stored naive, as a timestamp column without a zone returns it, to reach the naive-expiry branch.
    make_email_verification(
        active_user,
        "expired-token",
        expires_at=(now - timedelta(hours=1)).replace(tzinfo=None),
    )
    make_email_verification(active_user, "valid-token", expires_at=now + timedelta(hours=1))

    monkeypatch.setattr(auth_routes.cfg.auth, "session_expire_hours", 12)
    token_values = iter(["session-id", "login-session-id", "api-token"])
    monkeypatch.setattr(auth_routes, "generate_token", lambda: next(token_values))
    monkeypatch.setattr(auth_routes, "generate_session_secret", lambda: "session-secret")

    # The schema forbids a verification row without a user, so the orphan branch in
    # verify_email can only be reached by stubbing the lookup for that one token.
    real_get_email_verification = auth_routes.get_email_verification

    def get_email_verification(token):
        if token == "no-user-token":
            return SimpleNamespace(expires_at=now + timedelta(hours=1), user=None)
        return real_get_email_verification(token)

    monkeypatch.setattr(auth_routes, "get_email_verification", get_email_verification)

    invalid_verify = await auth_routes.verify_email("missing-token", Response())
    assert invalid_verify.headers["location"].startswith("/?error=invalid_token")

    expired_verify = await auth_routes.verify_email(
        "expired-token",
        Response(),
    )
    assert expired_verify.headers["location"].startswith("/?error=invalid_token")

    no_user_verify = await auth_routes.verify_email("no-user-token", Response())
    assert no_user_verify.headers["location"] == "/?error=user_not_found"

    success_verify = await auth_routes.verify_email("valid-token", Response())
    assert success_verify.headers["location"] == "/alice"
    assert any(header[0] == b"set-cookie" for header in success_verify.raw_headers)
    assert EmailVerification.get_or_none(EmailVerification.token == "valid-token") is None
    verified_session = Session.get(Session.session_id == "session-id")
    assert verified_session.user_id == active_user.id
    assert verified_session.secret == "session-secret"
    assert verified_session.expires_at.replace(tzinfo=timezone.utc) > now + timedelta(hours=11)

    with pytest.raises(HTTPException) as bad_login:
        await auth_routes.login(
            auth_routes.LoginRequest(username="alice", password="wrong"),
            Response(),
        )
    assert bad_login.value.status_code == 401

    with pytest.raises(HTTPException) as disabled_login:
        await auth_routes.login(
            auth_routes.LoginRequest(username="disabled", password="secret"),
            Response(),
        )
    assert disabled_login.value.detail == "Account is disabled"

    monkeypatch.setattr(auth_routes.cfg.auth, "require_email_verification", True)
    with pytest.raises(HTTPException) as unverified_login:
        await auth_routes.login(
            auth_routes.LoginRequest(username="unverified", password="secret"),
            Response(),
        )
    assert unverified_login.value.detail == "Please verify your email first"

    success_response = Response()
    monkeypatch.setattr(auth_routes.cfg.auth, "require_email_verification", False)
    login_payload = await auth_routes.login(
        auth_routes.LoginRequest(username="alice", password="secret"),
        success_response,
    )
    assert login_payload["session_secret"] == "session-secret"
    assert "session_id=login-session-id" in success_response.headers["set-cookie"]
    assert Session.get(Session.session_id == "login-session-id").user_id == active_user.id

    logout_response = Response()
    logout_payload = await auth_routes.logout(logout_response, active_user)
    assert logout_payload["success"] is True
    assert Session.select().where(Session.user == active_user).count() == 0

    me = auth_routes.get_me(active_user)
    assert me["username"] == "alice"

    make_token(active_user, hash_token("first-token"), name="first")
    make_token(active_user, hash_token("second-token"), name="second", last_used=now)
    listed_tokens = await auth_routes.list_tokens(active_user)
    by_name = {t["name"]: t for t in listed_tokens["tokens"]}
    assert by_name["first"]["last_used"] is None
    assert by_name["second"]["last_used"] is not None

    make_session(active_user, "browser-session", secret="browser-secret")
    created = await auth_routes.create_token_endpoint(
        auth_routes.CreateTokenRequest(name="cli"),
        active_user,
    )
    assert created["session_secret"] == "browser-secret"
    created_row = Token.get(Token.id == created["token_id"])
    assert created_row.user_id == active_user.id
    assert created_row.token_hash == hash_token("api-token")
    assert created_row.name == "cli"

    with pytest.raises(HTTPException) as missing_token:
        await auth_routes.revoke_token(created["token_id"] + 100, active_user)
    assert missing_token.value.status_code == 404

    revoked = await auth_routes.revoke_token(created["token_id"], active_user)
    assert revoked["success"] is True
    assert Token.get_or_none(Token.id == created["token_id"]) is None

    assert atomic_state["entered"] >= 1
