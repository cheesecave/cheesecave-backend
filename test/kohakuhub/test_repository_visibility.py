"""Shared read policy stays consistent across lists, discovery, detail and feed."""

from datetime import timedelta
import json

import pytest

from kohakuhub import repository_discovery
from kohakuhub.api.repo.routers import discovery, info
from kohakuhub.api.utils import trending
from kohakuhub.auth.permissions import (
    RepoReadDeniedError,
    check_repo_read_permission,
    compile_repository_predicate,
    filter_readable_repositories,
    repository_read_predicate,
)
from kohakuhub.db import (
    DailyRepoStats,
    Repository,
    RepositoryFacet,
    RepositoryMetadata,
    UserFollow,
    UserOrganization,
    utcnow,
)
from test.kohakuhub.test_social import STAMP, catalog, commit, feed, repository


@pytest.fixture
def visible_catalog(catalog, monkeypatch):
    extra = [RepositoryMetadata, RepositoryFacet, DailyRepoStats]
    with catalog.database.bind_ctx(extra):
        catalog.database.create_tables(extra)
        catalog.app.include_router(discovery.router, prefix="/api")
        monkeypatch.setattr(repository_discovery, "schedule_indexing", lambda scope: None)
        monkeypatch.setattr(info, "get_lakefs_client", lambda: object())
        rows = {
            "public": repository(catalog, name="public"),
            "own": repository(catalog, owner=catalog.viewer, private=True, name="own"),
            "impostor": repository(catalog, owner=catalog.author, private=True, name="impostor"),
            "org": repository(catalog, owner=catalog.org, private=True, name="org"),
        }
        # Neither old/mismatched namespace text nor a user's own username grants access.
        rows["own"].namespace = "author"
        rows["own"].full_id = "author/own"
        rows["own"].save()
        rows["impostor"].namespace = "viewer"
        rows["impostor"].full_id = "viewer/impostor"
        rows["impostor"].save()
        for key, repo in rows.items():
            commit(catalog, repo)
            RepositoryMetadata.create(
                repository=repo,
                state="ready",
                metadata=json.dumps({"license": [key]}),
                checked_at=utcnow(),
            )
            RepositoryFacet.create(repository=repo, key="license", value=key)
        for user in (catalog.viewer, catalog.outsider):
            UserFollow.create(follower=user, followed=catalog.author)
            UserFollow.create(follower=user, followed=catalog.org)
        catalog.rows = rows
        yield catalog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "viewer_name,keys",
    [
        (None, {"public"}),
        ("viewer", {"public", "own", "org"}),
        ("author", {"public", "impostor"}),
        ("outsider", {"public"}),
    ],
)
async def test_ordinary_discovery_detail_feed_share_owner_policy(
    visible_catalog, viewer_name, keys
):
    catalog = visible_catalog
    UserOrganization.create(user=catalog.viewer, organization=catalog.org, role="visitor")
    user = getattr(catalog, viewer_name) if viewer_name else None
    catalog.auth["user"] = user
    expected = {catalog.rows[key].full_id for key in keys}
    ordinary = await info._list_repos_internal("model", user=user, fallback=False)
    assert {row["id"] for row in ordinary} == expected
    response = catalog.session.get("/api/models/discover", params={"sort": "recent"})
    assert response.status_code == 200
    discover = response.json()
    assert {row["id"] for row in discover["items"]} == expected
    assert discover["total"] == discover["indexing"]["total"] == len(expected)
    licenses = next(facet for facet in discover["facets"] if facet["key"] == "license")
    assert {row["value"] for row in licenses["options"]} == keys
    for key, repo in catalog.rows.items():
        if key in keys:
            assert check_repo_read_permission(repo, user)
        else:
            with pytest.raises(RepoReadDeniedError):
                check_repo_read_permission(repo, user)
    assert check_repo_read_permission(catalog.rows["impostor"], None, is_admin=True)
    if user is None:
        assert catalog.session.get("/api/workspace/feed").status_code == 401
    elif viewer_name == "author":
        # The actor's own real commits are personal interests, independent of follows.
        assert {row["repository"]["id"] for row in feed(catalog)["items"]} == expected
    else:
        assert {row["repository"]["id"] for row in feed(catalog)["items"]} == expected
    if user is not None:
        self_expected = {
            "viewer": {"author/own"},
            "author": {"author/public", "viewer/impostor"},
            "outsider": set(),
        }[viewer_name]
        assert {
            row["repository"]["id"] for row in feed(catalog, scope="self")["items"]
        } == self_expected


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["visitor", "member", "admin", "super-admin"])
async def test_member_exit_and_namespace_author_filter_remain_live(visible_catalog, role):
    catalog = visible_catalog
    membership = UserOrganization.create(user=catalog.viewer, organization=catalog.org, role=role)
    # Build once, execute twice: membership is a live subquery, not a cached Python list.
    query = filter_readable_repositories(Repository.select(), catalog.viewer)
    assert catalog.rows["org"].id in {row.id for row in query}
    author_rows = await info._list_repos_internal(
        "model", author="author", user=catalog.viewer, fallback=False
    )
    assert {row["id"] for row in author_rows} == {"author/public", "author/own"}
    overview = await info.list_user_repos.__wrapped__(
        "viewer", request=None, user=catalog.viewer, limit=100, sort="recent", fallback=False
    )
    assert overview["models"] == []  # Namespace matches viewer, owner does not.
    membership.delete_instance()
    assert catalog.rows["org"].id not in {row.id for row in query.clone()}
    assert all(row["repository"]["id"] != "org/org" for row in feed(catalog)["items"])
    with pytest.raises(RepoReadDeniedError):
        check_repo_read_permission(catalog.rows["org"], catalog.viewer)
    result = catalog.session.get("/api/models/discover").json()
    assert {row["id"] for row in result["items"]} == {"author/public", "author/own"}
    assert result["total"] == result["indexing"]["total"] == 2


def test_compiled_alias_policy_and_feed_cursor_caps_recheck_membership(visible_catalog):
    catalog = visible_catalog
    membership = UserOrganization.create(
        user=catalog.viewer, organization=catalog.org, role="visitor"
    )
    repo = catalog.rows["org"]
    for index in range(105):
        commit(catalog, repo, stamp=STAMP + timedelta(days=1), sha=f"change-{index}")
    sql, params = compile_repository_predicate(
        repository_read_predicate(catalog.viewer, Repository.alias("r")), catalog.database
    )
    assert f"'{catalog.viewer.username}'" not in sql and "r" in sql
    actual = {
        row[0]
        for row in catalog.database.execute_sql(
            "SELECT r.id FROM repository r WHERE " + sql, params
        ).fetchall()
    }
    assert actual == {
        row.id for row in filter_readable_repositories(Repository.select(), catalog.viewer)
    }
    first = feed(catalog, limit=1)
    assert first["items"][0]["repository"]["id"] == repo.full_id
    membership.delete_instance()
    later = feed(catalog, limit=100, cursor=first["next_cursor"])
    assert {row["repository"]["id"] for row in later["items"]} == {"author/public", "author/own"}
    assert not later["has_more"]
    # Starting a new cursor excludes all inaccessible IDs from caps and has_more.
    catalog.auth["user"] = catalog.outsider
    result = feed(catalog, limit=100)
    assert len(result["items"]) == 2 and not result["has_more"]


@pytest.mark.asyncio
async def test_public_trending_filters_author_and_visibility_before_limit(
    visible_catalog, monkeypatch
):
    catalog = visible_catalog
    other = repository(catalog, owner=catalog.outsider, name="popular")
    commit(catalog, other)
    monkeypatch.setattr(
        trending,
        "calculate_trending_scores",
        lambda rt, days=7: {
            catalog.rows["impostor"].id: 1000,
            other.id: 900,
            catalog.rows["own"].id: 800,
            catalog.rows["public"].id: 1,
        },
    )
    result = await info._list_repos_internal(
        "model", author="author", user=catalog.viewer, limit=1, sort="trending", fallback=False
    )
    assert [row["id"] for row in result] == ["author/public"]
    assert trending.get_trending_repositories("model", limit=1)[0].id == other.id
    monkeypatch.setattr(trending, "calculate_trending_scores", lambda rt, days=7: {})
    result = await info._list_repos_internal(
        "model", author="author", user=catalog.viewer, limit=1, sort="trending", fallback=False
    )
    assert [row["id"] for row in result] == ["author/public"]
