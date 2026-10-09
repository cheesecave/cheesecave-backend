"""Unit tests for authentication dependencies, on real session, token and user rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
    monkeypatch.setattr(auth_deps.cfg.admin, "enabled", True)
    monkeypatch.setattr(auth_deps.cfg.admin, "secret_token", "expected-secret")

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
