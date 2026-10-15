"""What `huggingface_hub` sees of a revision that is not there on the commit list (#99).

`list_repo_commits` on a revision the repository does not have is `RevisionNotFoundError` (the client dispatches on
`X-Error-Code`), not a retried 500. The client cannot send a body that is not base64 or a `..` path (it encodes itself and
refuses `../x`), so those are tested on the route (`test_commit_bad_input.py`); here is what it can do: names with dots in
them are still committed. Real LakeFS and MinIO behind the live server.
"""

from __future__ import annotations

import asyncio

import pytest
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.utils import RevisionNotFoundError

REPO = "owner/demo-model"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    yield api
    if await _run(api.file_exists, REPO, "t99/a..b.txt"):
        await _run(api.delete_folder, "t99", repo_id=REPO, commit_message="clean up")


async def test_the_commits_of_a_revision_that_is_not_there_are_revision_not_found(api):
    if not hasattr(api, "list_repo_commits"):
        pytest.skip("this client has no list_repo_commits")

    with pytest.raises(RevisionNotFoundError):
        await _run(api.list_repo_commits, REPO, revision="no-such-branch")


async def test_the_commits_of_a_branch_that_is_there_are_listed(api):
    if not hasattr(api, "list_repo_commits"):
        pytest.skip("this client has no list_repo_commits")

    commits = await _run(api.list_repo_commits, REPO)

    assert commits and commits[0].commit_id


async def test_names_with_dots_in_them_are_committed(api):
    await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd("t99/a..b.txt", b"dots"), CommitOperationAdd("t99/..hidden", b"hidden")],
        commit_message="dots in a name",
    )

    assert await _run(api.file_exists, REPO, "t99/a..b.txt")
    assert await _run(api.file_exists, REPO, "t99/..hidden")
