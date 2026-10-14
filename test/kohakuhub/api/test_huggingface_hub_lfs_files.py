"""`HfApi.list_lfs_files` against a real stack; `permanently_delete_lfs_files` is not supported (#66).

Real LFS uploads (the client sends the bytes to MinIO through the presigned URLs the batch route hands out), then what a
repository owner sees of them. The deletion answers `501` with a reason the user can read, and deletes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

REPO = "owner/demo-model"
PATH = "ckpt/hf-list.bin"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _client(endpoint, token):
    """The client of the matrix: the oldest ones (0.20) have no ``list_lfs_files`` to call."""
    api = HfApi(endpoint=endpoint, token=token)
    if not callable(getattr(api, "list_lfs_files", None)):
        pytest.skip("this huggingface_hub has no list_lfs_files")
    return api


@pytest.fixture
async def api(live_server_url, hf_api_token):
    yield _client(live_server_url, hf_api_token)


async def _push(api, payload):
    # over the LFS threshold (1 KiB in this harness), under the multipart one: the LFS batch route and one presigned PUT
    await _run(api.upload_file, path_or_fileobj=payload, path_in_repo=PATH, repo_id=REPO, commit_message="checkpoint")


async def test_list_lfs_files_shows_the_versions_of_a_checkpoint(api):
    versions = [os.urandom(50_000 + i) for i in range(3)]
    for payload in versions:
        await _push(api, payload)
    oids = [hashlib.sha256(payload).hexdigest() for payload in versions]

    mine = [f for f in await _run(lambda: list(api.list_lfs_files(REPO))) if f.filename == PATH]
    assert [f.oid for f in mine] == oids[::-1]  # newest first
    assert [f.file_oid for f in mine] == oids[::-1]
    assert [f.size for f in mine] == [len(v) for v in versions][::-1]
    assert all(f.ref == "refs/heads/main" and f.pushed_at.tzinfo is not None for f in mine)


async def test_permanently_delete_lfs_files_says_it_is_not_supported_and_deletes_nothing(api):
    await _push(api, os.urandom(50_000))
    before = [f.oid for f in await _run(lambda: list(api.list_lfs_files(REPO)))]
    files = await _run(lambda: list(api.list_lfs_files(REPO)))
    for rewrite in (True, False):
        with pytest.raises(HfHubHTTPError) as caught:
            await _run(api.permanently_delete_lfs_files, REPO, files, rewrite_history=rewrite)
        assert caught.value.response.status_code == 501
        assert "permanently_delete_lfs_files is not supported" in str(caught.value)  # the user reads why
    assert [f.oid for f in await _run(lambda: list(api.list_lfs_files(REPO)))] == before


async def test_listing_a_repository_of_someone_else_is_refused(live_server_url):
    stranger = _client(live_server_url, None)
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(lambda: list(stranger.list_lfs_files(REPO)))
    assert caught.value.response.status_code in (401, 403, 404)
