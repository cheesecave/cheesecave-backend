"""A login session slides: use renews it, up to an absolute limit, and the cookie follows.

Real session rows on a real database; the cookie and the renewal are also driven over
HTTP through the ASGI app, since the cookie is written by a middleware.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Response

import kohakuhub.auth.dependencies as auth_deps
import kohakuhub.auth.session as sessions
from kohakuhub import session_cleanup
from kohakuhub.config import cfg
from kohakuhub.db import Session
from test.kohakuhub.support.factories import make_session, make_user

IDLE = timedelta(days=30)
LONG = timedelta(days=365)


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(cfg.auth, "session_expire_hours", 720)
    monkeypatch.setattr(cfg.auth, "session_absolute_days", 365)


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _request():
    return SimpleNamespace(state=SimpleNamespace())


def test_defaults_are_thirty_days_idle_and_a_year_absolute():
    from kohakuhub.config import AuthConfig

    assert AuthConfig().session_expire_hours == 720
    assert AuthConfig().session_absolute_days == 365


def test_next_expiry_is_the_idle_window_from_now():
    now = _now()
    assert sessions.next_expiry(now, now) == now + IDLE


def test_next_expiry_never_passes_the_absolute_limit():
    now = _now()
    created = now - timedelta(days=350)
    assert sessions.next_expiry(created, now) == created + LONG


def test_next_expiry_reads_a_naive_created_at_as_utc():
    now = _now()
    created = (now - timedelta(days=350)).replace(tzinfo=None)
    assert sessions.next_expiry(created, now) == _utc(created) + LONG


def test_a_zero_absolute_limit_means_no_limit(monkeypatch):
    monkeypatch.setattr(cfg.auth, "session_absolute_days", 0)
    now = _now()
    assert sessions.next_expiry(now - timedelta(days=900), now) == now + IDLE


def test_cookie_lives_as_long_as_what_is_left():
    response = Response()
    now = _now()
    sessions.set_session_cookie(response, "abc", now + timedelta(days=2), now)
    cookie = response.headers["set-cookie"]
    assert "session_id=abc" in cookie
    assert f"Max-Age={2 * 86400}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_cookie_of_a_past_expiry_does_not_go_negative():
    response = Response()
    now = _now()
    sessions.set_session_cookie(response, "abc", now - timedelta(days=1), now)
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.usefixtures("db_scope")
def test_a_session_from_the_old_fixed_window_is_renewed_on_use():
    now = _now()
    row = make_session(make_user("alice"), "s1", expires_at=now + timedelta(days=3))
    request = _request()

    sessions.renew_session(request, row, now)

    stored = _utc(Session.get_by_id(row.id).expires_at)
    assert abs(stored - (now + IDLE)) < timedelta(seconds=1)
    assert request.state.session_renewal[0] == "s1"
    assert request.state.session_renewal[1] == now + IDLE


@pytest.mark.usefixtures("db_scope")
def test_a_session_renewed_a_moment_ago_is_not_written_again():
    now = _now()
    row = make_session(make_user("alice"), "s1", expires_at=now + IDLE - timedelta(minutes=10))
    before = Session.get_by_id(row.id).expires_at
    request = _request()

    sessions.renew_session(request, row, now)

    assert Session.get_by_id(row.id).expires_at == before
    assert not hasattr(request.state, "session_renewal")


@pytest.mark.usefixtures("db_scope")
def test_renewal_stops_at_the_absolute_limit():
    now = _now()
    created = now - timedelta(days=364)
    row = make_session(
        make_user("alice"), "s1", expires_at=now + timedelta(hours=2), created_at=created
    )
    request = _request()

    sessions.renew_session(request, row, now)

    assert request.state.session_renewal[1] == created + LONG
    assert abs(_utc(Session.get_by_id(row.id).expires_at) - (created + LONG)) < timedelta(seconds=1)


@pytest.mark.usefixtures("db_scope")
def test_a_session_at_its_absolute_limit_is_not_extended():
    now = _now()
    created = now - timedelta(days=364)
    row = make_session(
        make_user("alice"), "s1", expires_at=created + LONG, created_at=created
    )
    request = _request()

    sessions.renew_session(request, row, now)

    assert not hasattr(request.state, "session_renewal")


@pytest.mark.usefixtures("db_scope")
def test_a_shortened_window_never_cuts_a_session_short(monkeypatch):
    monkeypatch.setattr(cfg.auth, "session_expire_hours", 1)
    now = _now()
    row = make_session(make_user("alice"), "s1", expires_at=now + timedelta(days=20))
    before = Session.get_by_id(row.id).expires_at
    request = _request()

    sessions.renew_session(request, row, now)

    assert Session.get_by_id(row.id).expires_at == before
    assert not hasattr(request.state, "session_renewal")


@pytest.mark.usefixtures("db_scope")
def test_a_session_logged_out_meanwhile_is_not_brought_back():
    now = _now()
    row = make_session(make_user("alice"), "s1", expires_at=now + timedelta(days=3))
    Session.delete().where(Session.id == row.id).execute()
    request = _request()

    sessions.renew_session(request, row, now)

    assert Session.get_or_none(Session.id == row.id) is None
    assert not hasattr(request.state, "session_renewal")


@pytest.mark.usefixtures("db_scope")
def test_using_a_session_through_the_dependency_renews_it():
    row = make_session(make_user("alice"), "s1", expires_at=_now() + timedelta(days=3))
    request = _request()

    user = auth_deps.get_current_user(request, session_id="s1", authorization=None)

    assert user.username == "alice"
    assert _utc(Session.get_by_id(row.id).expires_at) > _now() + timedelta(days=29)
    assert request.state.session_renewal[0] == "s1"


@pytest.mark.usefixtures("db_scope")
def test_an_expired_session_is_not_renewed():
    row = make_session(make_user("alice"), "s1", expires_at=_now() - timedelta(minutes=1))
    before = Session.get_by_id(row.id).expires_at
    request = _request()

    with pytest.raises(Exception) as exc:
        auth_deps.get_current_user(request, session_id="s1", authorization=None)

    assert exc.value.status_code == 401
    assert Session.get_by_id(row.id).expires_at == before


@pytest.mark.usefixtures("db_scope")
def test_the_inactive_users_session_is_not_renewed():
    row = make_session(
        make_user("alice", is_active=False), "s1", expires_at=_now() + timedelta(days=3)
    )
    before = Session.get_by_id(row.id).expires_at
    request = _request()

    with pytest.raises(Exception):
        auth_deps.get_current_user(request, session_id="s1", authorization=None)

    assert Session.get_by_id(row.id).expires_at == before


# --- cleanup ------------------------------------------------------------------------


@pytest.mark.usefixtures("db_scope")
async def test_the_cleanup_removes_only_expired_sessions():
    alice = make_user("alice")
    make_session(alice, "old", expires_at=_now() - timedelta(days=2))
    make_session(alice, "gone", expires_at=_now() - timedelta(seconds=5))
    make_session(alice, "live", expires_at=_now() + timedelta(days=2))

    await session_cleanup.expire_sessions({})

    assert {s.session_id for s in Session.select()} == {"live"}


@pytest.mark.usefixtures("db_scope")
async def test_the_cleanup_with_nothing_expired_changes_nothing():
    make_session(make_user("alice"), "live", expires_at=_now() + timedelta(days=2))

    await session_cleanup.expire_sessions({})

    assert Session.select().count() == 1


def test_the_cleanup_is_an_hourly_periodic_task():
    from kohakuhub import tasks

    spec = tasks.get_spec(session_cleanup.EXPIRE_KIND)
    assert spec.every == timedelta(hours=1)
