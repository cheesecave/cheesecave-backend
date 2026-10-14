"""User and organization overview, organization members: the Hub's routes over this hub's data.

Real database (SQLite and PostgreSQL through ``db_dual``); the app runs through ``TestClient``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from kohakuhub.api import hf_account
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import RepositoryLike, UserFollow, UserOrganization
from test.kohakuhub.support.factories import make_org, make_repo, make_user

STAMP = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def world(db_dual):
    viewer = make_user("viewer")
    author = make_user("author", full_name="An Author", bio="writes things")
    org = make_org("acme", admin=author, full_name="Acme Labs", description="makes models")
    auth = {"user": viewer}
    app = FastAPI()
    app.include_router(hf_account.router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: auth["user"]
    with TestClient(app) as session:
        yield SimpleNamespace(viewer=viewer, author=author, org=org, auth=auth, session=session)


def test_a_user_overview_has_the_hubs_fields_and_counts_what_the_viewer_may_read(world):
    make_repo(world.author, "m1")
    make_repo(world.author, "secret", private=True)
    make_repo(world.author, "d1", repo_type="dataset")
    make_repo(world.author, "s1", repo_type="space")
    liked = make_repo(world.org, "liked")
    RepositoryLike.create(repository=liked, user=world.author)
    UserFollow.create(follower=world.viewer, followed=world.author, created_at=STAMP)
    UserFollow.create(follower=world.author, followed=world.org, created_at=STAMP)
    UserFollow.create(follower=world.author, followed=world.viewer, created_at=STAMP)

    body = world.session.get("/api/users/author/overview").json()

    assert body["user"] == "author" and body["type"] == "user" and body["fullname"] == "An Author"
    assert body["details"] == "writes things" and body["isPro"] is False and body["_id"]
    assert (body["numModels"], body["numDatasets"], body["numSpaces"]) == (1, 1, 1)  # the private model is not counted
    assert (body["numFollowers"], body["numFollowing"], body["numLikes"]) == (1, 2, 1)
    assert body["isFollowing"] is True and body["avatarUrl"] is None
    assert body["numDiscussions"] == body["numPapers"] == body["numUpvotes"] == 0
    assert body["createdAt"].endswith("Z")
    assert [org["name"] for org in body["orgs"]] == ["acme"] and body["orgs"][0]["type"] == "org"


def test_the_owner_sees_the_private_repositories_in_the_counts(world):
    make_repo(world.author, "m1")
    make_repo(world.author, "secret", private=True)
    world.auth["user"] = world.author

    assert world.session.get("/api/users/author/overview").json()["numModels"] == 2


def test_an_anonymous_reader_is_not_following_anyone(world):
    world.auth["user"] = None

    body = world.session.get("/api/users/author/overview").json()

    assert body["isFollowing"] is False


def test_a_user_without_a_full_name_is_named_by_the_username(world):
    assert world.session.get("/api/users/viewer/overview").json()["fullname"] == "viewer"


@pytest.mark.parametrize("name", ["ghost", "acme"])  # missing, and an organization asked as a user
def test_a_user_overview_of_no_user_is_404(world, name):
    assert world.session.get(f"/api/users/{name}/overview").status_code == 404


def test_an_organization_overview_has_the_hubs_fields(world):
    make_repo(world.org, "m1")
    make_repo(world.org, "hidden", private=True)
    make_repo(world.org, "d1", repo_type="dataset")
    UserOrganization.create(user=world.viewer, organization=world.org, role="member")
    UserFollow.create(follower=world.viewer, followed=world.org, created_at=STAMP)

    body = world.session.get("/api/organizations/acme/overview").json()

    assert body["name"] == "acme" and body["fullname"] == "Acme Labs" and body["details"] == "makes models"
    assert (body["numModels"], body["numDatasets"], body["numSpaces"]) == (2, 1, 0)  # a member reads the private one
    assert (body["numUsers"], body["numFollowers"], body["isFollowing"]) == (2, 1, True)
    assert body["isVerified"] is False and body["plan"] is None and body["_id"] and body["avatarUrl"] is None


def test_an_outsider_does_not_count_the_private_repositories_of_an_organization(world):
    make_repo(world.org, "hidden", private=True)

    assert world.session.get("/api/organizations/acme/overview").json()["numModels"] == 0


@pytest.mark.parametrize("name", ["ghost", "author"])
def test_an_organization_overview_of_no_organization_is_404(world, name):
    assert world.session.get(f"/api/organizations/{name}/overview").status_code == 404


def test_organization_members_are_listed_as_hub_users(world):
    UserOrganization.create(user=world.viewer, organization=world.org, role="member")

    response = world.session.get("/api/organizations/acme/members")

    rows = {row["user"]: row for row in response.json()}
    assert set(rows) == {"author", "viewer"} and "next" not in response.links
    assert rows["author"] == {
        "avatarUrl": None,
        "fullname": "An Author",
        "isPro": False,
        "type": "user",
        "user": "author",
    }


@pytest.mark.parametrize("name", ["ghost", "author"])
def test_members_of_no_organization_is_404(world, name):
    assert world.session.get(f"/api/organizations/{name}/members").status_code == 404
