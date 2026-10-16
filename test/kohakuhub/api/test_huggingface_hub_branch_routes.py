"""What `huggingface_hub` sees of a branch that is not there on the branch routes (#103).

`super_squash_history` documents `RevisionNotFoundError` "if the branch to squash cannot be found"; the client dispatches on
`X-Error-Code`, and the route answered a bare 404, so the client raised a plain `HfHubHTTPError`. Real LakeFS and MinIO behind
the live server. Squash runs on PostgreSQL only (by design), like the other history operations.
"""

from __future__ import annotations

import asyncio

import pytest
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.utils import RevisionNotFoundError

from test.kohakuhub.support.db import history_operations_need_postgres

REPO = "owner/demo-model"
pytestmark = [pytest.mark.hf_client, history_operations_need_postgres]


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    yield api
    try:
        await _run(api.delete_branch, REPO, branch="t103-squash")
    except Exception:  # noqa: BLE001 - only there when a test made it
        pass


async def test_super_squash_history_of_a_branch_that_is_not_there_is_revision_not_found(api):
    if not hasattr(api, "super_squash_history"):
        pytest.skip("this client has no super_squash_history")

    with pytest.raises(RevisionNotFoundError):
        await _run(api.super_squash_history, REPO, branch="no-such-branch")


async def test_super_squash_history_of_a_branch_that_is_there_still_works(api):
    if not hasattr(api, "super_squash_history"):
        pytest.skip("this client has no super_squash_history")
    await _run(api.create_branch, REPO, branch="t103-squash")
    for i in range(2):
        await _run(api.create_commit, REPO, operations=[CommitOperationAdd(f"t103/s{i}.txt", b"x")], revision="t103-squash", commit_message=f"c{i}")

    await _run(api.super_squash_history, REPO, branch="t103-squash")

    assert len(await _run(api.list_repo_commits, REPO, revision="t103-squash")) == 1


async def test_the_other_branch_calls_on_a_branch_that_is_not_there_are_revision_not_found(api):
    with pytest.raises(RevisionNotFoundError):
        await _run(api.delete_branch, REPO, branch="no-such-branch")
    with pytest.raises(RevisionNotFoundError):
        await _run(api.create_branch, REPO, branch="t103-new", revision="no-such-branch")
