"""Product cards distinguish invite/private/public after the shared SQL read filter.

HF info/list fields remain ordinary HF booleans and retain their expand vocabulary.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kohakuhub import repo_derive
from kohakuhub.api import social
from kohakuhub.api.repo.routers import discovery, info
from kohakuhub.api.repo.utils.hf import hf_repo_not_found
from kohakuhub.auth.dependencies import get_current_user, get_optional_user
from kohakuhub.auth.permissions import RepoReadDeniedError
from kohakuhub.db import RepositoryAccessGrant, RepositoryCard, RepositoryMetadata, UserFollow, utcnow
from test.kohakuhub.support.factories import make_commit, make_repo, make_user


@pytest.fixture
def cards(db_fresh, monkeypatch):
    owner, reader = make_user("owner"), make_user("reader")
    UserFollow.create(follower=reader, followed=owner)
    repositories = {}
    for kind in ("model", "dataset", "space"):
        for state in ("public", "private", "invite"):
            repo = make_repo(owner, state, repo_type=kind, private=state != "public", invite_mode=state == "invite")
            make_commit(repo, f"{repo.id:040x}", author=owner)
            RepositoryMetadata.create(repository=repo, state="ready", checked_at=utcnow())
            RepositoryCard.create(repository=repo, card="{}", derived='{"tags":[],"pipeline_tag":null,"library_name":null,"sdk":null}', version=repo_derive.VERSION)
            repositories[kind, state] = repo
        RepositoryAccessGrant.create(repository=repositories[kind, "invite"], user=reader)
    auth = {"user": owner}
    app = FastAPI()
    app.include_router(info.router, prefix="/api")
    app.include_router(discovery.router, prefix="/api")
    app.include_router(social.router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: auth["user"]

    def authenticated():
        if auth["user"] is None:
            raise HTTPException(401, "Login required")
        return auth["user"]

    app.dependency_overrides[get_current_user] = authenticated

    @app.exception_handler(RepoReadDeniedError)
    async def denied(request, error):
        return hf_repo_not_found(error.repo_id, error.repo_type)

    storage = AsyncMock()
    storage.get_branch.return_value = {"commit_id": None}
    monkeypatch.setattr(info, "get_lakefs_client", lambda: storage)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, owner=owner, reader=reader, auth=auth, repositories=repositories)


@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
@pytest.mark.parametrize("viewer", ["owner", "reader", "anonymous"])
def test_discovery_and_profile_emit_boolean_invite_mode_only_for_readable_rows(cards, kind, viewer):
    cards.auth["user"] = None if viewer == "anonymous" else getattr(cards, viewer)
    visible = {"public", "private", "invite"} if viewer == "owner" else {"public", "invite"} if viewer == "reader" else {"public"}
    discovered = cards.client.get(f"/api/{kind}s/discover", params={"sort": "recent"})
    profile = cards.client.get("/api/users/owner/repos", params={"fallback": "false"})
    assert discovered.status_code == profile.status_code == 200
    for rows in (discovered.json()["items"], profile.json()[kind + "s"]):
        assert {row["id"] for row in rows} == {"owner/" + state for state in visible}
        for row in rows:
            state = row["id"].split("/")[1]
            assert row["private"] is (state != "public")
            assert row["invite_mode"] is (state == "invite")
            assert row["gated"] is False


@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
@pytest.mark.parametrize("viewer", ["owner", "reader"])
def test_workspace_feed_hydrates_invite_mode_without_bypassing_read_filter(cards, kind, viewer):
    cards.auth["user"] = getattr(cards, viewer)
    response = cards.client.get("/api/workspace/feed", params={"scope": "all", "repo_type": kind})
    assert response.status_code == 200
    rows = [item["repository"] for item in response.json()["items"]]
    visible = {"public", "private", "invite"} if viewer == "owner" else {"public", "invite"}
    assert {row["id"] for row in rows} == {"owner/" + state for state in visible}
    for row in rows:
        assert row["private"] is (row["id"] != "owner/public")
        assert row["invite_mode"] is (row["id"] == "owner/invite")


@pytest.mark.parametrize("kind", ["model", "dataset", "space"])
def test_hf_info_and_list_do_not_gain_product_only_fields_or_expand_vocabulary(cards, kind):
    listed = cards.client.get(f"/api/{kind}s", params={"fallback": "false"})
    assert listed.status_code == 200
    assert len(listed.json()) == 3
    for row in listed.json():
        assert type(row["private"]) is bool
        assert "invite_mode" not in row and "visibility" not in row
    info_response = cards.client.get(f"/api/{kind}s/owner/invite", params={"expand": "private", "fallback": "false"})
    assert info_response.status_code == 200 and info_response.json() == {
        "_id": str(cards.repositories[kind, "invite"].id), "id": "owner/invite", "private": True,
    }
    rejected = cards.client.get(f"/api/{kind}s/owner/invite", params={"expand": "invite_mode", "fallback": "false"})
    assert rejected.status_code == 400
