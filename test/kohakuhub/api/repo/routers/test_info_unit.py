"""Unit tests for repository info routes, on real repository rows.

The LakeFS client is an external service and stays a fake; repository lookup,
sorting, privacy filtering, storage quota and trending are real SQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

import kohakuhub.api.repo.routers.info as repo_info
from kohakuhub.auth.permissions import RepoReadDeniedError
from test.kohakuhub.support.factories import make_daily_stats, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


class _FakeClient:
    """LakeFS stand-in: the external service is answered, never reached."""

    def __init__(self):
        self.branch_result = {"commit_id": "commit-1234567890abcdef"}
        self.branch_error = None
        self.commit_result = {"creation_date": 1}
        self.commit_error = None
        self.list_error = None

    async def get_branch(self, **kwargs):
        if self.branch_error:
            raise self.branch_error
        return self.branch_result

    async def get_commit(self, **kwargs):
        if self.commit_error:
            raise self.commit_error
        return self.commit_result

    async def list_objects(self, **kwargs):
        if self.list_error:
            raise self.list_error
        return {"results": [], "pagination": {"has_more": False}}


def _request(path: str):
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.fixture
def lakefs(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repo_info, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_info, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    return client


def test_sorting_orders_real_rows_by_likes_and_downloads(lakefs):
    owner = make_user("alice")
    small = make_repo(owner, "small")
    big = make_repo(owner, "big")
    big.likes_count = 9
    big.downloads = 1
    big.save()
    small.likes_count = 2
    small.downloads = 50
    small.save()

    by_likes = repo_info._apply_repo_sorting(
        repo_info.Repository.select().where(repo_info.Repository.owner == owner), "model", "likes"
    )
    assert [repo.name for repo in by_likes] == ["big", "small"]

    by_downloads = repo_info._apply_repo_sorting(
        repo_info.Repository.select().where(repo_info.Repository.owner == owner),
        "model",
        "downloads",
    )
    assert [repo.name for repo in by_downloads] == ["small", "big"]


def test_privacy_filter_hides_private_repos_from_anonymous_readers():
    owner = make_user("alice")
    make_repo(owner, "open")
    make_repo(owner, "secret", private=True)

    anonymous = repo_info._filter_repos_by_privacy(repo_info.Repository.select(), None)
    assert [repo.name for repo in anonymous] == ["open"]

    own = repo_info._filter_repos_by_privacy(
        repo_info.Repository.select(), SimpleNamespace(id=owner.id, username="alice")
    )
    assert sorted(repo.name for repo in own) == ["open", "secret"]


@pytest.mark.asyncio
async def test_get_repo_info_reports_404_for_unknown_type_and_missing_repo(lakefs):
    response = await repo_info.get_repo_info.__wrapped__(
        "alice", "demo", request=_request("/api/unknown/alice/demo"), user=None, expand=None
    )
    assert response.status_code == 404

    response = await repo_info.get_repo_info.__wrapped__(
        "alice", "demo", request=_request("/api/models/alice/demo"), user=None, expand=None
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_repo_info_returns_the_row_counters_siblings_and_storage(lakefs, monkeypatch):
    owner = make_user("alice")
    repo = make_repo(owner, "demo")
    repo.downloads = 12
    repo.likes_count = 3
    repo.used_bytes = 20
    repo.save()

    sibling_calls = []

    async def fake_siblings(repo_row, lakefs_repo, commit, *, with_metadata):
        sibling_calls.append((commit, with_metadata))
        return '[{"rfilename": "README.md"}]'

    monkeypatch.setattr(repo_info, "hf_siblings_json", fake_siblings)
    monkeypatch.setattr(
        "kohakuhub.api.quota.util.get_repo_storage_info",
        lambda repo: {
            "quota_bytes": 100,
            "used_bytes": 20,
            "available_bytes": 80,
            "percentage_used": 20,
            "effective_quota_bytes": 100,
            "is_inheriting": False,
        },
    )

    async def call(user=None, expand=None, blobs=False):
        response = await repo_info.get_repo_info.__wrapped__(
            "alice",
            "demo",
            request=_request("/api/models/alice/demo"),
            user=user,
            expand=expand,
            blobs=blobs,
        )
        if getattr(response, "status_code", 200) != 200:
            return response
        return json.loads(response.body) if hasattr(response, "body") else response

    info = await call(user=SimpleNamespace(username="alice"))
    assert info["id"] == "alice/demo"
    assert info["usedStorage"] == 20
    assert info["storage"]["quota_bytes"] == 100
    assert info["siblings"] == [{"rfilename": "README.md"}]
    assert sibling_calls[-1] == ("commit-1234567890abcdef", False)

    sibling_calls.clear()
    page = await call(user=SimpleNamespace(username="alice"), expand=["sha", "private"])
    assert set(page) == {"_id", "id", "sha", "private"}
    assert not sibling_calls

    with_blobs = await call(expand=["sha"], blobs=True)
    assert with_blobs["siblings"]
    assert sibling_calls[-1] == ("commit-1234567890abcdef", True)

    bogus = await call(expand=["bogus"])
    assert bogus.status_code == 400


@pytest.mark.asyncio
async def test_get_repo_info_survives_lakefs_and_quota_failures(lakefs, monkeypatch):
    owner = make_user("alice")
    make_repo(owner, "demo")

    monkeypatch.setattr(
        "kohakuhub.api.quota.util.get_repo_storage_info",
        lambda repo: (_ for _ in ()).throw(RuntimeError("quota fail")),
    )
    lakefs.commit_error = RuntimeError("commit fail")

    async def no_siblings(*args, **kwargs):
        return "[]"

    monkeypatch.setattr(repo_info, "hf_siblings_json", no_siblings)

    response = await repo_info.get_repo_info.__wrapped__(
        "alice", "demo", request=_request("/api/models/alice/demo"), user=SimpleNamespace(username="alice"), expand=None
    )
    info = json.loads(response.body)
    assert "storage" not in info
    assert info["lastModified"] is None

    lakefs.commit_error = None
    lakefs.list_error = RuntimeError("list fail")

    async def failing_siblings(*args, **kwargs):
        raise RuntimeError("list fail")

    monkeypatch.setattr(repo_info, "hf_siblings_json", failing_siblings)
    info = json.loads(
        (
            await repo_info.get_repo_info.__wrapped__(
                "alice", "demo", request=_request("/api/models/alice/demo"), user=None, expand=None
            )
        ).body
    )
    assert info["siblings"] == []

    lakefs.branch_error = RuntimeError("missing branch")
    info = json.loads(
        (
            await repo_info.get_repo_info.__wrapped__(
                "alice", "demo", request=_request("/api/models/alice/demo"), user=None, expand=None
            )
        ).body
    )
    assert info["sha"] is None
    assert info["siblings"] == []


@pytest.mark.asyncio
async def test_get_repo_info_propagates_permission_outcomes(lakefs, monkeypatch):
    owner = make_user("alice")
    make_repo(owner, "demo", private=True)

    with pytest.raises(RepoReadDeniedError):
        await repo_info.get_repo_info.__wrapped__(
            "alice", "demo", request=_request("/api/models/alice/demo"), user=None, expand=None
        )

    def _conflict(repo, user):
        raise HTTPException(status_code=409, detail="unexpected")

    monkeypatch.setattr(repo_info, "check_repo_read_permission", _conflict)
    with pytest.raises(HTTPException) as propagated:
        await repo_info.get_repo_info.__wrapped__(
            "alice", "demo", request=_request("/api/models/alice/demo"), user=None, expand=None
        )
    assert propagated.value.status_code == 409


@pytest.mark.asyncio
async def test_trending_list_uses_real_stats_and_falls_back_to_lakefs(lakefs):
    owner = make_user("alice")
    repo = make_repo(owner, "demo")
    lakefs.commit_error = RuntimeError("commit fail")
    make_daily_stats(repo, datetime.now(timezone.utc).date() - timedelta(days=1), download_sessions=4)

    trending = await repo_info._list_repos_internal("model", sort="trending", user=None)

    assert [item["id"] for item in trending] == ["alice/demo"]
    assert trending[0]["lastModified"] is None


@pytest.mark.asyncio
async def test_list_routes_dispatch_by_path_and_reject_unknown_types(monkeypatch):
    async def _models(*args, **kwargs):
        return ["models"]

    async def _datasets(*args, **kwargs):
        return ["datasets"]

    async def _spaces(*args, **kwargs):
        return ["spaces"]

    monkeypatch.setattr(repo_info, "_list_models_with_aggregation", _models)
    monkeypatch.setattr(repo_info, "_list_datasets_with_aggregation", _datasets)
    monkeypatch.setattr(repo_info, "_list_spaces_with_aggregation", _spaces)

    assert await repo_info.list_repos(request=_request("/api/models")) == ["models"]
    assert await repo_info.list_repos(request=_request("/api/datasets")) == ["datasets"]
    assert await repo_info.list_repos(request=_request("/api/spaces")) == ["spaces"]
    assert (await repo_info.list_repos(request=_request("/api/unknown"))).status_code == 404


@pytest.mark.asyncio
async def test_user_repo_listing_404s_unknown_names_and_sorts_real_rows(lakefs):
    owner = make_user("alice")
    small = make_repo(owner, "small")
    big = make_repo(owner, "big")
    big.likes_count = 9
    big.save()
    small.downloads = 50
    small.save()

    missing = await repo_info.list_user_repos.__wrapped__(
        "ghost", request=None, user=None
    )
    assert missing.status_code == 404

    by_likes = await repo_info.list_user_repos.__wrapped__(
        "alice", request=None, limit=10, sort="likes", user=owner
    )
    assert by_likes["models"][0]["id"] == "alice/big"

    by_downloads = await repo_info.list_user_repos.__wrapped__(
        "alice", request=None, limit=10, sort="downloads", user=owner
    )
    assert by_downloads["models"][0]["id"] == "alice/small"

    recent = await repo_info.list_user_repos.__wrapped__(
        "alice", request=None, limit=10, sort="recent", user=owner
    )
    assert {item["id"] for item in recent["models"]} == {"alice/small", "alice/big"}
