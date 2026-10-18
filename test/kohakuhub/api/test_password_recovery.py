"""Changing a password, and the one-time reset link an administrator hands over (no email involved)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from kohakuhub.auth.utils import hash_password, hash_token
from kohakuhub.config import cfg
from kohakuhub.db import PasswordResetToken, Session, Token, User
from test.kohakuhub.support.factories import make_org, make_user

OLD = "OldPassw0rd!"
NEW = "NewPassw0rd!"
INVALID = "This reset link is invalid, expired or already used."


def new_user(prefix="pw", password=OLD, **overrides):
    name = f"{prefix}-{uuid4().hex[:8]}"
    make_user(name, password_hash=hash_password(password), **overrides)
    return name


async def login(app, username, password=OLD):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


async def can_login(app, username, password):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest:
        return (await guest.post("/api/auth/login", json={"username": username, "password": password})).status_code == 200


def sessions_of(username):
    return Session.select().join(User).where(User.username == username).count()


async def issue(admin_client, username, ttl=None):
    body = {} if ttl is None else {"ttl": ttl}
    response = await admin_client.post(f"/admin/api/users/{username}/password-reset-link", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def raw_token(link):
    return link["url"].split("#token=")[1]


def expire(username):
    PasswordResetToken.update(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)).where(
        PasswordResetToken.user == User.get(User.username == username)
    ).execute()


# ---- change password -----------------------------------------------------------------------------------------------


async def test_changing_the_password_keeps_this_session_and_signs_the_others_out(app):
    username = new_user()
    here, elsewhere = await login(app, username), await login(app, username)
    assert sessions_of(username) == 2

    response = await here.post("/api/auth/change-password", json={"current_password": OLD, "new_password": NEW})

    assert response.status_code == 200 and response.json()["success"] is True
    assert sessions_of(username) == 1
    assert (await here.get("/api/auth/me")).status_code == 200  # still signed in here
    assert (await elsewhere.get("/api/auth/me")).status_code == 401  # signed out there
    assert await can_login(app, username, NEW) and not await can_login(app, username, OLD)


async def test_a_wrong_current_password_changes_nothing(app):
    username = new_user()
    client = await login(app, username)

    response = await client.post("/api/auth/change-password", json={"current_password": "not-it-at-all", "new_password": NEW})

    assert response.status_code == 403 and response.json()["detail"] == "Current password is incorrect"
    assert await can_login(app, username, OLD) and not await can_login(app, username, NEW)


@pytest.mark.parametrize("bad", ["short", "1234567", "a" * 73, ""])
async def test_the_new_password_has_to_meet_the_policy(app, bad):
    username = new_user()
    client = await login(app, username)

    response = await client.post("/api/auth/change-password", json={"current_password": OLD, "new_password": bad})

    assert response.status_code == 422
    assert await can_login(app, username, OLD)


async def test_changing_the_password_needs_to_be_signed_in(client):
    response = await client.post("/api/auth/change-password", json={"current_password": OLD, "new_password": NEW})

    assert response.status_code == 401


async def test_with_an_access_token_and_no_session_every_session_goes_and_the_token_stays(app):
    username = new_user()
    web = await login(app, username)
    token = (await web.post("/api/auth/tokens/create", json={"name": "ci"})).json()["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as cli:
        response = await cli.post(
            "/api/auth/change-password",
            json={"current_password": OLD, "new_password": NEW},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert sessions_of(username) == 0
        # an access token is not a password: command-line users and CI keep working
        assert (await cli.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    assert Token.select().where(Token.token_hash == hash_token(token)).count() == 1


async def test_changing_the_password_withdraws_a_link_that_was_waiting(app, admin_client):
    username = new_user()
    link = await issue(admin_client, username)
    client = await login(app, username)

    await client.post("/api/auth/change-password", json={"current_password": OLD, "new_password": NEW})

    check = await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})
    assert check.status_code == 400


# ---- the administrator's link ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("ttl,seconds", [("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400), ("3d", 259200), ("7d", 604800)])
async def test_each_choice_gives_the_expiry_it_names(admin_client, ttl, seconds):
    username = new_user()

    link = await issue(admin_client, username, ttl)

    assert link["ttl"] == ttl and link["username"] == username
    expires = datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))
    assert abs((expires - datetime.now(timezone.utc)).total_seconds() - seconds) < 10


async def test_the_default_is_one_hour_and_the_link_carries_the_token_in_the_fragment(admin_client):
    username = new_user()

    link = await issue(admin_client, username)

    assert link["ttl"] == "1h"
    assert link["url"].startswith(f"{cfg.app.base_url.rstrip('/')}/reset-password#token=")
    assert len(raw_token(link)) == 64
    assert "?" not in link["url"]  # not in the query: it must not reach a server log or a Referer


async def test_only_the_stated_choices_are_accepted(admin_client):
    username = new_user()
    for bad in ("forever", "30d", "", 3600, None):
        response = await admin_client.post(f"/admin/api/users/{username}/password-reset-link", json={"ttl": bad})
        assert response.status_code == 422, bad


async def test_the_database_keeps_a_hash_not_the_token(admin_client):
    username = new_user()
    link = await issue(admin_client, username)

    row = PasswordResetToken.get(PasswordResetToken.user == User.get(User.username == username))

    assert row.token_hash == hash_token(raw_token(link))
    assert raw_token(link) not in (row.token_hash, row.created_by)


async def test_a_link_cannot_be_made_for_nobody_an_organization_or_a_disabled_account(admin_client):
    org = make_org(f"org-{uuid4().hex[:6]}")
    disabled = new_user(is_active=False)

    assert (await admin_client.post("/admin/api/users/nobody-here/password-reset-link", json={})).status_code == 404
    assert (await admin_client.post(f"/admin/api/users/{org.username}/password-reset-link", json={})).status_code == 400
    assert (await admin_client.post(f"/admin/api/users/{disabled}/password-reset-link", json={})).status_code == 400


async def test_the_link_endpoints_need_the_admin_token(client, owner_client):
    for method in ("post", "get", "delete"):
        for caller in (client, owner_client):
            response = await getattr(caller, method)("/admin/api/users/owner/password-reset-link")
            assert response.status_code in (401, 403), (method, response.status_code)


async def test_the_status_says_whether_a_link_waits_and_lists_the_choices(admin_client):
    username = new_user()

    before = (await admin_client.get(f"/admin/api/users/{username}/password-reset-link")).json()
    assert before["active"] is False and before["expires_at"] is None
    assert before["default"] == "1h"
    assert [c["key"] for c in before["choices"]] == ["15m", "1h", "6h", "24h", "3d", "7d"]
    assert before["choices"][0] == {"key": "15m", "seconds": 900, "label": "15 minutes"}

    link = await issue(admin_client, username, "24h")
    after = (await admin_client.get(f"/admin/api/users/{username}/password-reset-link")).json()
    assert after["active"] is True and after["expires_at"] == link["expires_at"] and after["created_at"]
    assert "url" not in after and "token" not in after  # the link is shown once, when it is made


async def test_revoking_withdraws_the_link(admin_client, client):
    username = new_user()
    link = await issue(admin_client, username)

    revoked = await admin_client.delete(f"/admin/api/users/{username}/password-reset-link")

    assert revoked.status_code == 200 and revoked.json() == {"revoked": 1}
    assert (await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})).status_code == 400
    again = await admin_client.delete(f"/admin/api/users/{username}/password-reset-link")
    assert again.json() == {"revoked": 0}
    assert (await admin_client.delete("/admin/api/users/nobody-here/password-reset-link")).status_code == 404
    assert (await admin_client.get("/admin/api/users/nobody-here/password-reset-link")).status_code == 404


async def test_a_second_link_replaces_the_first(admin_client, client):
    username = new_user()
    first = await issue(admin_client, username, "7d")
    second = await issue(admin_client, username, "15m")

    assert (await client.post("/api/auth/password-reset/check", json={"token": raw_token(first)})).status_code == 400
    assert (await client.post("/api/auth/password-reset/check", json={"token": raw_token(second)})).status_code == 200


# ---- what the person who gets the link does -------------------------------------------------------------------------


async def test_opening_the_link_names_the_account_and_the_expiry(admin_client, client):
    username = new_user()
    link = await issue(admin_client, username, "6h")

    response = await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})

    assert response.status_code == 200
    assert response.json() == {"username": username, "expires_at": link["expires_at"]}


async def test_setting_a_new_password_with_the_link(app, admin_client, client):
    username = new_user()
    elsewhere = await login(app, username)
    link = await issue(admin_client, username)

    response = await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})

    assert response.status_code == 200 and response.json()["success"] is True
    assert response.json()["username"] == username
    assert "set-cookie" not in response.headers  # nobody is signed in by it
    assert sessions_of(username) == 0  # and every session is gone
    assert (await elsewhere.get("/api/auth/me")).status_code == 401
    assert await can_login(app, username, NEW) and not await can_login(app, username, OLD)


async def test_a_reset_leaves_access_tokens_alone(app, admin_client, client):
    username = new_user()
    web = await login(app, username)
    token = (await web.post("/api/auth/tokens/create", json={"name": "ci"})).json()["token"]
    link = await issue(admin_client, username)

    await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})

    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_a_link_works_once(admin_client, client):
    username = new_user()
    link = await issue(admin_client, username)
    first = await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})
    assert first.status_code == 200

    second = await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": "Another1Passw0rd"})
    check = await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})

    assert second.status_code == 400 and check.status_code == 400


async def test_a_link_used_up_between_the_look_up_and_the_update_is_refused(app, admin_client, client, monkeypatch):
    """Two requests with one link: the one that is second finds the link used up by the conditional update, not by the look-up."""
    import sys

    routes = sys.modules["kohakuhub.auth.routes"]  # the copy the running app uses
    username = new_user()
    link = await issue(admin_client, username)
    real_find = routes.find_valid

    def find_then_let_the_other_request_use_it(token):
        row = real_find(token)
        routes.claim(row)  # the other request got there first, in the real database
        return row

    monkeypatch.setattr(routes, "find_valid", find_then_let_the_other_request_use_it)

    response = await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})

    assert response.status_code == 400 and response.json()["detail"] == INVALID
    assert await can_login(app, username, OLD) and not await can_login(app, username, NEW)


async def test_a_weak_new_password_does_not_use_up_the_link(app, admin_client, client):
    username = new_user()
    link = await issue(admin_client, username)

    weak = await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": "short"})
    assert weak.status_code == 422
    assert (await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})).status_code == 200

    assert (await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})).status_code == 200


async def test_unknown_expired_used_and_withdrawn_links_all_answer_the_same(admin_client, client):
    unknown = "f" * 64
    expired_user = new_user()
    expired = await issue(admin_client, expired_user)
    expire(expired_user)
    used_user = new_user()
    used = await issue(admin_client, used_user)
    await client.post("/api/auth/password-reset", json={"token": raw_token(used), "new_password": NEW})
    withdrawn_user = new_user()
    withdrawn = await issue(admin_client, withdrawn_user)
    await admin_client.delete(f"/admin/api/users/{withdrawn_user}/password-reset-link")

    answers = set()
    for token in (unknown, raw_token(expired), raw_token(used), raw_token(withdrawn)):
        for path, body in (
            ("/api/auth/password-reset/check", {"token": token}),
            ("/api/auth/password-reset", {"token": token, "new_password": NEW}),
        ):
            response = await client.post(path, json=body)
            assert response.status_code == 400, (path, token)
            answers.add(response.text)
    assert answers == {f'{{"detail":"{INVALID}"}}'}


async def test_the_link_of_an_account_disabled_since_is_refused(admin_client, client):
    username = new_user()
    link = await issue(admin_client, username)
    User.update(is_active=False).where(User.username == username).execute()

    assert (await client.post("/api/auth/password-reset/check", json={"token": raw_token(link)})).status_code == 400
    assert (await client.post("/api/auth/password-reset", json={"token": raw_token(link), "new_password": NEW})).status_code == 400


# ---- the policy where passwords were taken as they came ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "short", "a" * 73])
async def test_registration_applies_the_policy(client, bad):
    response = await client.post(
        "/api/auth/register",
        json={"username": f"reg-{uuid4().hex[:8]}", "email": f"{uuid4().hex[:8]}@example.com", "password": bad},
    )

    assert response.status_code == 422


async def test_registration_accepts_a_password_that_meets_it(client):
    name = f"reg-{uuid4().hex[:8]}"

    response = await client.post(
        "/api/auth/register",
        json={"username": name, "email": f"{name}@example.com", "password": "long-enough-1"},
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("bad", ["", "short", "a" * 73])
async def test_admin_create_user_applies_the_policy(admin_client, bad):
    response = await admin_client.post(
        "/admin/api/users",
        json={"username": f"adm-{uuid4().hex[:8]}", "email": f"{uuid4().hex[:8]}@example.com", "password": bad},
    )

    assert response.status_code == 422


async def test_logging_in_with_a_password_older_than_the_policy_still_works(app):
    username = new_user(password="short")  # set before there was a rule

    assert await can_login(app, username, "short")
