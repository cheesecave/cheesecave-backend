"""A repository guest cookie is never an HF, Git, LFS or account credential.

These tests use the real application routes and real SQL. TestClient is deliberately
not entered as a context manager: application startup would initialize external storage.
Only LakeFS and upstream fallback operations are replaced at their environment boundary.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kohakuhub import main, repository_invites as invites
from kohakuhub.api import repository_access
from kohakuhub.api.fallback import decorators as fallback
from kohakuhub.api.repo.routers import info
from kohakuhub.auth.utils import hash_token
from kohakuhub.config import cfg
from kohakuhub.db import RepositoryAccessGrant, RepositoryGuestSession, RepositoryInvite, Session, Token, User, utcnow
from test.kohakuhub.support.factories import make_file, make_repo, make_session, make_token, make_user


@pytest.fixture
def guest_boundary(db_fresh, monkeypatch):
    owner = make_user("owner")
    target = make_repo(owner, "target", private=True, invite_mode=True)
    sibling = make_repo(owner, "sibling", private=True)
    make_file(target, "file.bin", "a" * 64, size=32, lfs=True)
    make_file(sibling, "file.bin", "b" * 64, size=32, lfs=True)
    row, code = invites.issue_invite(
        target, owner, expires_at=utcnow() + timedelta(days=1),
    )
    upstream = {}
    monkeypatch.setattr(cfg.fallback, "enabled", True)
    for name in ("try_fallback_info", "try_fallback_tree", "try_fallback_paths_info", "try_fallback_resolve"):
        upstream[name] = AsyncMock(side_effect=AssertionError("A denied local repository must not contact an upstream"))
        monkeypatch.setattr(fallback, name, upstream[name])
    storage = AsyncMock()
    storage.get_branch.return_value = {"commit_id": None}
    monkeypatch.setattr(info, "get_lakefs_client", lambda: storage)
    # No context manager: no lifespan hooks, SMTP, storage initialization or workers.
    client = TestClient(main.app)
    response = client.post("/api/models/owner/target/access/guest", json={"token": code})
    assert response.status_code == 200 and response.json()["access"] == "guest"
    secret = client.cookies.get(repository_access.guest_cookie_name(target))
    assert invites.get_guest_session(target, secret) is not None
    assert invites.get_guest_session(sibling, secret) is None
    state = client.get("/api/models/owner/target/access")
    assert state.status_code == 200 and state.json()["access"] == "guest"
    try:
        yield SimpleNamespace(client=client, owner=owner, target=target, sibling=sibling,
                              invitation=row, code=code, secret=secret, upstream=upstream, storage=storage)
    finally:
        client.close()


def _assert_hf_denial(response):
    assert response.status_code == 404
    assert response.headers.get("x-error-code") == "RepoNotFound"
    assert response.headers.get("x-error-message")
    assert response.content == b""
    assert "location" not in response.headers
    assert not any(name.lower().startswith("x-xet-") for name in response.headers)


HF_SURFACES = [
    ("GET", "/api/models/owner/{repo}", {}),
    ("GET", "/api/models/owner/{repo}/revision/main", {}),
    ("GET", "/api/models/owner/{repo}/auth-check", {}),
    ("GET", "/api/models/owner/{repo}/tree/main", {}),
    ("POST", "/api/models/owner/{repo}/paths-info/main", {"data": {"paths": "file.bin"}}),
    ("GET", "/api/models/owner/{repo}/commits/main", {}),
    ("GET", "/api/models/owner/{repo}/refs", {}),
    ("GET", "/api/models/owner/{repo}/xet-read-token/main/file.bin", {}),
    ("GET", "/api/models/owner/{repo}/resolve/main/file.bin", {}),
    ("HEAD", "/api/models/owner/{repo}/resolve/main/file.bin", {}),
    ("GET", "/models/owner/{repo}/resolve/main/file.bin", {}),
    ("HEAD", "/models/owner/{repo}/resolve/main/file.bin", {}),
    ("GET", "/owner/{repo}/resolve/main/file.bin", {"headers": {"Range": "bytes=0-15"}}),
    ("GET", "/api/models/owner/{repo}/resolve/main/file.bin", {"headers": {"Range": "bytes=16-31"}}),
]


@pytest.mark.parametrize("repo", ["target", "sibling"])
@pytest.mark.parametrize("method,path,kwargs", HF_SURFACES, ids=[path + ":" + method for method, path, _ in HF_SURFACES])
def test_guest_cannot_read_hf_metadata_history_raw_or_ranges(guest_boundary, repo, method, path, kwargs):
    state = guest_boundary
    response = state.client.request(method, path.format(repo=repo), follow_redirects=False, **kwargs)
    _assert_hf_denial(response)
    state.storage.get_branch.assert_not_awaited()
    for probe in state.upstream.values():
        probe.assert_not_awaited()


@pytest.mark.parametrize("repo,oid", [("target", "a" * 64), ("sibling", "b" * 64)])
def test_guest_cannot_reconstruct_its_files_or_cross_repository_objects(guest_boundary, repo, oid):
    response = guest_boundary.client.get(f"/reconstructions/{oid}", follow_redirects=False)
    _assert_hf_denial(response)


@pytest.mark.parametrize("repo", ["target", "sibling"])
@pytest.mark.parametrize("prefix", ["/models/owner/{repo}.git", "/owner/{repo}.git", "/models/owner/{repo}", "/owner/{repo}"])
def test_guest_cannot_clone_fetch_or_push_any_git_url_form(guest_boundary, repo, prefix):
    base = prefix.format(repo=repo)
    advertisement = guest_boundary.client.get(base + "/info/refs?service=git-upload-pack")
    fetch = guest_boundary.client.post(base + "/git-upload-pack", content=b"0000")
    push = guest_boundary.client.post(base + "/git-receive-pack", content=b"0000")
    for response in (advertisement, fetch, push):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Basic realm="git-frontend"'
        assert response.headers["content-type"].startswith("text/plain")
        assert response.content == b"Invalid username or password."
        assert "location" not in response.headers


@pytest.mark.parametrize("repo", ["target", "sibling"])
@pytest.mark.parametrize("operation", ["download", "upload"])
def test_guest_cannot_obtain_lfs_object_actions(guest_boundary, repo, operation):
    response = guest_boundary.client.post(
        f"/models/owner/{repo}.git/info/lfs/objects/batch",
        json={"operation": operation, "objects": [{"oid": "a" * 64, "size": 32}]},
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/vnd.git-lfs+json")
    assert len(response.headers.get_list("lfs-authenticate")) == 2
    assert "actions" not in response.text and "href" not in response.text
    assert "location" not in response.headers


@pytest.mark.parametrize("repo", ["target", "sibling"])
def test_guest_cannot_use_authenticated_lfs_management_endpoints(guest_boundary, repo):
    # These browser management routes require a User before checking the repo,
    # unlike anonymous-readable HF endpoints: retain their existing 401 contract.
    listed = guest_boundary.client.get(f"/api/models/owner/{repo}/lfs-files")
    batch = guest_boundary.client.post(
        f"/api/models/owner/{repo}/lfs-files/batch", json={"file_paths": ["file.bin"]},
    )
    for response in (listed, batch):
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
        assert "location" not in response.headers


@pytest.mark.parametrize("method,path,kwargs", [
    ("GET", "/api/auth/me", {}),
    ("GET", "/api/auth/tokens", {}),
    ("POST", "/api/auth/tokens/create", {"json": {"name": "must-not-exist"}}),
    ("PUT", "/api/users/owner/settings", {"json": {"bio": "must-not-change"}}),
    ("GET", "/api/user/keys", {}),
    ("GET", "/api/models/owner/target/access/manage", {}),
    ("POST", "/api/models/owner/target/access/bind", {"json": {"token": "invalid"}}),
    ("POST", "/api/models/owner/target/commit/main", {"content": b""}),
])
def test_guest_never_becomes_an_account_or_writer(guest_boundary, method, path, kwargs):
    response = guest_boundary.client.request(method, path, **kwargs)
    assert response.status_code == 401
    assert Token.select().count() == Session.select().count() == 0
    assert User.get_by_id(guest_boundary.owner.id).bio is None
    assert RepositoryAccessGrant.select().count() == 0


@pytest.mark.parametrize("credential", ["code", "secret"])
def test_repository_exchange_secrets_are_not_bearer_access_tokens(guest_boundary, credential):
    state = guest_boundary
    headers = {"Authorization": "Bearer " + getattr(state, credential)}
    assert state.client.get("/api/auth/me", headers=headers).status_code == 401
    _assert_hf_denial(state.client.get("/api/models/owner/target/auth-check", headers=headers))
    assert Token.select().count() == 0


def test_renaming_a_guest_cookie_does_not_grant_a_different_repo_or_block_public_reads(guest_boundary):
    state = guest_boundary
    state.client.cookies.set(repository_access.guest_cookie_name(state.sibling), state.secret)
    sibling = state.client.get("/api/models/owner/sibling/access")
    missing = state.client.get("/api/models/owner/missing/access")
    assert sibling.status_code == missing.status_code == 200
    assert sibling.json() == missing.json() == {
        "access": "standard", "visibility": None, "can_manage": False, "invite_markdown": None,
    }
    _assert_hf_denial(state.client.get("/api/models/owner/sibling/auth-check"))
    _assert_hf_denial(state.client.get("/owner/sibling/resolve/main/file.bin", follow_redirects=False))
    public = make_repo(state.owner, "public")
    assert state.client.get(f"/api/models/{public.full_id}/auth-check").status_code == 200
    assert state.client.get(f"/api/models/{public.full_id}/access").json()["access"] == "full"


@pytest.mark.parametrize("repo_type", ["dataset", "space"])
def test_guest_cookies_do_not_authorize_other_repository_types(guest_boundary, repo_type):
    state = guest_boundary
    repo = make_repo(state.owner, "target", repo_type=repo_type, private=True, invite_mode=True)
    state.client.cookies.set(repository_access.guest_cookie_name(repo), state.secret)
    for path in (
        f"/api/{repo_type}s/owner/target",
        f"/api/{repo_type}s/owner/target/tree/main",
        f"/{repo_type}s/owner/target/resolve/main/file.bin",
    ):
        _assert_hf_denial(state.client.get(path, follow_redirects=False))


@pytest.mark.parametrize("identity", ["session", "token"])
def test_normal_account_identity_coexists_and_only_an_account_grant_unlocks_hf(guest_boundary, identity):
    state = guest_boundary
    reader = make_user("reader")
    own = make_repo(reader, "own", private=True)
    if identity == "session":
        make_session(reader, "normal-account-session")
        state.client.cookies.set("session_id", "normal-account-session")
    else:
        make_token(reader, hash_token("hf_normal_account_token"))
        state.client.headers["Authorization"] = "Bearer hf_normal_account_token"

    exchanged = state.client.post("/api/models/owner/target/access/guest", json={"token": state.code})
    assert exchanged.status_code == 200
    assert not any(cookie.startswith("session_id=") for cookie in exchanged.headers.get_list("set-cookie"))
    assert state.client.get("/api/auth/me").json()["username"] == "reader"
    assert state.client.get(f"/api/models/{own.full_id}/auth-check").status_code == 200
    assert state.client.get("/api/models/owner/target/access").json()["access"] == "guest"
    _assert_hf_denial(state.client.get("/api/models/owner/target/auth-check"))
    _assert_hf_denial(state.client.get("/api/models/owner/sibling/auth-check"))

    # Binding uses the existing account; the resulting read authorization is for this repo only.
    bound = state.client.post("/api/models/owner/target/access/bind", json={"token": state.code})
    assert bound.status_code == 200
    assert state.client.get("/api/models/owner/target/auth-check").status_code == 200
    info_response = state.client.get("/api/models/owner/target?expand=private")
    assert info_response.status_code == 200 and info_response.json()["private"] is True
    assert state.client.get("/api/models/owner/target/access").json()["access"] == "full"
    _assert_hf_denial(state.client.get("/api/models/owner/sibling/auth-check"))
    assert state.client.get("/api/models/owner/target/access/manage").status_code == 403
    assert state.client.put("/api/models/owner/target/settings", json={"invite_mode": False}).status_code == 403
    assert User.select().count() == 2 and RepositoryGuestSession.select().count() == 2
    assert Token.select().count() == (1 if identity == "token" else 0)
    assert Session.select().count() == (1 if identity == "session" else 0)
    grant = RepositoryAccessGrant.get(repository=state.target, user=reader)
    invites.revoke_grant(state.target, grant.id, state.owner)
    _assert_hf_denial(state.client.get("/api/models/owner/target/auth-check"))
    assert state.client.get("/api/models/owner/target/access").json()["access"] == "guest"


def test_approved_account_uses_its_personal_token_without_an_exchange_code(guest_boundary):
    state = guest_boundary
    reader = make_user("applicant")
    make_token(reader, hash_token("hf_applicant_personal_token"))
    make_token(state.owner, hash_token("hf_manager_personal_token"))
    reader_headers = {"Authorization": "Bearer hf_applicant_personal_token"}
    manager_headers = {"Authorization": "Bearer hf_manager_personal_token"}
    requested = state.client.post(
        "/api/models/owner/target/access/requests", headers=reader_headers, json={"message": "Research access"},
    )
    assert requested.status_code == 200
    request_id = requested.json()["request"]["id"]
    _assert_hf_denial(state.client.get("/api/models/owner/target/auth-check", headers=reader_headers))
    approved = state.client.post(
        f"/api/models/owner/target/access/requests/{request_id}/review",
        headers=manager_headers, json={"approve": True},
    )
    assert approved.status_code == 200
    assert state.client.get("/api/models/owner/target/auth-check", headers=reader_headers).status_code == 200
    assert state.client.get("/api/models/owner/target?expand=private", headers=reader_headers).json()["private"] is True
    _assert_hf_denial(state.client.get("/api/models/owner/sibling/auth-check", headers=reader_headers))
    assert state.client.get("/api/models/owner/target/access", headers=reader_headers).json()["access"] == "full"
    assert RepositoryInvite.get_by_id(state.invitation.id).redemption_count == 0
