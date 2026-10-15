"""What `huggingface_hub` sees of a revision that is not there on the commit list, and of the paths it may commit (#99).

`list_repo_commits` on a revision the repository does not have is `RevisionNotFoundError` (the client dispatches on
`X-Error-Code`), not a retried 500. The client refuses some paths before it sends them (`.`, a `..` segment, a `.git`
folder: `_validate_path_in_repo`); the server refuses the same ones when a raw request carries them, and keeps every name the
client commits (`.gitattributes`, `.github/...`, dots inside a name). The client encodes base64 itself, so a body that is not
base64 is tested on the route (`test_commit_bad_input.py`). Real LakeFS and MinIO behind the live server.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
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
    if await _run(api.file_exists, REPO, "t99/a..b.txt") or await _run(api.file_exists, REPO, "t99/.gitattributes"):
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


async def test_names_the_client_commits_are_kept(api):
    names = ["t99/a..b.txt", "t99/..hidden", "t99/.gitattributes", "t99/.github/workflows/ci.yml", "t99/.gitignore"]
    await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd(name, name.encode()) for name in names],
        commit_message="names that only look like the refused ones",
    )

    for name in names:
        assert await _run(api.file_exists, REPO, name), name


# What the client refuses before it sends a path
REFUSED_BY_THE_CLIENT = [".", "..", "../x", "a/../b", ".git", ".git/config", "t99/.git/x"]


@pytest.mark.parametrize("path", REFUSED_BY_THE_CLIENT)
async def test_the_server_refuses_what_the_client_refuses_when_a_raw_request_carries_it(live_server_url, hf_api_token, path):
    # Current clients refuse each of these themselves (older ones not all): the server must not depend on that
    header = {"key": "header", "value": {"summary": "raw"}}
    file = {"key": "file", "value": {"path": path, "content": base64.b64encode(b"x").decode(), "encoding": "base64"}}
    body = "\n".join(json.dumps(line) for line in (header, file)) + "\n"
    async with httpx.AsyncClient(base_url=live_server_url, headers={"Authorization": f"Bearer {hf_api_token}"}) as http:
        response = await http.post(f"/api/models/{REPO}/commit/main", content=body, headers={"Content-Type": "application/x-ndjson"})

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"
