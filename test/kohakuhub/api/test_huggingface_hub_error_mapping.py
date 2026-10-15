"""What `huggingface_hub` sees when LakeFS or the object store answers with an error (#10).

A client decides what to do from the status: `create_branch(exist_ok=True)` reads a 409 as "it exists", a 404 with
`RevisionNotFound` is a wrong branch, and a 5xx is a fault to retry. The routes read the status of the answer of
LakeFS, and a store that cannot answer is a server error, not "missing". Real LakeFS and MinIO behind the live server;
the outage of the store is injected (a targeted mock: it cannot be produced against the real MinIO).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import HfHubHTTPError, RevisionNotFoundError

REPO = "owner/demo-model"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    async def clear():
        for branch in (await _run(api.list_repo_refs, REPO)).branches:
            if branch.name != "main":
                await _run(api.delete_branch, REPO, branch=branch.name)

    await clear()
    yield api
    await clear()


async def test_create_branch_of_a_name_that_exists_is_a_409_and_exist_ok_reads_it(api):
    await _run(api.create_branch, REPO, branch="shared")
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(api.create_branch, REPO, branch="shared")
    assert caught.value.response.status_code == 409
    await _run(api.create_branch, REPO, branch="shared", exist_ok=True)  # the client swallows exactly this 409
    assert [b.name for b in (await _run(api.list_repo_refs, REPO)).branches].count("shared") == 1


@pytest.mark.parametrize("name", ["conflict", "x404x", "not-found", "409"])
async def test_a_branch_named_like_an_error_is_a_branch(api, live_server_url, hf_api_token, tmp_path, name):
    """The words of an error in a name (or in the URL of a LakeFS call) change nothing."""
    await _run(api.create_branch, REPO, branch=name)
    await _run(api.upload_file, path_or_fileobj=b"on " + name.encode(), path_in_repo="note.txt", repo_id=REPO, revision=name, commit_message="note")
    assert (await _run(api.model_info, REPO, revision=name)).sha
    path = await _run(hf_hub_download, REPO, "note.txt", revision=name, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
    assert open(path, "rb").read() == b"on " + name.encode()
    with pytest.raises(HfHubHTTPError) as again:
        await _run(api.create_branch, REPO, branch=name)
    assert again.value.response.status_code == 409


async def test_a_missing_revision_is_revision_not_found_on_every_read(api, live_server_url, hf_api_token, tmp_path):
    for call in (
        lambda: api.model_info(REPO, revision="no-such-branch"),
        lambda: list(api.list_repo_tree(REPO, revision="no-such-branch")),
        lambda: api.get_paths_info(REPO, ["README.md"], revision="no-such-branch"),
        lambda: hf_hub_download(REPO, "README.md", revision="no-such-branch", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path),
    ):
        with pytest.raises(RevisionNotFoundError):
            await _run(call)


async def test_an_outage_of_the_store_during_an_lfs_upload_is_a_server_error_not_missing_content(api, monkeypatch):
    """The LFS verify used to answer 404 "not found in storage" when the store could not answer HEAD: the client took the
    object for lost. It is a server error now, which a client retries; once the store is back the same upload works."""
    s3 = importlib.import_module("kohakuhub.utils.s3")
    real = s3.get_s3_client

    class Denying:
        def __init__(self, client):
            self._client = client

        def head_object(self, **kwargs):
            raise s3.ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

        def __getattr__(self, name):
            return getattr(self._client, name)

    payload = b"lfs object during an outage " * 2000
    monkeypatch.setattr(s3, "get_s3_client", lambda *a, **k: Denying(real(*a, **k)))
    with pytest.raises(Exception) as caught:
        await _run(api.upload_file, path_or_fileobj=payload, path_in_repo="outage.bin", repo_id=REPO, commit_message="during an outage")
    chain, error = [], caught.value
    while error is not None:
        chain.append(error)
        error = error.__cause__
    statuses = [e.response.status_code for e in chain if isinstance(e, HfHubHTTPError)]
    assert statuses and all(status >= 500 for status in statuses), (statuses, [type(e).__name__ for e in chain])

    monkeypatch.undo()
    await _run(api.upload_file, path_or_fileobj=payload, path_in_repo="outage.bin", repo_id=REPO, commit_message="after it")
    assert await _run(api.file_exists, REPO, "outage.bin")
