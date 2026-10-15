"""Unit tests for repository info routes, on real repository rows.

The LakeFS client is an external service and stays a fake; repository lookup,
sorting, privacy filtering, storage quota and trending are real SQL.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

import kohakuhub.api.repo.routers.info as repo_info
from kohakuhub.auth.permissions import RepoReadDeniedError
from test.kohakuhub.support.factories import make_daily_stats, make_repo, make_user
from test.kohakuhub.support.list_local import list_local

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

    trending = await list_local("model", sort="trending")

    assert [item["id"] for item in trending] == ["alice/demo"]
    assert trending[0]["lastModified"] is None


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


@pytest.mark.asyncio
async def test_a_repository_deleted_after_the_order_was_read_is_left_out(lakefs):
    owner = make_user("alice")
    kept = make_repo(owner, "kept")

    items = await repo_info._render_local([kept.id + 1000, kept.id], tag_source=False)

    assert [item["id"] for item in items] == ["alice/kept"]
    assert await repo_info._render_local([], tag_source=False) == []


# --- the fields the Hub puts in every list item (#95, B) ---------------------------------


@pytest.mark.asyncio
async def test_list_items_carry_the_ids_the_hub_gives(lakefs):
    owner = make_user("alice")
    model = make_repo(owner, "m")
    dataset = make_repo(owner, "d", repo_type="dataset")
    space = make_repo(owner, "s", repo_type="space")

    by_type = {r.repo_type: (await repo_info._render_local([r.id], tag_source=False))[0] for r in (model, dataset, space)}

    assert by_type["model"]["_id"] == str(model.id) and by_type["model"]["modelId"] == "alice/m"
    assert by_type["dataset"]["_id"] == str(dataset.id) and "modelId" not in by_type["dataset"]
    assert by_type["space"]["_id"] == str(space.id) and "modelId" not in by_type["space"]


@pytest.mark.asyncio
async def test_only_a_dataset_item_says_whether_it_is_disabled(lakefs):
    owner = make_user("alice")
    items = {
        r.repo_type: (await repo_info._render_local([r.id], tag_source=False))[0]
        for r in (make_repo(owner, "m"), make_repo(owner, "d", repo_type="dataset"), make_repo(owner, "s", repo_type="space"))
    }

    assert items["dataset"]["disabled"] is False
    assert "disabled" not in items["model"] and "disabled" not in items["space"]


@pytest.mark.asyncio
async def test_the_trending_score_of_an_item_is_the_one_of_the_trending_sort(lakefs):
    owner = make_user("alice")
    busy = make_repo(owner, "busy")
    quiet = make_repo(owner, "quiet")
    today = datetime.now(timezone.utc).date()
    make_daily_stats(busy, today, download_sessions=10)
    make_daily_stats(busy, today - timedelta(days=1), download_sessions=3)

    items = {i["id"]: i for i in await repo_info._render_local([busy.id, quiet.id], tag_source=False)}

    assert items["alice/busy"]["trendingScore"] == round(math.log(11) + math.log(4) * 0.8, 2)
    assert items["alice/quiet"]["trendingScore"] == 0


@pytest.mark.asyncio
async def test_the_trending_scores_of_a_page_are_computed_once_for_that_page_only(lakefs, monkeypatch):
    owner = make_user("alice")
    ids = [make_repo(owner, f"m{n}").id for n in range(3)]
    other = make_repo(owner, "elsewhere")
    calls = []
    real = repo_info.calculate_trending_scores

    def spy(repo_type, days=7, ids=None):
        calls.append((repo_type, sorted(ids)))
        return real(repo_type, days, ids)

    monkeypatch.setattr(repo_info, "calculate_trending_scores", spy)

    await repo_info._render_local(ids, tag_source=False)

    assert calls == [("model", sorted(ids))] and other.id not in calls[0][1]


@pytest.mark.asyncio
async def test_a_page_of_two_kinds_gets_each_kinds_own_scores(lakefs):
    owner = make_user("alice")
    model = make_repo(owner, "m")
    dataset = make_repo(owner, "d", repo_type="dataset")
    today = datetime.now(timezone.utc).date()
    make_daily_stats(model, today, download_sessions=1)
    make_daily_stats(dataset, today, download_sessions=7)

    items = {i["id"]: i for i in await repo_info._render_local([model.id, dataset.id], tag_source=False)}

    assert items["alice/m"]["trendingScore"] == round(math.log(2), 2)
    assert items["alice/d"]["trendingScore"] == round(math.log(8), 2)


def test_the_trending_scores_can_be_asked_for_some_repositories_only():
    from kohakuhub.api.utils.trending import calculate_trending_scores

    owner = make_user("alice")
    a, b = make_repo(owner, "a"), make_repo(owner, "b")
    today = datetime.now(timezone.utc).date()
    make_daily_stats(a, today, download_sessions=5)
    make_daily_stats(b, today, download_sessions=5)

    assert set(calculate_trending_scores("model")) == {a.id, b.id}
    assert set(calculate_trending_scores("model", ids=[a.id])) == {a.id}
    assert calculate_trending_scores("model", ids=[]) == {}
