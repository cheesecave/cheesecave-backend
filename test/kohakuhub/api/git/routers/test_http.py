"""Tests for Git Smart HTTP routes, on real user, token and repository rows."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.api.git.routers.http as git_http
from kohakuhub.auth.utils import hash_token
from kohakuhub.db import Token
from test.kohakuhub.support.factories import make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self) -> bytes:
        return self._body


def _basic(username: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()


def test_get_user_from_git_auth_handles_missing_invalid_and_active_users():
    user = make_user("owner")
    token = Token.create(user=user, token_hash=hash_token("secret"), name="git")

    assert git_http.get_user_from_git_auth(None) is None
    assert git_http.get_user_from_git_auth(_basic("owner", "wrong")) is None
    assert git_http.get_user_from_git_auth(_basic("owner", "secret")) == user
    assert Token.get_by_id(token.id).last_used is not None

    user.is_active = False
    user.save()
    assert git_http.get_user_from_git_auth(_basic("owner", "secret")) is None


@pytest.mark.asyncio
async def test_git_info_refs_handles_upload_and_receive_services(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo", repo_type="dataset")
    user = owner
    seen = {"handlers": []}

    monkeypatch.setattr(git_http, "get_user_from_git_auth", lambda authorization: user)
    monkeypatch.setattr(git_http, "check_repo_read_permission", lambda repo_arg, user_arg: seen.setdefault("read", []).append((repo_arg, user_arg)))
    monkeypatch.setattr(git_http, "check_repo_write_permission", lambda repo_arg, user_arg: seen.setdefault("write", []).append((repo_arg, user_arg)))

    class FakeBridge:
        def __init__(self, repo_type, namespace, name, lakefs_repo=None):
            # The route resolves the LakeFS id from the Repository row and hands
            # it over, instead of letting the bridge re-derive it.
            seen["bridge_args"] = (repo_type, namespace, name)
            seen["bridge_lakefs_repo"] = lakefs_repo

        async def get_refs(self, branch="main"):
            seen["branch"] = branch
            return {"HEAD": "1" * 40}

    class FakeUploadHandler:
        def __init__(self, repo_id):
            seen["handlers"].append(("upload", repo_id))

        def get_service_info(self, refs):
            seen["upload_refs"] = refs
            return b"upload-info"

    class FakeReceiveHandler:
        def __init__(self, repo_id):
            seen["handlers"].append(("receive", repo_id))

        def get_service_info(self, refs):
            seen["receive_refs"] = refs
            return b"receive-info"

    monkeypatch.setattr(git_http, "GitLakeFSBridge", FakeBridge)
    monkeypatch.setattr(git_http, "GitUploadPackHandler", FakeUploadHandler)
    monkeypatch.setattr(git_http, "GitReceivePackHandler", FakeReceiveHandler)

    upload_response = await git_http.git_info_refs("owner", "repo", "git-upload-pack", authorization="Basic x")
    receive_response = await git_http.git_info_refs("owner", "repo", "git-receive-pack", authorization="Basic x")

    assert upload_response.body == b"upload-info"
    assert upload_response.media_type == "application/x-git-upload-pack-advertisement"
    assert receive_response.body == b"receive-info"
    assert seen["bridge_args"] == ("dataset", "owner", "repo")
    assert seen["bridge_lakefs_repo"], (
        "the route must pass the row's resolved LakeFS id to the bridge"
    )
    assert seen["read"] == [(repo, user)]
    assert seen["write"] == [(repo, user)]


@pytest.mark.asyncio
async def test_git_info_refs_rejects_missing_repo_unknown_service_and_unauthenticated_push(monkeypatch):
    # no repository row yet: the lookup itself answers 404
    with pytest.raises(HTTPException) as not_found:
        await git_http.git_info_refs("owner", "repo", "git-upload-pack")

    assert not_found.value.status_code == 404

    repo = make_repo(make_user("owner"), "repo")
    monkeypatch.setattr(git_http, "get_user_from_git_auth", lambda authorization: None)
    monkeypatch.setattr(git_http, "check_repo_read_permission", lambda repo_arg, user_arg: None)

    with pytest.raises(HTTPException) as unknown_service:
        await git_http.git_info_refs("owner", "repo", "git-bad-service")

    assert unknown_service.value.status_code == 400

    with pytest.raises(HTTPException) as missing_auth:
        await git_http.git_info_refs("owner", "repo", "git-receive-pack")

    assert missing_auth.value.status_code == 401


@pytest.mark.asyncio
async def test_git_upload_pack_and_head_use_expected_handlers(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo", repo_type="space")
    user = owner
    seen = {}

    monkeypatch.setattr(git_http, "get_user_from_git_auth", lambda authorization: user)
    monkeypatch.setattr(git_http, "check_repo_read_permission", lambda repo_arg, user_arg: seen.setdefault("read", []).append((repo_arg, user_arg)))
    monkeypatch.setattr(git_http, "check_repo_write_permission", lambda repo_arg, user_arg: seen.setdefault("write", []).append((repo_arg, user_arg)))

    class FakeBridge:
        def __init__(self, repo_type, namespace, name, lakefs_repo=None):
            # The route resolves the LakeFS id from the Repository row and hands
            # it over, instead of letting the bridge re-derive it.
            seen["bridge_args"] = (repo_type, namespace, name)
            seen["bridge_lakefs_repo"] = lakefs_repo

    class FakeUploadHandler:
        def __init__(self, repo_id, bridge):
            seen["upload_handler"] = (repo_id, bridge.__class__.__name__)

        async def handle_upload_pack(self, request_body):
            seen["upload_body"] = request_body
            return b"upload-pack-result"

    monkeypatch.setattr(git_http, "GitLakeFSBridge", FakeBridge)
    monkeypatch.setattr(git_http, "GitUploadPackHandler", FakeUploadHandler)

    upload_response = await git_http.git_upload_pack(
        "owner",
        "repo",
        request=_FakeRequest(b"want main"),
        authorization="Basic x",
    )
    head_response = await git_http.git_head("owner", "repo", authorization="Basic x")

    assert upload_response.body == b"upload-pack-result"
    assert head_response.body == b"ref: refs/heads/main\n"
    assert seen["upload_body"] == b"want main"
    assert seen["bridge_args"] == ("space", "owner", "repo")
    assert seen["bridge_lakefs_repo"], (
        "the route must pass the row's resolved LakeFS id to the bridge"
    )


@pytest.mark.asyncio
async def test_git_receive_pack_requires_authentication(monkeypatch):
    make_repo(make_user("owner"), "repo")
    monkeypatch.setattr(git_http, "get_user_from_git_auth", lambda authorization: None)

    with pytest.raises(HTTPException) as exc_info:
        await git_http.git_receive_pack("owner", "repo", request=_FakeRequest(b"data"))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_git_receive_pack_is_refused_without_reading_the_body(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo")
    user = owner
    checked = []
    monkeypatch.setattr(git_http, "get_user_from_git_auth", lambda authorization: user)
    monkeypatch.setattr(
        git_http,
        "check_repo_write_permission",
        lambda repo_arg, user_arg: checked.append((repo_arg, user_arg)),
    )

    class BodyMustNotBeRead:
        async def body(self):
            raise AssertionError("the pack must not be read")

    class HandlerMustNotBeUsed:
        def __init__(self, repo_id):
            raise AssertionError("a push must not reach the receive-pack handler")

    monkeypatch.setattr(git_http, "GitReceivePackHandler", HandlerMustNotBeUsed)

    with pytest.raises(HTTPException) as exc_info:
        await git_http.git_receive_pack(
            "owner", "repo", request=BodyMustNotBeRead(), authorization="Basic x"
        )

    assert exc_info.value.status_code == 501
    assert checked == [(repo, user)], "permission is still checked first"


@pytest.mark.asyncio
async def test_git_receive_pack_still_404s_for_a_missing_repo_and_403s_for_a_reader(
    monkeypatch,
):
    with pytest.raises(HTTPException) as missing:
        await git_http.git_receive_pack("owner", "nope", request=_FakeRequest(b""))
    assert missing.value.status_code == 404

    make_repo(make_user("owner"), "repo")
    monkeypatch.setattr(
        git_http, "get_user_from_git_auth", lambda authorization: SimpleNamespace(username="x")
    )

    def deny(repo_arg, user_arg):
        raise HTTPException(403, detail="no")

    monkeypatch.setattr(git_http, "check_repo_write_permission", deny)
    with pytest.raises(HTTPException) as denied:
        await git_http.git_receive_pack(
            "owner", "repo", request=_FakeRequest(b""), authorization="Basic x"
        )
    assert denied.value.status_code == 403
