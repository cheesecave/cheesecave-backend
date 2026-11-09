"""Unit tests for the admin commit listing, on real repository and commit rows."""

from __future__ import annotations

import pytest

import kohakuhub.api.admin.routers.commits as admin_commits
from test.kohakuhub.support.factories import make_commit, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


async def list_commits(repo_full_id=None, repo_type=None, username=None):
    return (
        await admin_commits.list_commits_admin(
            repo_full_id=repo_full_id,
            repo_type=repo_type,
            username=username,
            limit=50,
            offset=0,
        )
    )["commits"]


@pytest.mark.asyncio
async def test_an_unknown_repository_has_no_commits_instead_of_every_commit():
    owner = make_user("committer")
    make_commit(make_repo(owner, "real"), "c1", author=owner)

    assert len(await list_commits()) == 1
    assert await list_commits(repo_full_id="committer/missing") == []
    assert await list_commits(repo_full_id="committer/rea") == []


@pytest.mark.asyncio
async def test_a_full_id_shared_by_a_model_and_a_dataset_lists_both_unless_a_type_is_given():
    owner = make_user("twin")
    model = make_repo(owner, "same", repo_type="model")
    dataset = make_repo(owner, "same", repo_type="dataset")
    make_commit(model, "m1", author=owner)
    make_commit(dataset, "d1", author=owner)

    both = await list_commits(repo_full_id="twin/same")
    assert sorted(c["repo_type"] for c in both) == ["dataset", "model"]
    only_dataset = await list_commits(repo_full_id="twin/same", repo_type="dataset")
    assert [(c["repo_type"], c["commit_id"]) for c in only_dataset] == [("dataset", "d1")]
    assert await list_commits(repo_full_id="twin/same", repo_type="space") == []


@pytest.mark.asyncio
async def test_a_type_alone_and_an_author_still_filter():
    alice, bob = make_user("alice"), make_user("bob")
    model = make_repo(alice, "m", repo_type="model")
    dataset = make_repo(alice, "d", repo_type="dataset")
    make_commit(model, "m1", author=alice)
    make_commit(dataset, "d1", author=bob)

    assert [c["commit_id"] for c in await list_commits(repo_type="dataset")] == ["d1"]
    assert [c["commit_id"] for c in await list_commits(username="alice")] == ["m1"]
    assert await list_commits(repo_full_id="alice/d", username="alice") == []
