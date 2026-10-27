"""Branch history counts use real repository permissions and mocked LakeFS only."""

from json import JSONDecodeError
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from kohakuhub.api.commit.routers import history
from kohakuhub.api.commit import count as counts
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import RepoReadDeniedError
from kohakuhub.api.repo.utils.hf import hf_repo_not_found
from kohakuhub.db import UserOrganization
from kohakuhub.utils.commit_id import hub_oid
from test.kohakuhub.support.factories import make_commit, make_org, make_repo, make_user
from test.kohakuhub.support.fakes import lakefs_error

pytestmark = pytest.mark.usefixtures("db_scope")
HEAD = "a" * 64
BASE = "/api/models/alice/demo/commit-count"


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
    counts._counts.clear()
    owner = make_user("alice")
    repo = make_repo(owner, "demo", lakefs_repo="count-model-demo")
    lakefs = LakeFSHistory()
    monkeypatch.setattr(history, "get_lakefs_rest_client", lambda: lakefs)
    actor = SimpleNamespace(user=None)
    application = FastAPI()
    application.include_router(history.router, prefix="/api")
    application.dependency_overrides[get_optional_user] = lambda: actor.user

    @application.exception_handler(RepoReadDeniedError)
    async def denied(_request, exc):
        return hf_repo_not_found(exc.repo_id, exc.repo_type)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver"
    ) as session:
        yield SimpleNamespace(session=session, lakefs=lakefs, owner=owner, repo=repo, actor=actor)


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
    monkeypatch.setattr(counts, "COMMIT_COUNTS_KEPT", 2)
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
