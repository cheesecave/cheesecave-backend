"""Who a git or git-lfs request is from, on real user, token and session rows.

``git`` sends HTTP Basic (user name ignored, the token is the password);
``huggingface_hub`` sends Bearer; the web UI sends a session cookie. One
resolver serves all of them, and a credential that does not check out means
"anonymous", never an error.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kohakuhub.api.git.auth import get_git_user
from kohakuhub.auth.utils import hash_token
from kohakuhub.db import Token
from test.kohakuhub.support.factories import make_session, make_token, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


def _basic(username, password):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _request():
    return SimpleNamespace(state=SimpleNamespace())


def _resolve(authorization=None, session_id=None):
    return get_git_user(_request(), session_id=session_id, authorization=authorization)


def _owner_with_token(token="secret"):
    user = make_user("owner")
    row = make_token(user, hash_token(token))
    return user, row


def test_basic_takes_the_password_as_the_token_and_ignores_the_user_name():
    user, row = _owner_with_token()

    assert _resolve(_basic("owner", "secret")) == user
    assert _resolve(_basic("whoever-git-was-told", "secret")) == user  # the name is not checked
    assert Token.get_by_id(row.id).last_used is not None


def test_bearer_is_still_accepted_alone_and_with_external_tokens():
    user, _ = _owner_with_token()

    assert _resolve("Bearer secret") == user
    assert _resolve("Bearer secret|https://huggingface.co,hf_external") == user


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer ",
        "Bearer wrong",
        "Bearer |https://huggingface.co,hf_external",  # an empty main token
        _basic("owner", "wrong"),
        _basic("owner", ""),
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"no-colon-at-all").decode(),
        "Digest abc",
        "Token secret",
    ],
)
def test_anything_that_does_not_check_out_is_anonymous(authorization):
    _owner_with_token()

    assert _resolve(authorization) is None


def test_an_inactive_user_is_anonymous():
    user, _ = _owner_with_token()
    user.is_active = False
    user.save()

    assert _resolve(_basic("owner", "secret")) is None
    assert _resolve("Bearer secret") is None


def test_a_session_cookie_identifies_the_web_ui():
    user = make_user("owner")
    make_session(user, "sess-1", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    assert _resolve(session_id="sess-1") == user
    assert _resolve(session_id="unknown") is None


def test_an_expired_session_is_anonymous():
    user = make_user("owner")
    make_session(user, "old", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

    assert _resolve(session_id="old") is None


def test_a_session_still_counts_when_the_basic_credential_is_wrong():
    user = make_user("owner")
    make_session(user, "sess-2", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    assert _resolve(_basic("owner", "wrong"), session_id="sess-2") == user
