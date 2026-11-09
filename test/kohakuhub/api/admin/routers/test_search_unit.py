"""Unit tests for the admin global search: users and organizations ranked like the lookup, on real rows."""

from __future__ import annotations

import pytest

import kohakuhub.api.admin.routers.search as admin_search
from kohakuhub.db import User
from test.kohakuhub.support.factories import make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")

DEFAULT_TYPES = ["users", "repos", "commits"]


async def search(q, types=DEFAULT_TYPES, limit=20):
    return await admin_search.global_search(q=q, types=types, limit=limit)


@pytest.mark.asyncio
async def test_the_default_types_still_answer_users_repositories_and_commits_only():
    make_user("lab-user")
    make_org("lab-org")

    payload = await search("lab")

    assert [u["username"] for u in payload["results"]["users"]] == ["lab-user"]
    assert payload["results"]["organizations"] == []
    assert payload["result_counts"] == {"users": 1, "organizations": 0, "repositories": 0, "commits": 0}


@pytest.mark.asyncio
async def test_organizations_are_searched_when_asked_and_never_among_the_users():
    make_user("lab-user")
    make_org("lab-org")

    payload = await search("lab", types=["users", "orgs"])

    assert [u["username"] for u in payload["results"]["users"]] == ["lab-user"]
    orgs = payload["results"]["organizations"]
    assert [(o["username"], o["is_org"], o["email"]) for o in orgs] == [("lab-org", True, None)]
    assert payload["result_counts"]["organizations"] == 1
    assert [o["username"] for o in (await search("lab", types=["orgs"]))["results"]["organizations"]] == ["lab-org"]


@pytest.mark.asyncio
async def test_users_and_organizations_are_found_by_id_and_ranked_exact_first():
    target = make_user("zed")
    make_user("malice")
    make_user("alice")
    make_org("alice-labs")

    by_id = (await search(f"#{target.id}", types=["users"]))["results"]["users"]
    assert by_id[0]["username"] == "zed" and by_id[0]["matched"] == "id"

    ranked = await search("alice", types=["users", "orgs"])
    assert [u["username"] for u in ranked["results"]["users"]] == ["alice", "malice"]
    assert [o["username"] for o in ranked["results"]["organizations"]] == ["alice-labs"]


@pytest.mark.asyncio
async def test_each_account_keeps_the_fields_the_old_search_returned_and_says_if_it_has_an_avatar():
    make_user("with-pic", avatar=b"\xff\xd8jpeg", email_verified=True)

    (hit,) = (await search("with-pic", types=["users"]))["results"]["users"]

    assert {"id", "username", "email", "email_verified", "is_active"} <= set(hit)
    assert hit["email_verified"] is True and hit["has_avatar"] is True and hit["is_org"] is False
    assert "avatar" not in hit


@pytest.mark.asyncio
async def test_repositories_and_the_limit_are_unchanged():
    owner = make_user("owner-s")
    for name in ("one", "two", "three"):
        make_repo(owner, name)

    payload = await search("owner-s", types=["repos"], limit=2)

    assert len(payload["results"]["repositories"]) == 2
    assert payload["results"]["users"] == []


@pytest.mark.asyncio
async def test_the_limit_applies_to_accounts_too():
    make_user("cap-a")
    make_user("cap-b")

    capped = (await search("cap", types=["users"], limit=1))["results"]["users"]

    assert [u["username"] for u in capped] == ["cap-a"]


@pytest.mark.asyncio
async def test_a_user_matched_by_id_and_by_name_is_listed_once():
    user = make_user("tmp")
    User.update(username=f"user{user.id}").where(User.id == user.id).execute()

    once = (await search(str(user.id), types=["users"]))["results"]["users"]

    assert [(u["username"], u["matched"]) for u in once] == [(f"user{user.id}", "id")]
