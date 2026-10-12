"""Tests for the Git Smart HTTP route handlers, on real user and repository rows.

Who may do what is pinned end to end in ``test_git_auth_matrix.py`` and
``test_git_clients_e2e.py``; these tests cover how a handler resolves its
repository and wires the LakeFS bridge (an external service, so a fake).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.git.routers.http as git_http
from kohakuhub.api.git.errors import GitProtocolError
from test.kohakuhub.support.factories import make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


class _Request:
    def __init__(self, body: bytes = b""):
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeBridge:
    """The LakeFS side: records what the route handed it."""

    seen: dict = {}

    def __init__(self, repo_type, namespace, name, lakefs_repo=None):
        # The route resolves the LakeFS id from the Repository row and hands it
        # over, instead of letting the bridge re-derive it.
        type(self).seen = {"args": (repo_type, namespace, name), "lakefs_repo": lakefs_repo}

    async def get_refs(self, branch="main"):
        type(self).seen["branch"] = branch
        return {"HEAD": "1" * 40}


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(git_http, "GitLakeFSBridge", _FakeBridge)
    _FakeBridge.seen = {}
    return _FakeBridge


@pytest.mark.asyncio
async def test_info_refs_hands_the_bridge_the_rows_type_and_resolved_lakefs_id(bridge, monkeypatch):
    owner = make_user("owner")
    make_repo(owner, "repo", repo_type="dataset")

    class UploadHandler:
        def __init__(self, repo_id):
            self.repo_id = repo_id

        def get_service_info(self, refs):
            return f"upload:{self.repo_id}:{sorted(refs)}".encode()

    class ReceiveHandler(UploadHandler):
        def get_service_info(self, refs):
            return f"receive:{self.repo_id}".encode()

    monkeypatch.setattr(git_http, "GitUploadPackHandler", UploadHandler)
    monkeypatch.setattr(git_http, "GitReceivePackHandler", ReceiveHandler)

    upload = await git_http.git_info_refs(
        "owner", "repo", repo_type="dataset", service="git-upload-pack", user=owner
    )
    receive = await git_http.git_info_refs(
        "owner", "repo", repo_type="dataset", service="git-receive-pack", user=owner
    )

    assert upload.body == b"upload:owner/repo:['HEAD']"
    assert upload.media_type == "application/x-git-upload-pack-advertisement"
    assert receive.body == b"receive:owner/repo"
    assert receive.media_type == "application/x-git-receive-pack-advertisement"
    assert bridge.seen["args"] == ("dataset", "owner", "repo")
    assert bridge.seen["lakefs_repo"], "the route must pass the row's resolved LakeFS id"
    assert bridge.seen["branch"] == "main"


@pytest.mark.asyncio
async def test_upload_pack_and_head_use_the_row_that_was_found(bridge, monkeypatch):
    owner = make_user("owner")
    make_repo(owner, "repo", repo_type="space")
    seen = {}

    class UploadHandler:
        def __init__(self, repo_id, bridge):
            seen["handler"] = (repo_id, bridge.__class__.__name__)

        async def handle_upload_pack(self, request_body):
            seen["body"] = request_body
            return b"upload-pack-result"

    monkeypatch.setattr(git_http, "GitUploadPackHandler", UploadHandler)

    pack = await git_http.git_upload_pack(
        "owner", "repo", request=_Request(b"want main"), repo_type=None, user=owner
    )
    head = await git_http.git_head("owner", "repo", repo_type="space", user=owner)

    assert pack.body == b"upload-pack-result"
    assert pack.media_type == "application/x-git-upload-pack-result"
    assert head.body == b"ref: refs/heads/main\n"
    assert seen["body"] == b"want main"
    assert bridge.seen["args"] == ("space", "owner", "repo")
    assert bridge.seen["lakefs_repo"]


@pytest.mark.asyncio
async def test_a_push_is_refused_without_reading_the_pack():
    owner = make_user("owner")
    make_repo(owner, "repo")

    class BodyMustNotBeRead:
        async def body(self):
            raise AssertionError("the pack must not be read")

    with pytest.raises(GitProtocolError) as refused:
        await git_http.git_receive_pack(
            "owner", "repo", request=BodyMustNotBeRead(), repo_type=None, user=owner
        )

    assert refused.value.status_code == 501
    assert "not supported" in refused.value.message


@pytest.mark.asyncio
async def test_a_push_checks_who_is_asking_before_it_refuses():
    make_repo(make_user("owner"), "repo")
    stranger = make_user("stranger")

    with pytest.raises(GitProtocolError) as anonymous:
        await git_http.git_receive_pack("owner", "repo", request=_Request(), repo_type=None, user=None)
    with pytest.raises(GitProtocolError) as other:
        await git_http.git_receive_pack("owner", "repo", request=_Request(), repo_type=None, user=stranger)

    assert anonymous.value.status_code == 401
    assert other.value.status_code == 403  # a public repository the caller can read but not write


def test_without_a_prefix_the_first_type_with_that_name_wins():
    owner = make_user("owner")
    space = make_repo(owner, "same", repo_type="space")
    dataset = make_repo(owner, "same", repo_type="dataset")

    assert git_http._find_repository(None, "owner", "same") == dataset
    assert git_http._find_repository("space", "owner", "same") == space
    assert git_http._find_repository("model", "owner", "same") is None
    assert git_http._find_repository(None, "owner", "nothing") is None


def test_an_unknown_type_prefix_is_not_found():
    with pytest.raises(HTTPException) as unknown:
        git_http._find_repository("widget", "owner", "repo")

    assert unknown.value.status_code == 404


@pytest.mark.parametrize("service", [None, "", "git-bad-service"])
def test_only_the_two_git_services_are_valid(service):
    with pytest.raises(GitProtocolError) as invalid:
        git_http._check_service(service)

    assert invalid.value.status_code == 400
    assert invalid.value.message == "invalid service"
    assert git_http._check_service("git-upload-pack") == "git-upload-pack"
    assert git_http._check_service("git-receive-pack") == "git-receive-pack"
