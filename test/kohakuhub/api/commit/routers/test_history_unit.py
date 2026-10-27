"""Unit tests for commit history routes, on real repository, commit and file rows."""

from __future__ import annotations

from datetime import datetime
import json
from json import JSONDecodeError
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
import pytest

import kohakuhub.api.commit.routers.history as commit_history
from kohakuhub import cache
from kohakuhub.api.repo.utils.hf import hf_repo_not_found
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import RepoReadDeniedError
from kohakuhub.config import cfg
from kohakuhub.db import UserOrganization
from kohakuhub.utils.commit_id import hub_oid
from test.kohakuhub.support.factories import make_commit, make_file, make_org, make_repo, make_user
from test.kohakuhub.support.fakes import lakefs_error

pytestmark = pytest.mark.usefixtures("db_scope")
HEAD = "a" * 64
BASE = "/api/models/alice/demo/commit-count"


class _FakeClient:
    """LakeFS is an environment service: it stays mocked, its database side is real rows."""

    def __init__(self):
        self.log_result = None
        self.log_error = None
        self.commit_result = None
        self.commit_error = None
        self.diff_result = None
        self.diff_error = None
        self.stat_results = {}
        self.stat_errors = {}
        self.object_results = {}
        self.object_errors = {}

    async def log_commits(self, **kwargs):
        if self.log_error:
            raise self.log_error
        return self.log_result

    async def get_commit(self, **kwargs):
        if self.commit_error:
            raise self.commit_error
        return self.commit_result

    async def diff_refs(self, **kwargs):
        if self.diff_error:
            raise self.diff_error
        return self.diff_result

    async def stat_object(self, **kwargs):
        key = (kwargs["ref"], kwargs["path"])
        if key in self.stat_errors:
            raise self.stat_errors[key]
        return self.stat_results[key]

    async def get_object(self, **kwargs):
        key = (kwargs["ref"], kwargs["path"])
        if key in self.object_errors:
            raise self.object_errors[key]
        return self.object_results[key]


@pytest.fixture
def alice():
    return make_user("alice")


@pytest.fixture
def repo(alice):
    return make_repo(alice, "demo")


@pytest.mark.asyncio
async def test_list_commits_covers_not_found_empty_parse_failure_and_server_error(
    monkeypatch,
    alice,
    repo,
):
    client = _FakeClient()
    make_commit(repo, "good-1", author=alice)

    monkeypatch.setattr(
        commit_history,
        "hf_repo_not_found",
        lambda repo_id, repo_type: SimpleNamespace(status_code=404, repo_id=repo_id),
    )
    monkeypatch.setattr(
        commit_history,
        "hf_server_error",
        lambda message: SimpleNamespace(status_code=500, message=message),
    )
    monkeypatch.setattr(commit_history, "check_repo_read_permission", lambda repo, user: None)
    monkeypatch.setattr(commit_history, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: client)

    not_found = await commit_history.list_commits("model", "nobody", "demo")
    assert not_found.status_code == 404

    client.log_result = None
    empty = await commit_history.list_commits("model", "alice", "demo", branch="main")
    assert empty.status_code == 200
    assert json.loads(empty.body) == []

    client.log_result = {
        "results": [
            {
                "id": "good-1",
                "message": "good commit",
                "creation_date": 123,
                "metadata": {"email": "alice@example.com"},
                "parents": ["parent"],
            },
            {
                "id": "broken-2",
                "message": "broken commit",
                # A null metadata cannot be read: LakeFS entry skipped, as before
                "metadata": None,
            },
        ],
        "pagination": {"has_more": True, "next_offset": "cursor-2"},
    }
    parsed = await commit_history.list_commits("model", "alice", "demo", after="cursor-1")
    assert parsed.status_code == 200
    assert json.loads(parsed.body) == [
        {
            "id": "good-1",
            "title": "good commit",
            "message": "good commit",
            "date": "1970-01-01T00:02:03.000000Z",
            "authors": [{"user": "alice"}],
        }
    ]

    client.log_error = RuntimeError("lakefs boom")
    failure = await commit_history.list_commits("model", "alice", "demo")
    assert failure.status_code == 500


@pytest.mark.asyncio
async def test_list_commits_sets_link_header_and_formatted_expand(monkeypatch, alice, repo):
    client = _FakeClient()
    make_commit(repo, "good-1", author=alice)

    class _RequestURL:
        def __init__(self):
            self.params = {}

        def include_query_params(self, **kwargs):
            merged = dict(self.params)
            merged.update(kwargs)
            query = "&".join(f"{key}={value}" for key, value in merged.items())
            return f"https://hub.local/api/models/alice/demo/commits/main?{query}"

    request = SimpleNamespace(
        url=_RequestURL(),
        query_params=SimpleNamespace(getlist=lambda key: ["formatted"] if key == "expand[]" else []),
    )

    monkeypatch.setattr(commit_history, "check_repo_read_permission", lambda repo, user: None)
    monkeypatch.setattr(commit_history, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: client)

    client.log_result = {
        "results": [
            {
                "id": "good-1",
                "title": "Add README title",
                "message": "Add README title\n\nbody",
                "creation_date": "2026-04-21T10:00:00.000000Z",
                "metadata": {"email": "alice@example.com"},
                "parents": ["parent"],
            }
        ],
        "pagination": {"has_more": True, "next_offset": "cursor-2"},
    }

    response = await commit_history.list_commits(
        "model",
        "alice",
        "demo",
        branch="main",
        request=request,
    )

    assert response.status_code == 200
    assert response.headers["link"] == (
        '<https://hub.local/api/models/alice/demo/commits/main?after=cursor-2>; rel="next"'
    )
    assert json.loads(response.body) == [
        {
            "id": "good-1",
            "title": "Add README title",
            "message": "Add README title\n\nbody",
            "date": "2026-04-21T10:00:00.000000Z",
            "authors": [{"user": "alice"}],
            "formatted": {
                "title": "Add README title",
                "message": "Add README title\n\nbody",
            },
        }
    ]


@pytest.mark.asyncio
async def test_list_commits_skips_link_when_cursor_missing_and_propagates_read_errors(
    monkeypatch,
    alice,
    repo,
):
    client = _FakeClient()
    make_commit(repo, "good-1", author=alice)

    monkeypatch.setattr(commit_history, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: client)

    def _raise_forbidden(repo, user):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(commit_history, "check_repo_read_permission", _raise_forbidden)
    with pytest.raises(HTTPException) as permission_error:
        await commit_history.list_commits("model", "alice", "demo")
    assert permission_error.value.status_code == 403

    monkeypatch.setattr(commit_history, "check_repo_read_permission", lambda repo, user: None)
    client.log_result = {
        "results": [
            {
                "id": "good-1",
                "message": "",
                "creation_date": 123,
            }
        ],
        "pagination": {"has_more": True, "next_offset": None},
    }
    response = await commit_history.list_commits("model", "alice", "demo")
    assert response.status_code == 200
    assert "link" not in response.headers
    assert json.loads(response.body)[0]["authors"] == [{"user": "alice"}]


@pytest.mark.asyncio
async def test_get_commit_detail_covers_not_found_fallback_author_and_server_error(
    monkeypatch,
    alice,
    repo,
):
    client = _FakeClient()
    now = datetime(2024, 1, 2)  # naive, as the TIMESTAMP column returns it on Postgres

    monkeypatch.setattr(
        commit_history,
        "hf_repo_not_found",
        lambda repo_id, repo_type: SimpleNamespace(status_code=404),
    )
    monkeypatch.setattr(
        commit_history,
        "hf_server_error",
        lambda message: SimpleNamespace(status_code=500, message=message),
    )
    monkeypatch.setattr(commit_history, "check_repo_read_permission", lambda repo, user: None)
    monkeypatch.setattr(commit_history, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: client)

    not_found = await commit_history.get_commit_detail("model", "nobody", "demo", "abc")
    assert not_found.status_code == 404

    client.commit_result = {
        "id": "abc",
        "message": "commit message",
        "creation_date": 321,
        "parents": ["base"],
        "metadata": {"email": "alice@example.com"},
        "committer": "fallback-user",
    }
    # No commit row for abc: the author is what LakeFS recorded
    detail = await commit_history.get_commit_detail("model", "alice", "demo", "abc")
    assert detail["author"] == "fallback-user"
    assert detail["metadata"] == {"email": "alice@example.com"}

    make_commit(repo, "abc", author=alice, description="desc", created_at=now)
    detail_with_author = await commit_history.get_commit_detail("model", "alice", "demo", "abc")
    assert detail_with_author["author"] == "alice"
    assert detail_with_author["user_id"] == alice.id
    assert detail_with_author["description"] == "desc"
    assert detail_with_author["committed_at"] == now.isoformat()

    client.commit_error = RuntimeError("commit boom")
    failure = await commit_history.get_commit_detail("model", "alice", "demo", "abc")
    assert failure.status_code == 500


@pytest.mark.asyncio
async def test_get_commit_diff_covers_parentless_diff_generation_skips_and_errors(
    monkeypatch,
    repo,
):
    client = _FakeClient()
    make_file(repo, "weights.bin", "weights", size=9, lfs=True)

    monkeypatch.setattr(
        commit_history,
        "hf_repo_not_found",
        lambda repo_id, repo_type: SimpleNamespace(status_code=404),
    )
    monkeypatch.setattr(
        commit_history,
        "hf_server_error",
        lambda message: SimpleNamespace(status_code=500, message=message),
    )
    monkeypatch.setattr(commit_history, "check_repo_read_permission", lambda repo, user: None)
    monkeypatch.setattr(commit_history, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: client)
    monkeypatch.setattr(
        commit_history,
        "should_use_lfs",
        lambda repo, path, size: path.endswith(".bin"),
    )

    not_found = await commit_history.get_commit_diff("model", "nobody", "demo", "abc")
    assert not_found.status_code == 404

    client.commit_result = {"id": "abc", "parents": [], "message": "initial", "creation_date": 1}
    parentless = await commit_history.get_commit_diff("model", "alice", "demo", "abc")
    assert parentless == {"files": [], "parent_commit": None}

    client.commit_result = {
        "id": "abc",
        "parents": ["parent-1"],
        "message": "update files",
        "creation_date": 9,
        "committer": "lakefs-user",
    }
    client.diff_result = {
        "results": [
            {"path": "added.txt", "type": "added", "path_type": "object", "size_bytes": 5},
            {"path": "weights.bin", "type": "changed", "path_type": "object", "size_bytes": 9},
            {"path": "large.txt", "type": "removed", "path_type": "object", "size_bytes": None},
            {"path": "broken.txt", "type": "changed", "path_type": "object", "size_bytes": 3},
        ]
    }
    client.stat_results = {
        ("abc", "added.txt"): {"size_bytes": 5, "checksum": "sha256:added"},
        ("abc", "weights.bin"): {"size_bytes": 9, "checksum": "plain-sha"},
        ("parent-1", "weights.bin"): {"size_bytes": 4, "checksum": "sha256:previous"},
        ("parent-1", "large.txt"): {"size_bytes": 1000000, "checksum": "sha256:large"},
    }
    client.stat_errors = {
        ("abc", "broken.txt"): RuntimeError("no current stat"),
        ("parent-1", "broken.txt"): RuntimeError("no previous stat"),
    }
    client.object_results = {
        ("abc", "added.txt"): b"hello\n",
        ("abc", "weights.bin"): b"new-binary",
        ("parent-1", "weights.bin"): b"old-binary",
        ("parent-1", "large.txt"): b"very large",
    }
    client.object_errors = {
        ("abc", "broken.txt"): RuntimeError("object broken"),
    }

    diff = await commit_history.get_commit_diff("model", "alice", "demo", "abc")
    assert diff["parent_commit"] == "parent-1"
    assert diff["author"] == "lakefs-user"
    assert diff["files"][0]["sha256"] == "added"
    assert diff["files"][0]["diff"].startswith("--- a/added.txt")
    assert diff["files"][1]["is_lfs"] is True
    assert diff["files"][1]["sha256"] == "plain-sha"
    assert diff["files"][1]["previous_sha256"] == "previous"
    assert diff["files"][2]["diff"] is None
    assert diff["files"][3]["diff"] is None

    client.commit_error = RuntimeError("diff commit boom")
    failure = await commit_history.get_commit_diff("model", "alice", "demo", "abc")
    assert failure.status_code == 500


class LakeFSHistory:
    """External history may include commits the Hub database never recorded."""

    def __init__(self):
        self.head = HEAD
        self.pages = {None: {"results": [{"id": HEAD}], "pagination": {"has_more": False}}}
        self.calls = []

    async def get_branch(self, **kwargs):
        self.calls.append(("branch", kwargs))
        return {"commit_id": self.head}

    async def get_commit(self, **kwargs):
        self.calls.append(("commit", kwargs))
        return {"id": kwargs["commit_id"]}

    async def log_commits(self, **kwargs):
        self.calls.append(("log", kwargs))
        page = self.pages[kwargs.get("after")]
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
async def count_api(monkeypatch):
    monkeypatch.setattr(cfg.cache, "enabled", False)
    monkeypatch.setattr(cfg.cache, "local_enabled", True)
    await cache.close_cache()
    cache.reset_metrics()
    owner = make_user("alice")
    repo = make_repo(owner, "demo", lakefs_repo="count-model-demo")
    lakefs = LakeFSHistory()
    monkeypatch.setattr(commit_history, "get_lakefs_rest_client", lambda: lakefs)
    actor = SimpleNamespace(user=None)
    application = FastAPI()
    application.include_router(commit_history.router, prefix="/api")
    application.dependency_overrides[get_optional_user] = lambda: actor.user

    @application.exception_handler(RepoReadDeniedError)
    async def denied(_request, exc):
        return hf_repo_not_found(exc.repo_id, exc.repo_type)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as session:
        yield SimpleNamespace(session=session, lakefs=lakefs, owner=owner, repo=repo, actor=actor)
    await cache.close_cache()


async def test_count_comes_from_the_resolved_branch_history_not_database_rows(count_api):
    make_commit(count_api.repo, "unreachable-other-branch", branch="other")
    response = await count_api.session.get(f"{BASE}/feature/topic")

    assert response.status_code == 200
    assert response.json() == {"count": 1, "head": hub_oid(HEAD)}
    assert count_api.lakefs.calls[0] == (
        "branch", {"repository": "count-model-demo", "branch": "feature/topic"}
    )
    assert [call[1]["ref"] for call in count_api.lakefs.calls if call[0] == "log"] == [HEAD]


async def test_all_pages_are_counted_against_one_immutable_head(count_api):
    count_api.lakefs.pages = {
        None: {"results": [{"id": HEAD}, {"id": "b" * 64}],
               "pagination": {"has_more": True, "next_offset": "page-two"}},
        "page-two": {"results": [{"id": "c" * 64}, {"id": "d" * 64}],
                     "pagination": {"has_more": True, "next_offset": "page-three"}},
        "page-three": {"results": [{"id": "e" * 64}],
                       "pagination": {"has_more": False}},
    }
    response = await count_api.session.get(f"{BASE}/main")

    assert response.status_code == 200
    assert response.json() == {"count": 5, "head": hub_oid(HEAD)}
    calls = [args for operation, args in count_api.lakefs.calls if operation == "log"]
    assert [args.get("after") for args in calls] == [None, "page-two", "page-three"]
    assert {args["ref"] for args in calls} == {HEAD}
    assert all(not args.get("first_parent") for args in calls)


async def test_cache_follows_the_head_across_branches_new_commits_and_resets(count_api):
    for branch in ("main", "main", "feature/same-head"):
        response = await count_api.session.get(f"{BASE}/{branch}")
        assert response.json() == {"count": 1, "head": hub_oid(HEAD)}
    assert sum(operation == "log" for operation, _ in count_api.lakefs.calls) == 1

    # Full ids sharing the Hub's displayed prefix are still different histories.
    next_head = "a" * 40 + "b" * 24
    count_api.lakefs.head = next_head
    count_api.lakefs.pages[None]["results"] = [{"id": next_head}, {"id": HEAD}]
    response = await count_api.session.get(f"{BASE}/main")
    assert response.json() == {"count": 2, "head": hub_oid(next_head)}
    count_api.lakefs.head = HEAD  # a reset reuses the immutable old head's count
    response = await count_api.session.get(f"{BASE}/main")
    assert response.json() == {"count": 1, "head": hub_oid(HEAD)}
    assert sum(operation == "log" for operation, _ in count_api.lakefs.calls) == 2
    assert sum(operation == "branch" for operation, _ in count_api.lakefs.calls) == 5


@pytest.mark.parametrize("bad_page", [
    {"results": [], "pagination": {"has_more": False}},
    {"results": {"id": HEAD}, "pagination": {"has_more": False}},
    {"results": [{}], "pagination": {"has_more": False}},
    {"results": [{"id": HEAD}], "pagination": {"has_more": "false"}},
    {"results": [{"id": HEAD}], "pagination": {"has_more": True, "next_offset": ""}},
])
async def test_incomplete_history_is_an_error_and_never_cached_as_zero_or_partial(count_api, bad_page):
    count_api.lakefs.pages[None] = bad_page
    response = await count_api.session.get(f"{BASE}/main")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"
    assert not response.content  # no misleading {count: 0}

    count_api.lakefs.pages[None] = {"results": [{"id": HEAD}], "pagination": {"has_more": False}}
    response = await count_api.session.get(f"{BASE}/main")
    assert response.json()["count"] == 1


@pytest.mark.parametrize("head", [None, "", 17])
async def test_a_branch_without_an_immutable_head_does_not_count_anything(count_api, head):
    count_api.lakefs.head = head
    response = await count_api.session.get(f"{BASE}/main")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)


async def test_repeated_pagination_cursor_stops_without_caching_a_partial_total(count_api, monkeypatch):
    calls = 0

    async def repeated(**_kwargs):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise RuntimeError("test prevents an endless upstream pagination loop")
        return {"results": [{"id": HEAD}], "pagination": {"has_more": True, "next_offset": "again"}}

    monkeypatch.setattr(count_api.lakefs, "log_commits", repeated)
    response = await count_api.session.get(f"{BASE}/main")
    assert response.status_code == 500
    assert calls == 2


async def test_private_permissions_are_rechecked_before_a_cached_count(count_api):
    assert (await count_api.session.get(f"{BASE}/main")).status_code == 200
    count_api.repo.private = True
    count_api.repo.save()
    count_api.lakefs.calls.clear()

    for actor in (None, make_user("stranger")):
        count_api.actor.user = actor
        response = await count_api.session.get(f"{BASE}/main")
        assert response.status_code == 404
        assert response.headers["x-error-code"] == "RepoNotFound"
        assert count_api.lakefs.calls == []

    count_api.actor.user = count_api.owner
    response = await count_api.session.get(f"{BASE}/main")
    assert response.json()["count"] == 1
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)


async def test_organization_membership_and_revocation_use_real_sql(count_api):
    org = make_org("team")
    membership = UserOrganization.create(user=count_api.owner, organization=org, role="visitor")
    make_repo(org, "demo", private=True, lakefs_repo="count-org-demo")
    count_api.actor.user = count_api.owner
    url = "/api/models/team/demo/commit-count/main"
    assert (await count_api.session.get(url)).json()["count"] == 1
    membership.delete_instance()
    count_api.lakefs.calls.clear()
    response = await count_api.session.get(url)
    assert response.status_code == 404
    assert count_api.lakefs.calls == []


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
async def test_type_routes_and_missing_local_repositories(count_api, repo_type):
    make_repo(count_api.owner, "typed", repo_type=repo_type, lakefs_repo=f"count-{repo_type}")
    response = await count_api.session.get(f"/api/{repo_type}s/alice/typed/commit-count/main")
    assert response.json()["count"] == 1
    count_api.lakefs.calls.clear()
    response = await count_api.session.get(f"/api/{repo_type}s/upstream/absent/commit-count/main")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RepoNotFound"
    assert count_api.lakefs.calls == []  # no external fallback


async def test_cache_is_bounded_lru_and_isolated_by_lakefs_repository(count_api, monkeypatch):
    monkeypatch.setattr(cfg.cache, "local_max_entries", 2)
    await cache.close_cache()
    for name in ("second", "third"):
        make_repo(count_api.owner, name, lakefs_repo=f"count-{name}")

    async def request(name):
        return await count_api.session.get(f"/api/models/alice/{name}/commit-count/main")

    assert (await request("demo")).json()["count"] == 1
    count_api.lakefs.pages[None]["results"].append({"id": "b" * 64})
    assert (await request("second")).json()["count"] == 2  # same head, different LakeFS repo
    assert (await request("demo")).json()["count"] == 1  # demo is now most recently used
    assert (await request("third")).json()["count"] == 2
    count_api.lakefs.calls.clear()
    assert (await request("demo")).json()["count"] == 1
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)
    assert (await request("second")).json()["count"] == 2  # evicted and recounted
    assert sum(operation == "log" for operation, _ in count_api.lakefs.calls) == 1


async def test_commit_counts_are_invalidated_through_the_shared_cache(count_api):
    assert (await count_api.session.get(f"{BASE}/main")).json()["count"] == 1
    await cache.cache_invalidate_prefix("commit-count:")
    count_api.lakefs.pages[None]["results"].append({"id": "b" * 64})
    assert (await count_api.session.get(f"{BASE}/main")).json()["count"] == 2


@pytest.mark.parametrize("status, expected, code", [
    (404, 404, "RevisionNotFound"), (503, 500, "ServerError"),
    (400, 400, "BadRequest"), (409, 409, "BadRequest"),
])
async def test_a_failed_history_page_uses_shared_errors_and_can_retry(count_api, status, expected, code):
    count_api.lakefs.pages = {
        None: {"results": [{"id": HEAD}], "pagination": {"has_more": True, "next_offset": "more"}},
        "more": lakefs_error(status, "upstream failure"),
    }
    response = await count_api.session.get(f"{BASE}/main")
    assert response.status_code == expected
    assert response.headers["x-error-code"] == code
    count_api.lakefs.pages["more"] = {"results": [{"id": "b" * 64}], "pagination": {"has_more": False}}
    response = await count_api.session.get(f"{BASE}/main")
    assert response.json()["count"] == 2


@pytest.mark.parametrize("stage, status, expected, code", [
    ("branch", 503, 500, "ServerError"),
    ("branch", 400, 400, "BadRequest"),
    ("commit", 404, 404, "RevisionNotFound"),
    ("commit", 503, 500, "ServerError"),
])
async def test_revision_lookup_does_not_hide_upstream_failure(count_api, monkeypatch, stage, status, expected, code):
    async def branch(**_kwargs):
        raise lakefs_error(status if stage == "branch" else 404, "branch lookup failed")

    async def commit(**_kwargs):
        raise lakefs_error(status, "commit lookup failed")

    monkeypatch.setattr(count_api.lakefs, "get_branch", branch)
    monkeypatch.setattr(count_api.lakefs, "get_commit", commit)
    response = await count_api.session.get(f"{BASE}/missing")
    assert response.status_code == expected
    assert response.headers["x-error-code"] == code
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)


async def test_commit_revision_is_resolved_and_squashed_history_is_rejected(count_api, monkeypatch):
    async def missing_branch(**_kwargs):
        raise lakefs_error(404, "not a branch")

    async def resolved_commit(**_kwargs):
        return {"id": HEAD}

    monkeypatch.setattr(count_api.lakefs, "get_branch", missing_branch)
    monkeypatch.setattr(count_api.lakefs, "get_commit", resolved_commit)
    response = await count_api.session.get(f"{BASE}/{HEAD[:40]}")
    assert response.json() == {"count": 1, "head": hub_oid(HEAD)}

    count_api.repo.history_root = "f" * 64
    count_api.repo.save()

    async def unrelated(**_kwargs):
        return None

    async def no_refs(**_kwargs):
        return {"results": [], "pagination": {"has_more": False}}

    monkeypatch.setattr(count_api.lakefs, "find_merge_base", unrelated, raising=False)
    monkeypatch.setattr(count_api.lakefs, "list_branches", no_refs, raising=False)
    monkeypatch.setattr(count_api.lakefs, "list_tags", no_refs, raising=False)
    count_api.lakefs.calls.clear()
    response = await count_api.session.get(f"{BASE}/{HEAD[:40]}")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)


@pytest.mark.parametrize("stage", ["branch", "commit", "log"])
async def test_malformed_upstream_json_is_a_server_error_not_a_missing_revision(count_api, monkeypatch, stage):
    async def invalid_json(**_kwargs):
        raise JSONDecodeError("invalid upstream response", "oops", 0)

    async def missing_branch(**_kwargs):
        raise lakefs_error(404, "not a branch")

    if stage == "commit":
        monkeypatch.setattr(count_api.lakefs, "get_branch", missing_branch)
    monkeypatch.setattr(count_api.lakefs, {"branch": "get_branch", "commit": "get_commit", "log": "log_commits"}[stage], invalid_json)
    response = await count_api.session.get(f"{BASE}/main")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"


async def test_invalid_revision_url_keeps_revision_not_found(count_api, monkeypatch):
    async def invalid_branch(**_kwargs):
        raise httpx.InvalidURL("invalid revision URL")

    monkeypatch.setattr(count_api.lakefs, "get_branch", invalid_branch)
    response = await count_api.session.get(f"{BASE}/invalid-ref")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert not any(operation == "log" for operation, _ in count_api.lakefs.calls)
