"""Over HTTP: login gives a 30-day cookie, and using an old session renews it and its cookie.

Driven through the ASGI app, since the cookie is written by a middleware. Kept apart
from the unit tests: the two kinds of fixtures do not share a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kohakuhub.db import Session

IDLE = timedelta(days=30)


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _max_age(response):
    cookie = response.headers.get("set-cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.lower().startswith("max-age="):
            return int(part.split("=", 1)[1])
    return None


async def test_login_gives_a_thirty_day_cookie_and_session(client):
    response = await client.post(
        "/api/auth/login", json={"username": "owner", "password": "KohakuTest123!"}
    )

    assert response.status_code == 200
    assert _max_age(response) == 30 * 86400
    row = Session.get(Session.session_id == client.cookies["session_id"])
    assert abs(_utc(row.expires_at) - (_now() + IDLE)) < timedelta(minutes=1)


async def test_using_an_old_session_over_http_renews_it_and_its_cookie(client):
    await client.post("/api/auth/login", json={"username": "owner", "password": "KohakuTest123!"})
    sid = client.cookies["session_id"]
    Session.update(expires_at=_now() + timedelta(days=3)).where(Session.session_id == sid).execute()

    response = await client.get("/api/auth/me")

    assert response.status_code == 200
    assert 29 * 86400 < _max_age(response) <= 30 * 86400
    assert f"session_id={sid}" in response.headers["set-cookie"]
    row = Session.get(Session.session_id == sid)
    assert _utc(row.expires_at) > _now() + timedelta(days=29)


async def test_a_session_just_renewed_gets_no_new_cookie(client):
    await client.post("/api/auth/login", json={"username": "owner", "password": "KohakuTest123!"})

    response = await client.get("/api/auth/me")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers


async def test_a_request_without_a_session_gets_no_cookie(client):
    response = await client.get("/api/auth/me")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
