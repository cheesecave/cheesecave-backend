"""What `huggingface_hub` sees of a revision that is not a branch that is there, on a commit and on a preupload (#103).

`create_commit`'s documentation: a revision that is not a branch name (a commit id) makes the server answer
`RevisionNotFoundError`. The client sends the files themselves to the store after the preupload, so the preupload is where a
missing branch has to be said (the client sent them for nothing before). An empty commit is never sent by the client (it
skips it and reads the head), so a raw request stands for it. Real LakeFS and MinIO behind the live server.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import RevisionNotFoundError

REPO = "owner/demo-model"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    yield api
    for tag in ("t103-tag",):
        try:
            await _run(api.delete_tag, REPO, tag=tag)
        except Exception:  # noqa: BLE001 - only there when a test made it
            pass
    if await _run(api.file_exists, REPO, "t103/same.txt"):
        await _run(api.delete_folder, "t103", repo_id=REPO, commit_message="clean up")


async def test_the_same_content_again_answers_the_head(api):
    await _run(api.upload_file, path_or_fileobj=b"same", path_in_repo="t103/same.txt", repo_id=REPO, commit_message="same")
    head = (await _run(api.list_repo_commits, REPO))[0].commit_id

    again = await _run(api.upload_file, path_or_fileobj=b"same", path_in_repo="t103/same.txt", repo_id=REPO, commit_message="same again")

    assert again.oid == head and len(head) == 40
    assert (await _run(api.list_repo_commits, REPO))[0].commit_id == head  # nothing was committed


async def test_an_empty_commit_on_a_branch_that_is_not_there_is_revision_not_found(api):
    with pytest.raises(RevisionNotFoundError):
        await _run(api.create_commit, REPO, operations=[], commit_message="empty", revision="no-such-branch")


async def test_an_empty_commit_on_a_branch_that_is_there_answers_the_head(api):
    head = (await _run(api.list_repo_commits, REPO))[0].commit_id

    info = await _run(api.create_commit, REPO, operations=[], commit_message="empty")

    assert info.oid == head


async def test_an_upload_to_a_branch_that_is_not_there_stops_at_the_preupload(api):
    with pytest.raises(RevisionNotFoundError) as caught:
        await _run(api.upload_file, path_or_fileobj=b"x" * 3_000_000, path_in_repo="t103/big.bin", repo_id=REPO, revision="no-such-branch", commit_message="no")

    assert "/preupload/" in str(caught.value.response.url)  # not after the bytes went to the store


async def test_an_upload_to_a_tag_is_revision_not_found_at_the_commit(api):
    """A tag that is there is answered by the preupload as a branch is (#12); it is the commit that refuses a revision that is
    not a branch, with the error the documentation of `create_commit` names."""
    await _run(api.create_tag, REPO, tag="t103-tag")

    with pytest.raises(RevisionNotFoundError) as caught:
        await _run(api.upload_file, path_or_fileobj=b"x", path_in_repo="t103/small.txt", repo_id=REPO, revision="t103-tag", commit_message="no")

    assert "/commit/" in str(caught.value.response.url)


async def test_what_the_client_cannot_send_is_answered_too(live_server_url, hf_api_token):
    """A header and no operation, raw: 404 for a revision that is not a branch, the head and the Hub's fields for one that is."""
    header = json.dumps({"key": "header", "value": {"summary": "raw"}}) + "\n"
    async with httpx.AsyncClient(base_url=live_server_url, headers={"Authorization": f"Bearer {hf_api_token}"}) as http:
        missing = await http.post(f"/api/models/{REPO}/commit/no-such-branch", content=header, headers={"Content-Type": "application/x-ndjson"})
        there = await http.post(f"/api/models/{REPO}/commit/main", content=header, headers={"Content-Type": "application/x-ndjson"})

    assert missing.status_code == 404
    assert missing.headers["x-error-code"] == "RevisionNotFound"
    body = there.json()
    assert there.status_code == 200 and body["success"] is True and body["hookOutput"] == ""
    assert len(body["commitOid"]) == 40
