"""Unit tests for the admin lookup (the typeahead behind every user / organization / repository picker), on real rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.admin.routers.commits as admin_commits
import kohakuhub.api.admin.routers.search as admin_search
from test.kohakuhub.support.factories import make_commit, make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


async def lookup(q="", kinds=("user", "org"), limit=8, repo_type=None):
    return (await admin_search.lookup_entities(q=q, kinds=list(kinds), limit=limit, repo_type=repo_type))["results"]


def names(results):
    return [r.get("username") or r.get("full_id") for r in results]


@pytest.mark.asyncio
async def test_a_number_finds_the_row_with_that_id_before_name_matches():
    target = make_user("zed")
    make_user(f"user{target.id}-fan")  # contains the number, but is not the id

    for typed in (str(target.id), f"#{target.id}"):
        hits = await lookup(typed)
        assert hits[0]["username"] == "zed"
        assert hits[0]["matched"] == "id"
        assert hits[0]["id"] == target.id
    assert "user%d-fan" % target.id in names(await lookup(str(target.id)))


@pytest.mark.asyncio
async def test_results_are_ranked_exact_then_prefix_then_contains_ignoring_case():
    make_user("malice")
    make_user("alice2")
    make_user("Alice")

    assert names(await lookup("alice")) == ["Alice", "alice2", "malice"]
    assert [r["matched"] for r in await lookup("alice")] == ["username"] * 3


@pytest.mark.asyncio
async def test_an_email_matches_and_says_so_and_an_exact_email_wins_over_a_name_hit():
    make_user("ops", email="park.lee@example.org")
    make_user("park.lee-fan", email="fan@example.org")

    by_part = await lookup("park.lee@")
    assert names(by_part) == ["ops"]
    assert by_part[0]["matched"] == "email"
    assert by_part[0]["email"] == "park.lee@example.org"

    exact = await lookup("park.lee@example.org")
    assert names(exact)[0] == "ops"


@pytest.mark.asyncio
async def test_like_wildcards_in_what_is_typed_are_literal():
    make_user("a_b")
    make_user("axb")
    make_user("100%pure")
    make_user("100xpure")

    assert names(await lookup("a_b")) == ["a_b"]
    assert names(await lookup("100%")) == ["100%pure"]


@pytest.mark.asyncio
async def test_kinds_select_users_organizations_or_both_and_an_org_has_no_email():
    make_user("lab-user")
    make_org("lab-org")

    assert names(await lookup("lab", kinds=("user",))) == ["lab-user"]
    org_hits = await lookup("lab", kinds=("org",))
    assert names(org_hits) == ["lab-org"]
    assert org_hits[0]["is_org"] is True and org_hits[0]["email"] is None
    assert sorted(names(await lookup("lab"))) == ["lab-org", "lab-user"]


@pytest.mark.asyncio
async def test_repositories_match_the_full_id_and_can_be_limited_to_a_type():
    owner = make_user("owner-a")
    make_repo(owner, "vision", repo_type="model")
    make_repo(owner, "vision", repo_type="dataset")
    make_repo(owner, "audio", repo_type="model")

    both = await lookup("owner-a/vis", kinds=("repo",))
    assert sorted((r["repo_type"], r["full_id"]) for r in both) == [
        ("dataset", "owner-a/vision"),
        ("model", "owner-a/vision"),
    ]
    only_models = await lookup("vision", kinds=("repo",), repo_type="model")
    assert [(r["repo_type"], r["namespace"], r["name"]) for r in only_models] == [("model", "owner-a", "vision")]
    assert all(r["kind"] == "repo" for r in both)


@pytest.mark.asyncio
async def test_empty_query_lists_the_first_rows_by_name_and_limit_is_honoured():
    for name in ("c-user", "a-user", "b-user"):
        make_user(name)
    assert names(await lookup("", limit=2)) == ["a-user", "b-user"]
    assert len(await lookup("-user", limit=1)) == 1


@pytest.mark.asyncio
async def test_an_unknown_kind_is_rejected():
    with pytest.raises(HTTPException) as bad:
        await lookup("x", kinds=("user", "team"))
    assert bad.value.status_code == 400


@pytest.mark.asyncio
async def test_commit_filter_on_an_unknown_repository_returns_nothing_not_everything():
    owner = make_user("committer")
    repo = make_repo(owner, "real")
    make_commit(repo, "c1", author=owner)

    everything = await admin_commits.list_commits_admin(repo_full_id=None, repo_type=None, username=None, limit=10, offset=0)
    assert len(everything["commits"]) == 1
    nothing = await admin_commits.list_commits_admin(
        repo_full_id="committer/missing", repo_type=None, username=None, limit=10, offset=0
    )
    assert nothing["commits"] == []


@pytest.mark.asyncio
async def test_commit_filter_covers_every_repository_type_sharing_a_full_id_unless_one_is_chosen():
    owner = make_user("twin")
    model = make_repo(owner, "same", repo_type="model")
    dataset = make_repo(owner, "same", repo_type="dataset")
    make_commit(model, "m1", author=owner, repo_type="model")
    make_commit(dataset, "d1", author=owner, repo_type="dataset")

    both = await admin_commits.list_commits_admin(repo_full_id="twin/same", repo_type=None, username=None, limit=10, offset=0)
    assert sorted(c["repo_type"] for c in both["commits"]) == ["dataset", "model"]
    one = await admin_commits.list_commits_admin(repo_full_id="twin/same", repo_type="dataset", username=None, limit=10, offset=0)
    assert [c["repo_type"] for c in one["commits"]] == ["dataset"]


@pytest.mark.asyncio
async def test_a_suggestion_says_whether_there_is_an_avatar_without_loading_the_image():
    make_user("with-pic", avatar=b"\xff\xd8jpeg")
    make_user("no-pic")
    make_org("org-pic", avatar=b"\xff\xd8jpeg")

    hits = {r["username"]: r for r in await lookup("-pic") + await lookup("pic")}
    assert hits["with-pic"]["has_avatar"] is True
    assert hits["no-pic"]["has_avatar"] is False
    assert hits["org-pic"]["has_avatar"] is True
    assert all("avatar" not in r for r in hits.values())
