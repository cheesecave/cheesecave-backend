"""An LFS object is served only through a repository that itself references it.

Objects are stored once for the whole instance, keyed by their sha256. The
batch route authorizes the caller against the repository in the URL, so what
that repository may hand out has to be decided against that repository, not
against the whole store. Driven through the real application; rows are written
straight into the real database, the presigned URL is signed locally.

Roles come from the shared baseline: ``owner`` (public model
``owner/demo-model``), ``member`` (admin of ``acme-labs``, which owns the
private dataset ``acme-labs/private-dataset``), ``outsider`` (no access), and
an anonymous caller.
"""

from __future__ import annotations

import hashlib

import pytest

LFS_HEADERS = {
    "Accept": "application/vnd.git-lfs+json",
    "Content-Type": "application/vnd.git-lfs+json",
}
PUBLIC = "/owner/demo-model.git"
PRIVATE = "/datasets/acme-labs/private-dataset.git"
SIDE = "/owner/side-private.git"


def oid_of(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _repo(repo_type, namespace, name):
    from kohakuhub.db import Repository

    return Repository.get(
        (Repository.repo_type == repo_type)
        & (Repository.namespace == namespace)
        & (Repository.name == name)
    )


def _public():
    return _repo("model", "owner", "demo-model")


def _private():
    return _repo("dataset", "acme-labs", "private-dataset")


def _side_private():
    """A second, private repository of the public repository's owner."""
    from kohakuhub.db import Repository, User

    owner = User.get(User.username == "owner")
    return Repository.get_or_create(
        repo_type="model",
        namespace="owner",
        name="side-private",
        defaults={"full_id": "owner/side-private", "owner": owner, "private": True},
    )[0]


def _file(repo, oid, *, branch="main", path=None, deleted=False):
    from kohakuhub.db import File

    path = path or f"{oid[:12]}.bin"

    return File.create(
        repository=repo,
        owner_id=repo.owner_id,
        path_in_repo=path,
        sha256=oid,
        size=3,
        lfs=True,
        branch=branch,
        is_deleted=deleted,
    )


def _history(repo, oid):
    from kohakuhub.db import LFSObjectHistory

    return LFSObjectHistory.create(
        repository=repo, path_in_repo="old.bin", sha256=oid, size=3, commit_id="c" * 40
    )


def _head_ref(repo, oid, branch):
    from kohakuhub.db import LfsHeadRef

    return LfsHeadRef.create(
        repository=repo, branch=branch, path_in_repo="head.bin", sha256=oid
    )


async def batch(client, base, oids, operation="download"):
    response = await client.post(
        f"{base}/info/lfs/objects/batch",
        json={
            "operation": operation,
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": 3} for oid in oids],
        },
        headers=LFS_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["objects"]


def assert_served(obj, oid):
    assert obj["oid"] == oid
    assert obj["actions"]["download"]["href"].startswith("http")
    assert "error" not in obj


def assert_not_found(obj, oid):
    assert obj["oid"] == oid
    assert obj["error"] == {"code": 404, "message": "Object not found"}
    assert "actions" not in obj


# --- the attack pattern ----------------------------------------------------


async def test_outsider_cannot_fetch_a_private_object_through_a_public_repo(outsider_client):
    oid = oid_of("only-in-private-dataset")
    _file(_private(), oid)

    # asking the private repository itself is refused
    refused = await outsider_client.post(
        f"{PRIVATE}/info/lfs/objects/batch",
        json={"operation": "download", "objects": [{"oid": oid, "size": 3}]},
        headers=LFS_HEADERS,
    )
    assert refused.status_code == 404

    # asking through the public repository must not hand out the object
    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_not_found(obj, oid)


async def test_an_object_only_in_another_private_repo_of_the_same_owner_is_not_served(
    outsider_client,
):
    oid = oid_of("only-in-owner-private")
    _file(_side_private(), oid)

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_not_found(obj, oid)


async def test_an_object_held_by_both_is_served_through_the_public_repo(outsider_client):
    oid = oid_of("in-both")
    _file(_private(), oid)
    _file(_public(), oid)

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_served(obj, oid)


# --- what counts as held by the requested repository ------------------------


async def test_an_older_commit_of_the_repo_is_served(outsider_client):
    oid = oid_of("older-commit")
    _history(_public(), oid)

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_served(obj, oid)


async def test_a_side_branch_head_link_is_served(outsider_client):
    oid = oid_of("side-branch-head")
    _head_ref(_public(), oid, "dev")

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_served(obj, oid)


async def test_a_side_branch_file_row_is_served(outsider_client):
    oid = oid_of("side-branch-file")
    _file(_public(), oid, branch="dev")

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_served(obj, oid)


async def test_a_soft_deleted_row_alone_is_not_served(outsider_client):
    oid = oid_of("soft-deleted")
    _file(_public(), oid, deleted=True)

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_not_found(obj, oid)


async def test_a_collected_object_is_not_served(outsider_client):
    from kohakuhub.db import LfsObjectTombstone

    oid = oid_of("tombstoned")
    _file(_public(), oid)
    LfsObjectTombstone.create(sha256=oid, state="deleted")

    (obj,) = await batch(outsider_client, PUBLIC, [oid])
    assert_not_found(obj, oid)


# --- legitimate readers keep working ----------------------------------------


async def test_anonymous_downloads_the_public_repos_own_object(client):
    oid = oid_of("anonymous-own")
    _file(_public(), oid)

    (obj,) = await batch(client, PUBLIC, [oid])
    assert_served(obj, oid)


async def test_a_member_downloads_the_private_repos_own_object(member_client):
    oid = oid_of("member-own")
    _file(_private(), oid)

    (obj,) = await batch(member_client, PRIVATE, [oid])
    assert_served(obj, oid)


async def test_a_batch_answers_per_object(outsider_client):
    served, hidden = oid_of("mixed-served"), oid_of("mixed-hidden")
    _file(_public(), served)
    _file(_private(), hidden)

    a, b = await batch(outsider_client, PUBLIC, [served, hidden])
    assert_served(a, served)
    assert_not_found(b, hidden)


# --- upload: the "already stored" shortcut is scoped too ---------------------


@pytest.fixture
def stored(monkeypatch):
    """Storage holds every object (as after another repository uploaded it)."""
    from kohakuhub.api.git.routers import lfs

    async def exists(bucket, key):
        return True

    monkeypatch.setattr(lfs, "object_exists", exists)


async def test_upload_of_an_object_the_repo_holds_is_skipped(owner_client, stored):
    oid = oid_of("upload-own")
    _file(_public(), oid)

    (obj,) = await batch(owner_client, PUBLIC, [oid], "upload")
    assert "actions" not in obj and "error" not in obj


async def test_upload_of_an_object_the_caller_may_read_elsewhere_is_skipped_and_remembered(
    owner_client, stored
):
    """The owner can read the private dataset, so its content is theirs to reuse."""
    from kohakuhub.db import LfsGrant

    oid = oid_of("upload-elsewhere")
    _file(_private(), oid)

    (obj,) = await batch(owner_client, PUBLIC, [oid], "upload")

    assert "actions" not in obj and "error" not in obj
    grant = LfsGrant.get((LfsGrant.repository == _public()) & (LfsGrant.sha256 == oid))
    assert grant.kind == "upload"
