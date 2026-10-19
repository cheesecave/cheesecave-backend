"""Unit tests for authentication dependencies, on real session, token and user rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.auth.admin_token as admin_token
from kohakuhub.auth.admin_throttle import AdminAuthThrottle
import kohakuhub.auth.dependencies as auth_deps
from kohakuhub.auth.utils import hash_token
from kohakuhub.db import Token
from test.kohakuhub.support.factories import make_session, make_token, make_user


@pytest.mark.usefixtures("db_scope")
def test_get_current_user_rejects_inactive_session_and_token(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace())
    alice = make_user("alice", is_active=False)
    make_session(alice, "session-1")
    token = make_token(alice, hash_token("plain-token"))
    monkeypatch.setattr(
        auth_deps,
        "parse_auth_header",
        lambda authorization: ("plain-token", {"https://hf.local": "hf_token"}),
    )

    with pytest.raises(HTTPException) as exc:
        auth_deps.get_current_user(request, session_id="session-1", authorization="Bearer plain-token")

    assert exc.value.status_code == 401
    assert request.state.external_tokens == {"https://hf.local": "hf_token"}
    # The token lookup ran its update: last_used is now set on the real row.
    assert Token.get_by_id(token.id).last_used is not None


@pytest.mark.usefixtures("db_scope")
def test_get_current_user_handles_missing_session_and_invalid_token(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(
        auth_deps,
        "parse_auth_header",
        lambda authorization: ("plain-token", {}),
    )

    with pytest.raises(HTTPException) as exc:
        auth_deps.get_current_user(request, session_id="missing-session", authorization="Bearer plain-token")

    assert exc.value.status_code == 401


@pytest.mark.usefixtures("db_scope")
def test_get_current_user_accepts_active_token_user(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace())
    make_token(make_user("token-user"), hash_token("plain-token"))
    monkeypatch.setattr(
        auth_deps,
        "parse_auth_header",
        lambda authorization: ("plain-token", {}),
    )

    user = auth_deps.get_current_user(request, session_id=None, authorization="Bearer plain-token")

    assert user.username == "token-user"


def test_get_optional_user_and_get_external_tokens_cover_fallback_state(monkeypatch):
    request = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(
        auth_deps,
        "get_current_user",
        lambda request, session_id=None, authorization=None: (_ for _ in ()).throw(
            HTTPException(status_code=401, detail="missing")
        ),
    )
    monkeypatch.setattr(
        auth_deps,
        "parse_auth_header",
        lambda authorization: (None, {"https://mirror.local": "mirror-token"}),
    )

    assert auth_deps.get_optional_user(request, authorization="Bearer ignored") is None
    assert request.state.external_tokens == {"https://mirror.local": "mirror-token"}
    assert auth_deps.get_external_tokens(SimpleNamespace(state=SimpleNamespace())) == {}


def test_get_current_user_or_admin_covers_admin_user_and_failure_paths(monkeypatch):
    monkeypatch.setattr(admin_token.cfg.admin, "enabled", True)
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", "expected-secret")
    monkeypatch.setattr(admin_token.cfg.admin, "allow_weak_token", True)

    admin_request = SimpleNamespace(state=SimpleNamespace())
    assert auth_deps.get_current_user_or_admin(admin_request, x_admin_token="expected-secret") == (None, True)
    assert admin_request.state.is_admin is True

    user = SimpleNamespace(username="alice")
    user_request = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(auth_deps, "get_current_user", lambda *args, **kwargs: user)
    assert auth_deps.get_current_user_or_admin(user_request, x_admin_token=None) == (user, False)
    assert user_request.state.is_admin is False

    monkeypatch.setattr(
        auth_deps,
        "get_current_user",
        lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(status_code=401, detail="missing")),
    )
    with pytest.raises(HTTPException) as exc:
        auth_deps.get_current_user_or_admin(
            SimpleNamespace(state=SimpleNamespace()), x_admin_token=None
        )
    assert exc.value.status_code == 401


class _Headers:
    def getlist(self, name):
        return []


def _admin_request(peer="198.51.100.20"):
    return SimpleNamespace(
        state=SimpleNamespace(), client=SimpleNamespace(host=peer), headers=_Headers()
    )


@pytest.fixture
def admin_enabled(monkeypatch):
    long_token = "k" * 40
    monkeypatch.setattr(admin_token.cfg.admin, "enabled", True)
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", long_token)
    monkeypatch.setattr(admin_token.cfg.admin, "allow_weak_token", False)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_throttle_enabled", True)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_free_failures", 2)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_trusted_proxy_hops", 0)
    monkeypatch.setattr(admin_token.cfg.admin, "auth_allowlist", [])
    monkeypatch.setattr(admin_token, "throttle", AdminAuthThrottle())  # in memory: no database in these tests
    yield long_token


def test_a_wrong_admin_token_still_lets_a_logged_in_user_through(monkeypatch, admin_enabled):
    user = SimpleNamespace(username="alice")
    monkeypatch.setattr(auth_deps, "get_current_user", lambda *a, **k: user)
    request = _admin_request()
    assert auth_deps.get_current_user_or_admin(request, x_admin_token="wrong") == (user, False)
    assert admin_token.throttle.failures("198.51.100.20") == 1  # but the wrong token was counted


def test_a_weak_token_is_not_accepted_here_either_but_users_still_work(monkeypatch, admin_enabled):
    monkeypatch.setattr(admin_token.cfg.admin, "secret_token", "short")
    user = SimpleNamespace(username="alice")
    monkeypatch.setattr(auth_deps, "get_current_user", lambda *a, **k: user)
    assert auth_deps.get_current_user_or_admin(_admin_request(), x_admin_token="short") == (user, False)


def test_a_client_that_keeps_failing_waits_and_is_answered_429_even_as_a_user(monkeypatch, admin_enabled):
    user = SimpleNamespace(username="alice")
    monkeypatch.setattr(auth_deps, "get_current_user", lambda *a, **k: user)
    request = _admin_request()
    for _ in range(3):  # two free failures, the third starts a wait
        auth_deps.get_current_user_or_admin(request, x_admin_token="wrong")
    with pytest.raises(HTTPException) as waiting:
        auth_deps.get_current_user_or_admin(request, x_admin_token=admin_enabled)
    assert waiting.value.status_code == 429
    assert waiting.value.headers["Retry-After"] == "1"


def test_both_doors_share_one_count(monkeypatch, admin_enabled):
    import asyncio

    user = SimpleNamespace(username="alice")
    monkeypatch.setattr(auth_deps, "get_current_user", lambda *a, **k: user)
    request = _admin_request()
    auth_deps.get_current_user_or_admin(request, x_admin_token="wrong")
    auth_deps.get_current_user_or_admin(request, x_admin_token="wrong")
    with pytest.raises(HTTPException):  # the third failure comes through the admin routes' door
        asyncio.run(admin_token.verify_admin_token(request, "wrong"))
    with pytest.raises(HTTPException) as waiting:
        auth_deps.get_current_user_or_admin(request, x_admin_token=admin_enabled)
    assert waiting.value.status_code == 429

