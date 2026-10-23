"""Admin sessions: what the server keeps, when one stops counting, and what is checked before a cookie is believed.

Real SQL on SQLite and PostgreSQL. A session id is a random secret the browser holds; the table keeps only its hash, the
time it was made and last used, the two ends of its life (idle and absolute) and a fingerprint of the shared admin token
it was made with, so changing that token ends every session.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kohakuhub.auth import admin_session as sessions
from kohakuhub.config import cfg
from kohakuhub.db import AdminSession

T0 = datetime(2026, 10, 23, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(cfg.admin, "secret_token", "a-long-random-token-for-the-tests-0123456789")
    monkeypatch.setattr(cfg.admin, "session_idle_minutes", 30)
    monkeypatch.setattr(cfg.admin, "session_absolute_hours", 12)
    monkeypatch.setattr(cfg.admin, "auth_trusted_proxy_hops", 0)
    monkeypatch.setattr(cfg.app, "base_url", "http://localhost:48888")
    return cfg.admin


def request(ip="203.0.113.7", agent="Mozilla/5.0 test", forwarded=None, headers=None):
    lines = {"user-agent": [agent]}
    if forwarded:
        lines["x-forwarded-for"] = forwarded
    for key, value in (headers or {}).items():
        lines[key.lower()] = [value]

    class Headers:
        def getlist(self, name):
            return lines.get(name.lower(), [])

        def get(self, name, default=None):
            values = lines.get(name.lower())
            return values[0] if values else default

    return SimpleNamespace(client=SimpleNamespace(host=ip), headers=Headers(), method="POST", state=SimpleNamespace())


def test_the_session_id_is_random_and_only_its_hash_is_kept(db_dual, admin):
    first, row = sessions.create(request(), now=T0)
    second, _ = sessions.create(request(), now=T0)

    assert first != second and len(first) >= 43  # 256 bits, url-safe
    assert row.token_hash == sessions.hash_session_id(first) != first
    assert len(row.token_hash) == 64
    assert not AdminSession.select().where(AdminSession.token_hash == first).exists()


def test_a_new_session_records_when_where_and_with_what(db_dual, admin):
    _, row = sessions.create(request(ip="198.51.100.9", agent="x" * 300), now=T0)

    assert row.created_at.replace(tzinfo=timezone.utc) == T0
    assert row.expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(minutes=30)
    assert row.absolute_expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=12)
    assert row.ip == "198.51.100.9" and row.user_agent == "x" * 200
    assert row.principal_type == "token" and row.user_id is None and row.revoked_at is None
    assert row.secret_fingerprint == sessions.fingerprint(admin.secret_token)


def test_the_client_address_follows_the_trusted_proxy_setting(db_dual, admin, monkeypatch):
    forwarded = request(ip="172.18.0.2", forwarded=["198.51.100.20, 172.18.0.2"])
    assert sessions.create(forwarded, now=T0)[1].ip == "172.18.0.2"  # not believed with hops 0

    monkeypatch.setattr(admin, "auth_trusted_proxy_hops", 2)
    assert sessions.create(forwarded, now=T0)[1].ip == "198.51.100.20"


def test_the_fingerprint_depends_on_the_token_and_on_a_server_secret(admin, monkeypatch):
    one = sessions.fingerprint("token-one")
    assert one == sessions.fingerprint("token-one") and one != sessions.fingerprint("token-two") and len(one) == 64
    monkeypatch.setattr(cfg.auth, "session_secret", "another-server-secret")
    assert sessions.fingerprint("token-one") != one


def test_a_session_authenticates_until_it_is_idle_for_too_long(db_dual, admin):
    sid, _ = sessions.create(request(), now=T0)

    assert sessions.authenticate(sid, now=T0 + timedelta(minutes=29)) is not None
    assert sessions.authenticate(sid, now=T0 + timedelta(minutes=29) + timedelta(minutes=31)) is None


def test_using_a_session_moves_its_idle_end_forward_but_never_past_the_absolute_end(db_dual, admin):
    sid, _ = sessions.create(request(), now=T0)

    row = sessions.authenticate(sid, now=T0 + timedelta(minutes=20))
    assert row.expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(minutes=50)
    # a long working day with a click every 25 minutes: the idle end keeps moving, then stays at the cap
    for minutes in range(45, 11 * 60 + 51, 25):
        row = sessions.authenticate(sid, now=T0 + timedelta(minutes=minutes))
        assert row is not None
    assert row.expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=12)
    assert sessions.authenticate(sid, now=T0 + timedelta(hours=12, seconds=1)) is None


def test_the_last_use_is_written_at_most_once_a_minute(db_dual, admin):
    sid, _ = sessions.create(request(), now=T0)

    sessions.authenticate(sid, now=T0 + timedelta(seconds=20))
    stored = AdminSession.get().last_used_at.replace(tzinfo=timezone.utc)
    assert stored == T0  # nothing written: less than a minute since the last write

    sessions.authenticate(sid, now=T0 + timedelta(seconds=61))
    stored = AdminSession.get().last_used_at.replace(tzinfo=timezone.utc)
    assert stored == T0 + timedelta(seconds=61)


def test_an_unknown_empty_or_malformed_id_is_nothing(db_dual, admin):
    assert sessions.authenticate("", now=T0) is None
    assert sessions.authenticate(None, now=T0) is None
    assert sessions.authenticate("not-a-session", now=T0) is None


def test_a_revoked_session_is_nothing_and_stays_a_row_for_the_list(db_dual, admin):
    sid, row = sessions.create(request(), now=T0)

    sessions.revoke(row, now=T0 + timedelta(minutes=1))

    assert sessions.authenticate(sid, now=T0 + timedelta(minutes=2)) is None
    assert AdminSession.get().revoked_at is not None


def test_changing_the_shared_token_ends_every_session(db_dual, admin, monkeypatch):
    sid, _ = sessions.create(request(), now=T0)
    other, _ = sessions.create(request(), now=T0)
    assert sessions.authenticate(sid, now=T0 + timedelta(minutes=1)) is not None

    monkeypatch.setattr(admin, "secret_token", "the-token-was-rotated-0123456789abcdefgh")

    assert sessions.authenticate(sid, now=T0 + timedelta(minutes=2)) is None
    assert sessions.authenticate(other, now=T0 + timedelta(minutes=2)) is None
    assert AdminSession.select().where(AdminSession.revoked_at.is_null()).count() == 0  # revoked, not just refused


def test_the_active_sessions_are_the_ones_not_revoked_and_not_expired(db_dual, admin):
    _, live = sessions.create(request(ip="198.51.100.1"), now=T0)
    _, revoked = sessions.create(request(ip="198.51.100.2"), now=T0)
    _, idle = sessions.create(request(ip="198.51.100.3"), now=T0 - timedelta(hours=2))
    sessions.revoke(revoked, now=T0)

    assert [row.ip for row in sessions.active_sessions(now=T0 + timedelta(minutes=1))] == ["198.51.100.1"]


def test_revoke_others_keeps_one_and_says_how_many_went(db_dual, admin):
    _, keep = sessions.create(request(), now=T0)
    sessions.create(request(), now=T0)
    sessions.create(request(), now=T0)

    assert sessions.revoke_others(keep.id, now=T0 + timedelta(minutes=1)) == 2
    assert [row.id for row in sessions.active_sessions(now=T0 + timedelta(minutes=2))] == [keep.id]
    assert sessions.revoke_others(keep.id, now=T0 + timedelta(minutes=3)) == 0


def test_the_cookie_lives_as_long_as_the_session_can_and_says_how_it_may_travel(db_dual, admin, monkeypatch):
    from fastapi import Response

    monkeypatch.setattr(admin, "session_cookie_secure", True)
    response = Response()
    sid, row = sessions.create(request(), now=T0)
    sessions.set_cookie(response, sid, row.absolute_expires_at, now=T0)

    header = response.headers["set-cookie"].lower()
    assert f"admin_session={sid}".lower() in header
    assert "path=/admin" in header and "httponly" in header and "samesite=strict" in header and "secure" in header
    assert f"max-age={12 * 3600}" in header

    monkeypatch.setattr(admin, "session_cookie_secure", False)
    plain = Response()
    sessions.set_cookie(plain, sid, row.absolute_expires_at, now=T0)
    assert "secure" not in plain.headers["set-cookie"].lower()

    gone = Response()
    sessions.clear_cookie(gone)
    assert "admin_session=" in gone.headers["set-cookie"] and "max-age=0" in gone.headers["set-cookie"].lower()


def test_the_cookie_is_secure_by_default_exactly_when_the_public_address_is_https(admin, monkeypatch):
    monkeypatch.setattr(admin, "session_cookie_secure", None)
    monkeypatch.setattr(cfg.app, "base_url", "https://hub.example.test")
    assert sessions.cookie_secure() is True
    monkeypatch.setattr(cfg.app, "base_url", "http://localhost:48888")
    assert sessions.cookie_secure() is False
    monkeypatch.setattr(admin, "session_cookie_secure", True)
    assert sessions.cookie_secure() is True


def test_the_allowed_origins_are_the_public_address_and_the_configured_ones(admin, monkeypatch):
    monkeypatch.setattr(cfg.app, "base_url", "https://hub.example.test/some/prefix/")
    monkeypatch.setattr(admin, "session_allowed_origins", ["https://admin.example.test/", "bad value", ""])

    assert sessions.allowed_origins() == {"https://hub.example.test", "https://admin.example.test"}


@pytest.mark.parametrize(
    "method,headers,problem",
    [
        ("GET", {}, None),  # a read needs nothing more than the cookie
        ("HEAD", {}, None),
        ("POST", {"X-Requested-With": "cheesecave-admin", "Origin": "http://localhost:48888"}, None),
        ("DELETE", {"X-Requested-With": "cheesecave-admin", "Origin": "http://localhost:48888"}, None),
        ("POST", {"Origin": "http://localhost:48888"}, "X-Requested-With"),
        ("POST", {"X-Requested-With": "XMLHttpRequest", "Origin": "http://localhost:48888"}, "X-Requested-With"),
        ("POST", {"X-Requested-With": "cheesecave-admin"}, "Origin"),
        ("PUT", {"X-Requested-With": "cheesecave-admin", "Origin": "https://evil.example"}, "Origin"),
        ("POST", {"X-Requested-With": "cheesecave-admin", "Origin": "null"}, "Origin"),
    ],
)
def test_a_cookie_request_that_changes_something_needs_the_header_and_the_right_origin(admin, method, headers, problem):
    req = request(headers=headers)
    req.method = method

    reason = sessions.csrf_problem(req)

    if problem is None:
        assert reason is None
    else:
        assert reason and problem in reason


def test_the_idle_and_absolute_limits_come_from_the_settings(db_dual, admin, monkeypatch):
    monkeypatch.setattr(admin, "session_idle_minutes", 5)
    monkeypatch.setattr(admin, "session_absolute_hours", 1)

    _, row = sessions.create(request(), now=T0)

    assert row.expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(minutes=5)
    assert row.absolute_expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=1)
    # an idle window longer than the cap is cut at the cap
    monkeypatch.setattr(admin, "session_idle_minutes", 600)
    _, capped = sessions.create(request(), now=T0)
    assert capped.expires_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=1)


def test_the_cleanup_deletes_what_can_no_longer_be_used_and_keeps_the_rest(db_dual, admin):
    from kohakuhub import admin_session_cleanup as cleanup

    now = datetime.now(timezone.utc)
    _, fresh = sessions.create(request(ip="198.51.100.1"), now=now)
    _, expired = sessions.create(request(ip="198.51.100.2"), now=now - timedelta(days=3))
    _, revoked_long_ago = sessions.create(request(ip="198.51.100.3"), now=now)  # still inside its life
    sessions.revoke(revoked_long_ago, now=now - timedelta(days=9))
    _, revoked_today = sessions.create(request(ip="198.51.100.4"), now=now)
    sessions.revoke(revoked_today, now=now)

    assert cleanup.delete_unusable(now=now) == 2

    assert sorted(row.ip for row in AdminSession.select()) == ["198.51.100.1", "198.51.100.4"]


async def test_the_periodic_task_frees_the_dead_sessions_and_leaves_the_live_ones(db_dual, admin):
    from kohakuhub import admin_session_cleanup as cleanup

    now = datetime.now(timezone.utc)
    sessions.create(request(ip="198.51.100.1"), now=now)
    sessions.create(request(ip="198.51.100.2"), now=now - timedelta(days=2))

    await cleanup.expire_admin_sessions({})
    assert [row.ip for row in AdminSession.select()] == ["198.51.100.1"]

    await cleanup.expire_admin_sessions({})  # nothing left to free: still fine
    assert AdminSession.select().count() == 1
