"""Follow and derived activity contracts on the shared real-database fixtures.

The app runs through TestClient (its own thread), so rows must be committed: the catalog
uses ``db_dual``, so every test runs on SQLite and on PostgreSQL.
"""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase
import pytest

from kohakuhub.api import social
from kohakuhub.auth.dependencies import get_current_user, get_optional_user
from kohakuhub.db import Commit, Repository, RepositoryLike, User, UserFollow, UserOrganization
from test.kohakuhub.support.db import peer_connection
from test.kohakuhub.support.factories import make_commit, make_org, make_repo, make_user

STAMP = datetime(2025, 1, 1, 12)


@pytest.fixture
def catalog(db_dual):
    viewer = make_user("viewer")
    author = make_user("author", full_name="An author")
    outsider = make_user("outsider")
    org = make_org("org")
    auth = {"user": viewer}
    app = FastAPI()
    app.include_router(social.router, prefix="/api")

    def authenticated():
        if auth["user"] is None:
            raise HTTPException(401, "Login required")
        return auth["user"]

    app.dependency_overrides[get_current_user] = authenticated
    app.dependency_overrides[get_optional_user] = lambda: auth["user"]
    with TestClient(app) as session:
        yield SimpleNamespace(
            database=db_dual,
            viewer=viewer,
            author=author,
            outsider=outsider,
            org=org,
            auth=auth,
            session=session,
            app=app,
        )


def repository(catalog, owner=None, private=False, repo_type="model", name="repo", stamp=STAMP):
    return make_repo(
        owner or catalog.author,
        name,
        repo_type=repo_type,
        private=private,
        created_at=stamp,
    )


def commit(catalog, repo, author=None, stamp=STAMP, branch="main", sha=None):
    return make_commit(
        repo,
        sha or uuid4().hex,
        author=author or catalog.author,
        branch=branch,
        message="Real change",
        created_at=stamp,
    )


def feed(catalog, **params):
    response = catalog.session.get("/api/workspace/feed", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_follow_auth_idempotency_orgs_self_and_local_only(catalog):
    session = catalog.session
    catalog.auth["user"] = None
    assert session.get("/api/users/author/follow").json() == {
        "username": "author",
        "is_org": False,
        "following": False,
        "can_follow": False,
        "followers_count": 0,
        "following_count": 0,
    }
    assert session.put("/api/users/author/follow").status_code == 401
    assert session.get("/api/workspace/feed").status_code == 401
    assert session.get("/api/users/remote/follow").status_code == 404
    catalog.auth["user"] = catalog.org
    assert session.put("/api/users/author/follow").status_code == 403
    catalog.auth["user"] = catalog.viewer
    assert session.put("/api/users/viewer/follow").status_code == 400
    for _ in range(2):
        assert session.put("/api/users/author/follow").json()["following"]
    assert UserFollow.select().count() == 1
    assert session.put("/api/users/org/follow").json()["is_org"]
    assert session.get("/api/users/viewer/follow").json()["following_count"] == 2
    for _ in range(2):
        assert not session.delete("/api/users/author/follow").json()["following"]
    catalog.author.is_active = False
    catalog.author.save()
    assert not session.get("/api/users/author/follow").json()["can_follow"]
    assert session.put("/api/users/author/follow").status_code == 400


def test_schema_self_check_unique_and_cascade(catalog):
    with pytest.raises(IntegrityError), catalog.database.atomic():
        UserFollow.create(follower=catalog.viewer, followed=catalog.viewer)
    UserFollow.create(follower=catalog.viewer, followed=catalog.author)
    with pytest.raises(IntegrityError), catalog.database.atomic():
        UserFollow.create(follower=catalog.viewer, followed=catalog.author)
    catalog.author.delete_instance()
    assert UserFollow.select().count() == 0


def test_concurrent_first_follow_writes_are_idempotent(catalog):
    def write(_):
        try:
            return social.follow("author", Response(), catalog.viewer)
        finally:
            catalog.database.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(12)))
    assert all(result["following"] and result["followers_count"] == 1 for result in results)
    assert UserFollow.select().count() == 1


def test_lists_minimal_profiles_pagination_and_cursor_binding(catalog):
    for index in range(105):
        target = User.create(
            username=f"target{index}",
            normalized_name=f"target{index}",
            email=f"private{index}@test.invalid",
        )
        UserFollow.create(follower=catalog.viewer, followed=target, created_at=STAMP)
    seen = []
    cursor = None
    while True:
        result = catalog.session.get(
            "/api/users/viewer/following",
            params={"limit": 17, **({"cursor": cursor} if cursor else {})},
        ).json()
        seen += result["items"]
        if not result["has_more"]:
            break
        cursor = result["next_cursor"]
    assert len(seen) == len({row["username"] for row in seen}) == 105
    assert all(
        set(row) == {"username", "full_name", "is_org", "avatar_url", "followed_at"}
        and row["followed_at"].endswith("Z")
        for row in seen
    )
    first = catalog.session.get("/api/users/viewer/following", params={"limit": 1}).json()
    assert (
        catalog.session.get(
            "/api/users/viewer/followers", params={"cursor": first["next_cursor"]}
        ).status_code
        == 422
    )


def test_real_authors_creation_null_scopes_and_type(catalog):
    org_repo = repository(catalog, owner=catalog.org, repo_type="dataset")
    own_repo = repository(catalog, owner=catalog.viewer, name="own")
    foreign = repository(catalog, owner=catalog.outsider, name="foreign")
    commit(catalog, org_repo)
    commit(catalog, own_repo, author=catalog.outsider)
    commit(catalog, foreign, author=catalog.viewer)
    commit(catalog, org_repo, branch="dev")
    RepositoryLike.create(
        repository=org_repo, user=catalog.author, created_at=STAMP + timedelta(hours=1)
    )
    UserFollow.create(follower=catalog.viewer, followed=catalog.author)
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    UserOrganization.create(user=catalog.viewer, organization=catalog.org, role="visitor")
    following = feed(catalog, scope="following")
    assert len(following["items"]) == 3
    assert following["items"][0]["kind"] == "like"
    assert following["items"][0]["actor"]["username"] == "author"
    assert (
        next(row for row in following["items"] if row["kind"] == "commit")["actor"]["username"]
        == "author"
    )
    assert next(row for row in following["items"] if row["kind"] == "repo_created")["actor"] is None
    personal = feed(catalog, scope="personal")
    assert len(personal["items"]) == 6
    all_items = feed(catalog)["items"]
    assert len(all_items) == len({row["id"] for row in all_items}) == 5
    assert len(feed(catalog, repo_type="dataset")["items"]) == 3


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
def test_feed_repository_stats_are_current_and_distinct_from_event_times(catalog, repo_type):
    repo = repository(catalog, owner=catalog.viewer, repo_type=repo_type)
    first = commit(catalog, repo, author=catalog.viewer, stamp=STAMP + timedelta(days=1))
    liked = RepositoryLike.create(
        repository=repo, user=catalog.viewer, created_at=STAMP + timedelta(days=2)
    )
    latest = commit(catalog, repo, author=catalog.viewer, stamp=STAMP + timedelta(days=3))
    commit(catalog, repo, stamp=STAMP + timedelta(days=9), branch="dev")
    Repository.update(likes_count=17, downloads=429).where(Repository.id == repo.id).execute()

    result = feed(catalog, scope="self", repo_type=repo_type)
    assert [row["id"] for row in result["items"]] == [
        f"commit:{latest.id}",
        f"like:{liked.id}",
        f"commit:{first.id}",
        f"repo_created:{repo.id}",
    ]
    expected = {
        "id": repo.full_id,
        "type": repo_type,
        "private": False,
        "lastModified": "2025-01-04T12:00:00Z",
        "likes": 17,
        "downloads": 429,
    }
    assert all(row["repository"] == expected for row in result["items"])
    assert [row["created_at"] for row in result["items"]] == [
        "2025-01-04T12:00:00Z",
        "2025-01-03T12:00:00Z",
        "2025-01-02T12:00:00Z",
        "2025-01-01T12:00:00Z",
    ]

    Repository.update(likes_count=18, downloads=430).where(Repository.id == repo.id).execute()
    refreshed = feed(catalog, scope="self", event_type="like")
    assert len(refreshed["items"]) == 1
    assert refreshed["items"][0]["repository"] == {**expected, "likes": 18, "downloads": 430}
    assert refreshed["items"][0]["created_at"] == result["items"][1]["created_at"]


def test_feed_repository_stats_empty_main_and_batch_query_count(catalog, monkeypatch):
    repo = repository(catalog, owner=catalog.viewer)
    commit(catalog, repo, stamp=STAMP + timedelta(days=1), branch="dev")
    queries = []
    execute_sql = catalog.database.execute_sql

    def record(sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("SELECT"):
            queries.append(sql)
        return execute_sql(sql, *args, **kwargs)

    monkeypatch.setattr(catalog.database, "execute_sql", record)
    first = feed(catalog, scope="self")
    assert first["items"][0]["repository"] == {
        "id": repo.full_id,
        "type": "model",
        "private": False,
        "lastModified": "2025-01-01T12:00:00Z",
        "likes": 0,
        "downloads": 0,
    }
    first_count = len(queries)
    for index in range(8):
        additional = repository(catalog, owner=catalog.viewer, name=f"additional{index}")
        commit(catalog, additional, author=catalog.viewer, stamp=STAMP + timedelta(days=1))
    queries.clear()
    larger = feed(catalog, scope="self", limit=100)
    assert len(larger["items"]) == 17
    assert len(queries) == first_count
    assert all(
        row["repository"]["lastModified"] == "2025-01-02T12:00:00Z"
        for row in larger["items"]
        if row["repository"]["id"] != repo.full_id
    )


def test_privacy_before_limit_and_current_org_membership(catalog):
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    public = repository(catalog, owner=catalog.org, name="public")
    for index in range(110):
        private = repository(
            catalog,
            owner=catalog.org,
            private=True,
            name=f"secret{index}",
            stamp=STAMP + timedelta(days=1),
        )
        commit(catalog, private, stamp=STAMP + timedelta(days=1))
    result = feed(catalog, scope="following", limit=1)
    assert [row["repository"]["id"] for row in result["items"]] == [public.full_id]
    assert not result["has_more"] and result["next_cursor"] is None
    membership = UserOrganization.create(
        user=catalog.viewer, organization=catalog.org, role="visitor"
    )
    first = feed(catalog, scope="following", limit=1)
    assert first["items"][0]["repository"]["private"] and first["has_more"]
    membership.delete_instance()
    later = feed(catalog, scope="following", limit=1, cursor=first["next_cursor"])
    assert [row["repository"]["id"] for row in later["items"]] == [public.full_id]
    assert not later["has_more"]


def test_tied_multisource_cursor_after_100_snapshot_and_bad_context(catalog):
    repo = repository(catalog, owner=catalog.viewer)
    for _ in range(120):
        commit(catalog, repo, author=catalog.viewer)
    RepositoryLike.create(repository=repo, user=catalog.viewer, created_at=STAMP)
    first = feed(catalog, limit=17)
    assert first["items"][0]["kind"] == "like"
    new_commit = commit(catalog, repo, author=catalog.viewer, stamp=STAMP + timedelta(days=5))
    cursor = first["next_cursor"]
    seen = list(first["items"])
    while cursor:
        result = feed(catalog, limit=17, cursor=cursor)
        seen += result["items"]
        cursor = result["next_cursor"]
    assert len(seen) == len({row["id"] for row in seen}) == 122
    assert f"commit:{new_commit.id}" not in {row["id"] for row in seen}
    assert [row["kind"] for row in seen[-2:]] == ["commit", "repo_created"]
    for params in ({"scope": "following"}, {"repo_type": "dataset"}, {"cursor": "not-json"}):
        response = catalog.session.get(
            "/api/workspace/feed", params={"cursor": first["next_cursor"], **params}
        )
        assert response.status_code == 422
    catalog.auth["user"] = catalog.outsider
    assert (
        catalog.session.get(
            "/api/workspace/feed", params={"cursor": first["next_cursor"]}
        ).status_code
        == 422
    )


def test_cursor_boundaries_exclude_invisible_source_ids(catalog):
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    public = repository(catalog, owner=catalog.org)
    public_commit = commit(catalog, public)
    hidden = repository(catalog, owner=catalog.org, private=True, name="hidden")
    commit(catalog, hidden)
    RepositoryLike.create(repository=hidden, user=catalog.author)
    first = feed(catalog, scope="following", limit=1)
    state = social._decode(
        first["next_cursor"], ["feed", catalog.viewer.id, "following", "all", "actor-v2", "all"]
    )
    assert state["caps"] == [public.id, public_commit.id, 0]


def test_utc_and_history_removals_and_transfer(catalog):
    repo = repository(catalog, owner=catalog.viewer)
    # PostgreSQL source timestamps have no zone: use the established naive UTC convention there.
    stamp = (
        STAMP.replace(tzinfo=timezone.utc)
        if isinstance(catalog.database, PostgresqlDatabase)
        else datetime(2025, 1, 1, 20, tzinfo=timezone(timedelta(hours=8)))
    )
    change = commit(catalog, repo, author=catalog.author, stamp=stamp)
    like = RepositoryLike.create(repository=repo, user=catalog.viewer, created_at=STAMP)
    result = feed(catalog)
    assert all(
        row["created_at"].startswith("2025-01-01T12:00:00") and row["created_at"].endswith("Z")
        for row in result["items"]
    )
    like.delete_instance()
    assert all(row["kind"] != "like" for row in feed(catalog)["items"])
    repo.owner = catalog.org
    repo.namespace = "org"
    repo.full_id = "org/repo"
    repo.save()
    UserFollow.create(follower=catalog.viewer, followed=catalog.author)
    result = feed(catalog, scope="following")
    assert len(result["items"]) == 1 and result["items"][0]["actor"]["username"] == "author"
    assert result["items"][0]["namespace"] == {"username": "org", "is_org": True}
    change.delete_instance()
    assert feed(catalog, scope="following")["items"] == []
    repo.delete_instance()
    assert Commit.select().count() == RepositoryLike.select().count() == 0


def test_read_snapshot_survives_concurrent_cascade(catalog, monkeypatch):
    if isinstance(catalog.database, SqliteDatabase):
        pytest.skip("Concurrent deletion while reading is tested against PostgreSQL MVCC")
    repo = repository(catalog, owner=catalog.viewer)
    commit(catalog, repo, author=catalog.viewer)
    other = peer_connection(catalog.database)
    real_execute = catalog.database.execute_sql
    deleted = []

    def execute(sql, params=None, *args, **kwargs):
        result = real_execute(sql, params, *args, **kwargs)
        if sql.startswith("SELECT * FROM (") and "UNION ALL" in sql and not deleted:
            other.execute_sql("DELETE FROM repository WHERE id=%s", (repo.id,))
            deleted.append(True)
        return result

    monkeypatch.setattr(catalog.database, "execute_sql", execute)
    try:
        result = feed(catalog)
        assert len(result["items"]) == 2
        assert all(row["repository"]["id"] == repo.full_id for row in result["items"])
        assert feed(catalog)["items"] == []
    finally:
        other.close()


def test_self_scope_includes_only_self_owned_or_self_actor_visible_events(catalog):
    UserOrganization.create(user=catalog.viewer, organization=catalog.org, role="visitor")
    own = repository(catalog, owner=catalog.viewer, name="own")
    own_commit = commit(catalog, own, author=catalog.author)
    org_repo = repository(catalog, owner=catalog.org, private=True, name="org-work")
    self_commit = commit(catalog, org_repo, author=catalog.viewer)
    org_commit = commit(catalog, org_repo, author=catalog.author)
    self_like = RepositoryLike.create(repository=org_repo, user=catalog.viewer, created_at=STAMP)
    outside = repository(catalog, owner=catalog.outsider, name="outside")
    external_self_commit = commit(catalog, outside, author=catalog.viewer)
    hidden = repository(catalog, owner=catalog.outsider, private=True, name="hidden")
    commit(catalog, hidden, author=catalog.viewer)
    result = feed(catalog, scope="self")
    assert {row["id"] for row in result["items"]} == {
        f"repo_created:{own.id}",
        f"commit:{self_commit.id}",
        f"like:{self_like.id}",
        f"commit:{external_self_commit.id}",
    }
    personal = {row["id"] for row in feed(catalog, scope="personal")["items"]}
    assert personal == {row["id"] for row in result["items"]} | {
        f"commit:{own_commit.id}",
        f"repo_created:{org_repo.id}",
        f"commit:{org_commit.id}",
    }
    assert {row["id"] for row in feed(catalog)["items"]} == personal - {f"commit:{own_commit.id}"}
    first = feed(catalog, scope="self", limit=1)
    assert (
        catalog.session.get(
            "/api/workspace/feed", params={"scope": "personal", "cursor": first["next_cursor"]}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role", ["visitor", "member", "admin", "super-admin"])
def test_organization_scope_precise_and_all_current_member_roles(catalog, role):
    UserOrganization.create(user=catalog.viewer, organization=catalog.org, role=role)
    repo = repository(catalog, owner=catalog.org, private=True)
    change = commit(catalog, repo)
    like = RepositoryLike.create(repository=repo, user=catalog.author, created_at=STAMP)
    other_org = User.create(username="second-org", normalized_name="second-org", is_org=True)
    UserOrganization.create(user=catalog.viewer, organization=other_org, role=role)
    outside = repository(catalog, owner=other_org, private=True)
    commit(catalog, outside, author=catalog.author)
    personal = repository(catalog, owner=catalog.viewer, name="personal")
    commit(catalog, personal, author=catalog.author)
    # Actual org member/commit author activity in other namespaces is excluded.
    UserOrganization.create(user=catalog.author, organization=catalog.org, role="member")
    result = feed(catalog, scope="organization", organization="org")
    assert {row["id"] for row in result["items"]} == {
        f"repo_created:{repo.id}",
        f"commit:{change.id}",
        f"like:{like.id}",
    }
    assert {row["repository"]["id"] for row in result["items"]} == {repo.full_id}
    assert len(feed(catalog, scope="organization", organization="second-org")["items"]) == 2
    empty = User.create(username="empty-org", normalized_name="empty-org", is_org=True)
    UserOrganization.create(user=catalog.viewer, organization=empty, role=role)
    assert feed(catalog, scope="organization", organization="empty-org") == {
        "items": [],
        "has_more": False,
        "next_cursor": None,
    }


def test_organization_scope_requires_membership_valid_parameters_and_bound_cursor(catalog):
    session = catalog.session
    for name in ("org", "missing", "author"):
        assert (
            session.get(
                "/api/workspace/feed", params={"scope": "organization", "organization": name}
            ).status_code
            == 403
        )
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    assert (
        session.get(
            "/api/workspace/feed", params={"scope": "organization", "organization": "org"}
        ).status_code
        == 403
    )

    for params in (
        {"scope": "organization"},
        {"scope": "organization", "organization": ""},
        {"scope": "self", "organization": "org"},
        {"scope": "all", "organization": "org"},
        {"scope": "personal", "organization": "org"},
        {"scope": "following", "organization": "org"},
    ):
        assert session.get("/api/workspace/feed", params=params).status_code == 400
    membership = UserOrganization.create(
        user=catalog.viewer, organization=catalog.org, role="visitor"
    )
    repo = repository(catalog, owner=catalog.org, private=True)
    for index in range(105):
        commit(catalog, repo, sha=f"org-change-{index}")
    # A later source outside the selected organization must not affect its caps.
    outside = repository(catalog, owner=catalog.viewer, name="outside")
    commit(catalog, outside)
    first = feed(catalog, scope="organization", organization="org", limit=17)
    state = social._decode(
        first["next_cursor"],
        ["feed", catalog.viewer.id, "organization", "all", "actor-v2", "all", catalog.org.id],
    )
    assert state["caps"] == [repo.id, 105, 0]
    seen = list(first["items"])
    cursor = first["next_cursor"]
    while cursor:
        page = feed(catalog, scope="organization", organization="org", cursor=cursor, limit=17)
        seen += page["items"]
        cursor = page["next_cursor"]
    assert len(seen) == len({row["id"] for row in seen}) == 106
    other_org = User.create(username="second-org", normalized_name="second-org", is_org=True)
    UserOrganization.create(user=catalog.viewer, organization=other_org, role="member")
    assert (
        session.get(
            "/api/workspace/feed",
            params={
                "scope": "organization",
                "organization": "second-org",
                "cursor": first["next_cursor"],
            },
        ).status_code
        == 422
    )
    assert (
        session.get(
            "/api/workspace/feed", params={"scope": "self", "cursor": first["next_cursor"]}
        ).status_code
        == 422
    )
    membership.delete_instance()
    assert (
        session.get(
            "/api/workspace/feed",
            params={"scope": "organization", "organization": "org", "cursor": first["next_cursor"]},
        ).status_code
        == 403
    )
    assert (
        session.get(
            "/api/workspace/feed", params={"scope": "organization", "organization": "org"}
        ).status_code
        == 403
    )


def test_event_categories_are_disjoint_full_pages_and_cursor_bound(catalog):
    repository_events, like_events, model_events = set(), set(), set()
    for index in range(106):
        repo_type = ("model", "dataset", "space")[index % 3]
        repo = repository(catalog, owner=catalog.viewer, repo_type=repo_type, name=f"work-{index}")
        change = commit(catalog, repo, author=catalog.viewer)
        like = RepositoryLike.create(
            repository=repo, user=catalog.viewer, created_at=STAMP + timedelta(days=1)
        )
        events = {f"repo_created:{repo.id}", f"commit:{change.id}"}
        repository_events |= events
        like_events.add(f"like:{like.id}")
        if repo_type == "model":
            model_events |= events
    hidden = repository(catalog, owner=catalog.outsider, private=True, name="hidden")
    commit(catalog, hidden, author=catalog.viewer)
    RepositoryLike.create(
        repository=hidden, user=catalog.viewer, created_at=STAMP + timedelta(days=2)
    )

    def paginate(event_type, repo_type="all"):
        items, cursor, pages = [], None, []
        while True:
            params = {
                "scope": "self",
                "event_type": event_type,
                "repo_type": repo_type,
                "limit": 17,
            }
            if cursor:
                params["cursor"] = cursor
            page = feed(catalog, **params)
            items += page["items"]
            pages.append(page)
            if not page["has_more"]:
                break
            assert len(page["items"]) == 17
            cursor = page["next_cursor"]
        assert len(items) == len({row["id"] for row in items})
        return items, pages

    repos, repo_pages = paginate("repository")
    likes, like_pages = paginate("like")
    models, _ = paginate("repository", "model")
    all_events, _ = paginate("all")
    assert {row["id"] for row in repos} == repository_events
    assert {row["id"] for row in likes} == like_events
    assert {row["id"] for row in models} == model_events
    assert {row["id"] for row in all_events} == repository_events | like_events
    assert {row["kind"] for row in repos} == {"repo_created", "commit"}
    assert {row["kind"] for row in likes} == {"like"}
    assert all(row["repository"]["type"] == "model" for row in models)
    # The API default keeps all event kinds; only explicit category excludes likes.
    assert (
        feed(catalog, scope="self", limit=17)["items"]
        == feed(catalog, scope="self", event_type="all", limit=17)["items"]
    )
    assert {
        row["kind"] for row in feed(catalog, scope="self", repo_type="model", limit=17)["items"]
    } == {"like"}
    repo_cursor = repo_pages[0]["next_cursor"]
    state = social._decode(
        repo_cursor, ["feed", catalog.viewer.id, "self", "all", "actor-v2", "repository"]
    )
    assert state["caps"] == [106, 106, 0]
    like_cursor = like_pages[0]["next_cursor"]
    state = social._decode(
        like_cursor, ["feed", catalog.viewer.id, "self", "all", "actor-v2", "like"]
    )
    assert state["caps"] == [0, 0, 106]
    for event_type, cursor in (
        ("like", repo_cursor),
        ("repository", like_cursor),
        ("all", repo_cursor),
    ):
        assert (
            catalog.session.get(
                "/api/workspace/feed",
                params={"scope": "self", "event_type": event_type, "cursor": cursor},
            ).status_code
            == 422
        )
    old_cursor = social._encode(
        {
            "v": 1,
            "context": ["feed", catalog.viewer.id, "self", "all"],
            "caps": [106, 106, 106],
            "last": [1735732800000, 2, 100],
        }
    )
    assert (
        catalog.session.get(
            "/api/workspace/feed", params={"scope": "self", "cursor": old_cursor}
        ).status_code
        == 422
    )
    assert (
        catalog.session.get("/api/workspace/feed", params={"event_type": "invalid"}).status_code
        == 422
    )


def test_personal_following_tracks_actors_not_other_peoples_actions_on_their_repo(catalog):
    UserFollow.create(follower=catalog.viewer, followed=catalog.author)
    followed_repo = repository(catalog, name="followed-person")
    # Namespace text does not identify either the owner or the action's actor.
    followed_repo.namespace = "outsider"
    followed_repo.full_id = "outsider/followed-person"
    followed_repo.save()
    own_repo = repository(catalog, owner=catalog.viewer, name="self")
    own_repo.namespace = "author"
    own_repo.full_id = "author/self"
    own_repo.save()
    other_commit = commit(catalog, followed_repo, author=catalog.outsider)
    other_like = RepositoryLike.create(
        repository=followed_repo, user=catalog.outsider, created_at=STAMP
    )
    followed_commit = commit(catalog, own_repo, author=catalog.author)
    followed_like = RepositoryLike.create(
        repository=own_repo, user=catalog.author, created_at=STAMP
    )
    stranger_commit = commit(catalog, own_repo, author=catalog.outsider)
    stranger_like = RepositoryLike.create(
        repository=own_repo, user=catalog.outsider, created_at=STAMP
    )
    following = feed(catalog, scope="following")
    expected = {
        f"repo_created:{followed_repo.id}",
        f"commit:{followed_commit.id}",
        f"like:{followed_like.id}",
    }
    assert {row["id"] for row in following["items"]} == expected
    assert all(row["actor"]["username"] == "author" for row in following["items"] if row["actor"])
    assert {row["id"] for row in feed(catalog, scope="self")["items"]} == {
        f"repo_created:{own_repo.id}"
    }
    assert {row["id"] for row in feed(catalog)["items"]} == expected | {
        f"repo_created:{own_repo.id}"
    }
    legacy = {row["id"] for row in feed(catalog, scope="personal")["items"]}
    assert {f"commit:{stranger_commit.id}", f"like:{stranger_like.id}"}.issubset(legacy)
    assert not {f"commit:{other_commit.id}", f"like:{other_like.id}"}.intersection(expected)
    assert {
        row["id"] for row in feed(catalog, scope="following", event_type="repository")["items"]
    } == {f"repo_created:{followed_repo.id}", f"commit:{followed_commit.id}"}
    assert {row["id"] for row in feed(catalog, scope="following", event_type="like")["items"]} == {
        f"like:{followed_like.id}"
    }


def test_org_following_and_membership_aggregate_other_actors_but_categories_stay_separate(catalog):
    org_repo = repository(catalog, owner=catalog.org)
    change = commit(catalog, org_repo, author=catalog.outsider)
    like = RepositoryLike.create(repository=org_repo, user=catalog.outsider, created_at=STAMP)
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    expected = {f"repo_created:{org_repo.id}", f"commit:{change.id}", f"like:{like.id}"}
    assert {row["id"] for row in feed(catalog, scope="following")["items"]} == expected
    UserFollow.delete().execute()
    membership = UserOrganization.create(
        user=catalog.viewer, organization=catalog.org, role="visitor"
    )
    assert {row["id"] for row in feed(catalog)["items"]} == expected
    assert feed(catalog, scope="self")["items"] == []
    assert {
        row["id"]
        for row in feed(catalog, scope="organization", organization="org", event_type="repository")[
            "items"
        ]
    } == {f"repo_created:{org_repo.id}", f"commit:{change.id}"}
    assert {
        row["id"]
        for row in feed(catalog, scope="organization", organization="org", event_type="like")[
            "items"
        ]
    } == {f"like:{like.id}"}
    org_repo.private = True
    org_repo.save()
    assert {row["id"] for row in feed(catalog)["items"]} == expected
    membership.delete_instance()
    UserFollow.create(follower=catalog.viewer, followed=catalog.org)
    for event_type in ("all", "repository", "like"):
        result = feed(catalog, event_type=event_type)
        assert result == {"items": [], "has_more": False, "next_cursor": None}
