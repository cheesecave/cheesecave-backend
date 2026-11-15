"""Invitation repositories through the installed real huggingface_hub client.

Run this file with each client version of backend-regression.yml. The actual app
serves HTTP with lifespan disabled; only LakeFS and object storage are fixtures.
No Docker or full service fixture is needed, and authorization always uses real SQL.
"""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import huggingface_hub
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import RepositoryNotFoundError
import pytest
import requests
import uvicorn

from kohakuhub import main, repo_derive, repository_invites as invites
from kohakuhub.api import files
from kohakuhub.api.repo.routers import info
from kohakuhub.auth.utils import hash_token
from kohakuhub.config import cfg
from kohakuhub.db import Repository, RepositoryCard, RepositoryMetadata, utcnow
from kohakuhub.utils import lakefs
from test.kohakuhub.support.factories import make_commit, make_file, make_repo, make_token, make_user

pytestmark = pytest.mark.hf_client

PAYLOAD = b"CheeseCave invitation-granted download fixture\n" * 64
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
COMMIT = "c" * 40


@contextmanager
def _object_server():
    """A synthetic S3 object, served over HTTP after the backend authorizes resolve."""
    downloaded = []

    class ObjectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            downloaded.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ObjectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/object", downloaded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@contextmanager
def _backend_server():
    """Bind the socket before starting uvicorn, and explicitly turn lifespan off."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("The isolated invitation test server did not start")
            time.sleep(0.01)
        yield address
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


class FixtureLakeFS:
    """Only the storage calls used by info/list and ordinary resolve, not API policy."""

    async def get_branch(self, *, repository, branch):
        return {"id": branch, "commit_id": COMMIT}

    async def get_commit(self, *, repository, commit_id):
        return {"id": COMMIT, "creation_date": 1704067200, "message": "Seed", "parents": []}

    async def list_objects(self, **kwargs):
        return {"results": [self._object()], "pagination": {"has_more": False}}

    async def stat_object(self, **kwargs):
        return self._object()

    @staticmethod
    def _object():
        return {
            "path": "weights.bin", "path_type": "object", "size_bytes": len(PAYLOAD),
            "physical_address": f"s3://test-bucket/lfs/{SHA256[:2]}/{SHA256[2:4]}/{SHA256}",
            "checksum": SHA256, "mtime": 1704067200,
        }


@pytest.fixture
def invitation_hub(db_fresh, monkeypatch):
    monkeypatch.setattr(cfg.fallback, "enabled", False)
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    owner, reader = make_user("owner"), make_user("reader")
    make_token(owner, hash_token("hf_matrix_owner"))
    make_token(reader, hash_token("hf_matrix_reader"))
    public = make_repo(owner, "public")
    target = make_repo(owner, "invited", private=True, invite_mode=True)
    sibling = make_repo(owner, "sibling", private=True)
    for repo in (public, target, sibling):
        make_commit(repo, COMMIT)
        make_file(repo, "weights.bin", SHA256, size=len(PAYLOAD), lfs=True)
        RepositoryMetadata.create(repository=repo, main_sha=COMMIT, state="ready", checked_at=utcnow())
        RepositoryCard.create(
            repository=repo, main_sha=COMMIT, card="{}", version=repo_derive.VERSION,
            derived=json.dumps({"tags": [], "pipeline_tag": None, "library_name": None, "sdk": None}),
        )
    _, code = invites.issue_invite(target, owner, expires_at=utcnow() + timedelta(days=1))
    storage = FixtureLakeFS()
    monkeypatch.setattr(info, "get_lakefs_client", lambda: storage)
    monkeypatch.setattr(files, "get_lakefs_client", lambda: storage)
    monkeypatch.setattr(lakefs, "get_lakefs_client", lambda: storage)
    with _object_server() as (object_url, downloaded):
        async def presign(**kwargs):
            return object_url

        monkeypatch.setattr(files, "generate_download_presigned_url", presign)
        with _backend_server() as endpoint:
            monkeypatch.setattr(cfg.app, "base_url", endpoint)
            yield SimpleNamespace(
                endpoint=endpoint, owner=owner, reader=reader, target=target, sibling=sibling,
                code=code, downloaded=downloaded,
                anonymous=HfApi(endpoint=endpoint, token=False),
                owner_api=HfApi(endpoint=endpoint, token="hf_matrix_owner"),
                reader_api=HfApi(endpoint=endpoint, token="hf_matrix_reader"),
            )


def _assert_private_denied(api, repo_id):
    with pytest.raises(RepositoryNotFoundError) as caught:
        api.model_info(repo_id)
    assert caught.value.response.status_code == 404
    assert caught.value.response.headers["X-Error-Code"] == "RepoNotFound"


def test_invitation_repository_is_private_not_hf_gated_and_hidden_until_granted(invitation_hub):
    hub = invitation_hub
    for api in (hub.anonymous, hub.reader_api):
        _assert_private_denied(api, hub.target.full_id)
        _assert_private_denied(api, hub.sibling.full_id)
        assert {repo.id for repo in api.list_models(author="owner")} == {"owner/public"}
    owner_info = hub.owner_api.model_info(hub.target.full_id)
    assert owner_info.private is True and owner_info.gated is False
    invites.grant_account(hub.target, hub.owner, hub.reader)
    granted_info = hub.reader_api.model_info(hub.target.full_id)
    assert granted_info.private is True and granted_info.gated is False
    assert {repo.id for repo in hub.reader_api.list_models(author="owner")} == {"owner/public", "owner/invited"}
    assert {repo.id for repo in hub.anonymous.list_models(author="owner")} == {"owner/public"}
    _assert_private_denied(hub.reader_api, hub.sibling.full_id)


def test_only_a_granted_accounts_own_token_downloads_the_raw_file(invitation_hub, tmp_path):
    hub = invitation_hub
    # The exchange code is not a Bearer access token, before or after account binding.
    code_api = HfApi(endpoint=hub.endpoint, token=hub.code)
    _assert_private_denied(code_api, hub.target.full_id)
    for token in (False, "hf_matrix_reader", hub.code):
        with pytest.raises(RepositoryNotFoundError):
            hf_hub_download(hub.target.full_id, "weights.bin", token=token,
                            endpoint=hub.endpoint, cache_dir=tmp_path / str(bool(token)), etag_timeout=5)
    assert not hub.downloaded
    bound = requests.post(
        hub.endpoint + "/api/models/owner/invited/access/bind", json={"token": hub.code},
        headers={"Authorization": "Bearer hf_matrix_reader"}, timeout=5,
    )
    assert bound.status_code == 200
    result = hf_hub_download(hub.target.full_id, "weights.bin", token="hf_matrix_reader",
                             endpoint=hub.endpoint, cache_dir=tmp_path / "granted", etag_timeout=5)
    assert Path(result).read_bytes() == PAYLOAD
    assert hub.downloaded == ["/object"]
    with pytest.raises(RepositoryNotFoundError):
        hf_hub_download(hub.sibling.full_id, "weights.bin", token="hf_matrix_reader",
                        endpoint=hub.endpoint, cache_dir=tmp_path / "sibling", etag_timeout=5)
    _assert_private_denied(code_api, hub.target.full_id)


def test_client_visibility_updates_keep_invitation_mode_until_explicit_public(invitation_hub):
    hub = invitation_hub
    # 0.20 uses update_repo_visibility/private; newer clients use settings/visibility.
    update = getattr(hub.owner_api, "update_repo_settings", None)
    if update is None:
        update = getattr(hub.owner_api, "update_repo_visibility", None)
    if update is None:
        pytest.skip(f"huggingface_hub {huggingface_hub.__version__} has no visibility update API")
    update(repo_id=hub.target.full_id, private=True)
    row = Repository.get_by_id(hub.target.id)
    assert row.private is True and row.invite_mode is True
    update(repo_id=hub.target.full_id, private=False)
    row = Repository.get_by_id(hub.target.id)
    assert row.private is False and row.invite_mode is False
    assert hub.anonymous.model_info(hub.target.full_id).private is False
