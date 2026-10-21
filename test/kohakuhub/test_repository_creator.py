"""``creator_profile``: who a repository's page names as its creator (real SQL, SQLite and PostgreSQL).

A record names its person; without a record only a personal namespace can say (the account is the creator);
a record whose person is gone, or an organization's repository without a record, has nobody to name.
"""

from datetime import datetime

from kohakuhub.db import RepositoryCreator, User
from kohakuhub.repository_creator import creator_profile
from test.kohakuhub.support.factories import make_org, make_repo, make_user


def _repo(owner, name="repo"):
    return make_repo(owner, name, created_at=datetime(2025, 1, 1))


def test_a_record_names_its_person_with_the_profile_fields_of_a_feed(db_dual):
    org, alice = make_org("acme"), make_user("alice", full_name="Alice A")
    repo = _repo(org)
    RepositoryCreator.create(repository=repo, user=alice, source="recorded")

    assert creator_profile(repo) == {
        "username": "alice",
        "full_name": "Alice A",
        "is_org": False,
        "avatar_url": None,
    }


def test_the_avatar_of_the_person_is_given_when_they_have_one(db_dual):
    org, alice = make_org("acme"), make_user("alice")
    User.update(avatar=b"\x89PNG", avatar_updated_at=datetime(2025, 2, 3, 4, 5, 6)).where(User.id == alice.id).execute()
    repo = _repo(org)
    RepositoryCreator.create(repository=repo, user=alice, source="inferred")

    assert creator_profile(repo)["avatar_url"] == "/api/users/alice/avatar?fallback=false&v=2025-02-03T04:05:06.000000Z"


def test_a_personal_repository_without_a_record_was_created_by_its_owner(db_dual):
    carol = make_user("carol")

    assert creator_profile(_repo(carol))["username"] == "carol"


def test_an_organization_repository_without_a_record_has_nobody_to_name(db_dual):
    assert creator_profile(_repo(make_org("acme"))) is None


def test_a_record_whose_person_is_gone_names_nobody_even_in_a_personal_namespace(db_dual):
    carol = make_user("carol")
    repo = _repo(carol)
    RepositoryCreator.create(repository=repo, user=None, source="inferred")

    assert creator_profile(repo) is None
