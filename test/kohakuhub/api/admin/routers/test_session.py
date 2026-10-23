"""Admin sessions over HTTP, on the real stack: the shared token exchanged once for an HttpOnly cookie.

The token keeps working in ``X-Admin-Token`` (scripts); a cookie request that changes something also needs the panel's own
header and an allowed ``Origin``; the cookie only travels under ``/admin``, so the three repository actions the panel used
to send with the token have admin-scoped routes.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

TEMPLATES = "/admin/api/database/templates"  # cheap, needs only the token
SESSION = "/admin/api/session"
SESSIONS = "/admin/api/admin-sessions"  # (/admin/api/sessions is the list of the users' login sessions)
ORIGIN = "http://localhost:48888"
PANEL = {"X-Requested-With": "cheesecave-admin", "Origin": ORIGIN}


@pytest.fixture
def live(monkeypatch):
    """The settings of the *running* app (the bootstrap reloads every kohakuhub module)."""
    config = sys.modules["kohakuhub.config"].cfg
    for name, value in {
        "auth_free_failures": 3,
        "auth_base_wait_seconds": 1,
        "auth_max_wait_seconds": 900,
        "auth_throttle_enabled": True,
        "auth_trusted_proxy_hops": 0,
        "auth_allowlist": [],
        "allow_weak_token": True,  # the test token is only 16 characters
        "session_enabled": True,
        "session_idle_minutes": 30,
        "session_absolute_hours": 12,
        "session_cookie_secure": False,  # the test client talks plain HTTP
        "session_allowed_origins": [],
    }.items():
        monkeypatch.setattr(config.admin, name, value)
    monkeypatch.setattr(config.app, "base_url", ORIGIN)
    module = sys.modules["kohakuhub.auth.admin_token"]
    module.throttle.set_clock(time.time)
    module.throttle.reset()
    sys.modules["kohakuhub.db"].AdminSession.delete().execute()
    return config


def token_of(admin_client):
    return admin_client.headers["X-Admin-Token"]


async def sign_in(client, admin_client, **extra):
    return await client.post(SESSION, json={"token": token_of(admin_client)}, headers={**PANEL, **extra})


async def test_the_token_is_exchanged_once_for_an_http_only_cookie(client, admin_client, live):
    response = await sign_in(client, admin_client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"expires_at", "absolute_expires_at"}
    assert body["expires_at"] < body["absolute_expires_at"]
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("admin_session=")
    assert "httponly" in cookie and "path=/admin" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # the setting says plain HTTP here
    assert "max-age=43" in cookie  # 12 hours, less the moment it took
    assert token_of(admin_client) not in response.text and token_of(admin_client) not in cookie


async def test_the_cookie_is_enough_for_every_admin_route_and_the_header_is_not_needed(client, admin_client, live):
    await sign_in(client, admin_client)

    assert (await client.get(TEMPLATES)).status_code == 200
    assert (await client.get("/admin/api/auth/client")).status_code == 200


async def test_the_secure_attribute_follows_the_public_address(client, admin_client, live, monkeypatch):
    monkeypatch.setattr(live.admin, "session_cookie_secure", None)
    monkeypatch.setattr(live.app, "base_url", "https://hub.example.test")
    secure = await client.post(
        SESSION, json={"token": token_of(admin_client)}, headers={**PANEL, "Origin": "https://hub.example.test"}
    )

    assert secure.status_code == 201 and "secure" in secure.headers["set-cookie"].lower()


async def test_a_wrong_token_is_403_and_the_throttle_of_the_token_check_applies_to_the_login(client, live):
    for _ in range(4):
        wrong = await client.post(SESSION, json={"token": "not-the-token"}, headers=PANEL)
        assert wrong.status_code == 403
    waiting = await client.post(SESSION, json={"token": "not-the-token"}, headers=PANEL)

    assert waiting.status_code == 429 and waiting.headers["retry-after"] == "1"
    assert "set-cookie" not in waiting.headers


async def test_the_login_needs_the_panels_header_and_an_allowed_origin(client, admin_client, live):
    body = {"token": token_of(admin_client)}
    assert (await client.post(SESSION, json=body)).status_code == 403
    assert (await client.post(SESSION, json=body, headers={"X-Requested-With": "cheesecave-admin"})).status_code == 403
    assert (await client.post(SESSION, json=body, headers={**PANEL, "Origin": "https://evil.example"})).status_code == 403
    refused = await client.post(SESSION, json=body, headers={"Origin": ORIGIN})
    assert refused.status_code == 403 and "X-Requested-With" in refused.json()["detail"]["error"]
    assert "set-cookie" not in refused.headers
    assert (await client.post(SESSION, json=body, headers=PANEL)).status_code == 201


async def test_an_origin_the_operator_added_is_allowed(client, admin_client, live, monkeypatch):
    monkeypatch.setattr(live.admin, "session_allowed_origins", ["https://panel.example.test"])

    response = await client.post(
        SESSION, json={"token": token_of(admin_client)}, headers={**PANEL, "Origin": "https://panel.example.test"}
    )

    assert response.status_code == 201


async def test_a_reload_restores_the_session_and_says_when_it_ends(client, admin_client, live):
    assert (await client.get(SESSION)).status_code == 401  # nobody is signed in
    login = (await sign_in(client, admin_client)).json()

    restored = await client.get(SESSION)

    assert restored.status_code == 200
    assert restored.json()["absolute_expires_at"] == login["absolute_expires_at"]


async def test_restoring_needs_the_cookie_and_not_the_header(client, admin_client, live):
    # a script with the token is not a browser session: there is nothing to restore
    response = await client.get(SESSION, headers={"X-Admin-Token": token_of(admin_client)})

    assert response.status_code == 401


async def test_signing_out_ends_the_session_and_clears_the_cookie(client, admin_client, live):
    await sign_in(client, admin_client)

    out = await client.delete(SESSION, headers=PANEL)

    assert out.status_code == 200 and "admin_session=" in out.headers["set-cookie"]
    assert "max-age=0" in out.headers["set-cookie"].lower()
    stolen = client.cookies.get("admin_session")  # the cookie value before the browser dropped it
    assert (await client.get(TEMPLATES)).status_code == 401
    client.cookies.set("admin_session", stolen or "x", domain="testserver.local", path="/admin")
    assert (await client.get(TEMPLATES)).status_code == 401  # revoked on the server, not only forgotten


async def test_a_change_made_with_the_cookie_needs_the_panels_header_and_origin(client, admin_client, live):
    await sign_in(client, admin_client)

    assert (await client.post(f"{SESSIONS}/revoke-others")).status_code == 403
    assert (await client.post(f"{SESSIONS}/revoke-others", headers={"Origin": ORIGIN})).status_code == 403
    assert (await client.post(f"{SESSIONS}/revoke-others", headers={**PANEL, "Origin": "https://evil.example"})).status_code == 403
    assert (await client.post(f"{SESSIONS}/revoke-others", headers=PANEL)).status_code == 200
    assert (await client.get(TEMPLATES)).status_code == 200  # reads need only the cookie


async def test_the_header_token_is_unchanged_and_decides_when_both_are_there(client, admin_client, live):
    await sign_in(client, admin_client)

    assert (await admin_client.get(TEMPLATES)).status_code == 200  # a script, no cookie
    assert (await admin_client.post(f"{SESSIONS}/revoke-others")).status_code == 200  # no CSRF rules for the header
    wrong_header = await client.get(TEMPLATES, headers={"X-Admin-Token": "not-the-token"})
    assert wrong_header.status_code == 403  # the header was sent, so it is the header that is judged


async def test_a_session_that_is_idle_for_too_long_is_over(client, admin_client, live):
    await sign_in(client, admin_client)
    model = sys.modules["kohakuhub.db"].AdminSession
    model.update(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)).execute()

    response = await client.get(TEMPLATES)

    assert response.status_code == 401
    assert "session" in response.json()["detail"]["error"].lower()


async def test_a_session_past_its_absolute_end_is_over_even_if_it_was_just_used(client, admin_client, live):
    await sign_in(client, admin_client)
    sys.modules["kohakuhub.db"].AdminSession.update(
        absolute_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    ).execute()

    assert (await client.get(TEMPLATES)).status_code == 401


async def test_changing_the_shared_token_ends_the_sessions(client, admin_client, live, monkeypatch):
    await sign_in(client, admin_client)
    assert (await client.get(TEMPLATES)).status_code == 200

    monkeypatch.setattr(live.admin, "secret_token", "the-token-was-rotated-0123456789")

    assert (await client.get(TEMPLATES)).status_code == 401
    assert (await client.get(SESSION)).status_code == 401


async def test_a_weak_token_or_a_disabled_api_closes_the_cookie_too(client, admin_client, live, monkeypatch):
    await sign_in(client, admin_client)

    monkeypatch.setattr(live.admin, "allow_weak_token", False)
    weak = await client.get(TEMPLATES)
    assert weak.status_code == 503 and "too weak" in weak.json()["detail"]["error"]

    monkeypatch.setattr(live.admin, "allow_weak_token", True)
    monkeypatch.setattr(live.admin, "enabled", False)
    assert (await client.get(TEMPLATES)).status_code == 503


async def test_sessions_can_be_switched_off_and_the_panel_then_sees_a_404(client, admin_client, live, monkeypatch):
    monkeypatch.setattr(live.admin, "session_enabled", False)

    assert (await sign_in(client, admin_client)).status_code == 404
    assert (await client.get(SESSION)).status_code == 404
    assert (await admin_client.get(TEMPLATES)).status_code == 200  # the token still works


async def test_the_list_shows_the_sessions_and_marks_the_one_asking(client, admin_client, live):
    other = await sign_in(admin_client.__class__(transport=client._transport, base_url="http://testserver"), admin_client)
    assert other.status_code == 201
    await sign_in(client, admin_client, **{"User-Agent": "Panel/1.0"})

    rows = (await client.get(SESSIONS)).json()["sessions"]

    assert len(rows) == 2
    assert sorted(row["current"] for row in rows) == [False, True]
    current = next(row for row in rows if row["current"])
    assert current["user_agent"] == "Panel/1.0" and current["ip"]
    assert {"id", "created_at", "last_used_at", "expires_at", "absolute_expires_at", "ip", "user_agent", "current"} == set(current)
    from_script = (await admin_client.get(SESSIONS)).json()["sessions"]
    assert len(from_script) == 2 and not any(row["current"] for row in from_script)


async def test_one_session_can_be_revoked_and_it_stops_at_once(client, admin_client, live):
    stranger = admin_client.__class__(transport=client._transport, base_url="http://testserver")
    await sign_in(stranger, admin_client)
    await sign_in(client, admin_client)
    rows = (await client.get(SESSIONS)).json()["sessions"]
    other = next(row for row in rows if not row["current"])

    assert (await client.delete(f"{SESSIONS}/{other['id']}", headers=PANEL)).status_code == 200

    assert (await stranger.get(TEMPLATES)).status_code == 401
    assert (await client.get(TEMPLATES)).status_code == 200
    assert (await client.delete(f"{SESSIONS}/{other['id']}", headers=PANEL)).status_code == 404  # already gone
    assert (await client.delete(f"{SESSIONS}/999999", headers=PANEL)).status_code == 404


async def test_revoking_the_session_one_is_using_signs_out(client, admin_client, live):
    await sign_in(client, admin_client)
    mine = (await client.get(SESSIONS)).json()["sessions"][0]

    response = await client.delete(f"{SESSIONS}/{mine['id']}", headers=PANEL)

    assert response.status_code == 200 and "max-age=0" in response.headers["set-cookie"].lower()
    assert (await client.get(TEMPLATES)).status_code == 401


async def test_revoke_others_keeps_the_one_asking(client, admin_client, live):
    stranger = admin_client.__class__(transport=client._transport, base_url="http://testserver")
    await sign_in(stranger, admin_client)
    await sign_in(client, admin_client)

    response = await client.post(f"{SESSIONS}/revoke-others", headers=PANEL)

    assert response.json() == {"revoked": 1}
    assert (await stranger.get(TEMPLATES)).status_code == 401
    assert (await client.get(TEMPLATES)).status_code == 200


async def test_a_script_that_revokes_others_ends_every_session(client, admin_client, live):
    await sign_in(client, admin_client)

    response = await admin_client.post(f"{SESSIONS}/revoke-others")

    assert response.json() == {"revoked": 1}
    assert (await client.get(TEMPLATES)).status_code == 401


async def test_the_cookie_does_not_reach_the_routes_outside_the_admin_path(client, admin_client, live):
    await sign_in(client, admin_client)

    # /api/repos/delete takes the header token or a user; the admin cookie is not sent there
    response = await client.request("DELETE", "/api/repos/delete", json={"type": "model", "name": "nobody/nothing"})

    assert response.status_code == 401


async def test_the_three_repository_actions_have_admin_scoped_routes_that_take_the_cookie(client, admin_client, owner_client, live):
    created = await owner_client.post(
        "/api/repos/create", json={"type": "dataset", "name": "by-the-panel", "organization": "acme-labs"}
    )
    assert created.status_code == 200
    await sign_in(client, admin_client)

    moved = await client.post(
        "/admin/api/repos/move",
        json={"fromRepo": "acme-labs/by-the-panel", "toRepo": "acme-labs/moved-by-the-panel", "type": "dataset"},
        headers=PANEL,
    )
    assert moved.status_code == 200, moved.text
    gone = await client.request(
        "DELETE",
        "/admin/api/repos/delete",
        json={"type": "dataset", "name": "moved-by-the-panel", "organization": "acme-labs"},
        headers=PANEL,
    )
    assert gone.status_code == 200 and "deleted" in gone.json()["message"].lower()
    missing = await client.post("/admin/api/repos/squash", json={"repo": "acme-labs/not-there", "type": "model"}, headers=PANEL)
    assert missing.status_code in (404, 403)  # the same answer the header route gives for a repository that is not there


async def test_the_repository_actions_refuse_a_cookie_request_without_the_panels_header(client, admin_client, live):
    await sign_in(client, admin_client)

    assert (await client.post("/admin/api/repos/move", json={"fromRepo": "a/b", "toRepo": "a/c"})).status_code == 403
    assert (await client.request("DELETE", "/admin/api/repos/delete", json={"type": "model", "name": "x"})).status_code == 403
    assert (await client.post("/admin/api/repos/squash", json={"repo": "a/b"})).status_code == 403


async def test_the_repository_actions_still_take_the_header_token_and_nothing_else(admin_client, client, live):
    assert (await client.post("/admin/api/repos/move", json={"fromRepo": "a/b", "toRepo": "a/c"})).status_code == 401
    response = await admin_client.post("/admin/api/repos/move", json={"fromRepo": "a/b", "toRepo": "a/c", "type": "model"})
    assert response.status_code in (404, 400)


async def test_the_worker_registers_the_task_that_frees_dead_sessions():
    import importlib

    importlib.import_module("kohakuhub.worker")  # importing it registers every task
    from kohakuhub import tasks

    spec = tasks._registry["admin.expire_sessions"]
    assert spec.every == timedelta(hours=1)
