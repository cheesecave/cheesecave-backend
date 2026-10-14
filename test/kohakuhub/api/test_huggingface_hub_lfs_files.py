"""`HfApi.list_lfs_files` and `HfApi.permanently_delete_lfs_files` against a real stack (#66).

Real LFS uploads (the client sends the bytes to MinIO through the presigned URLs the batch route hands out), then the
storage clean-up a repository owner does: list what is stored, delete the old versions for good.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
from datetime import timedelta

import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

REPO = "owner/demo-model"
PATH = "ckpt/hf-clean-up.bin"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _live(name):
    return importlib.import_module(name)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    yield HfApi(endpoint=live_server_url, token=hf_api_token)
    db = _live("kohakuhub.db")
    db.LFSObjectHistory.delete().where(db.LFSObjectHistory.path_in_repo.startswith("ckpt/")).execute()


def _grace_over():
    """The uploads' 24 hour grace period is what the garbage collection waits for; a test cannot wait for it."""
    db = _live("kohakuhub.db")
    db.LfsRecentObject.update(touched_at=db.utcnow() - timedelta(days=2)).execute()


async def _push(api, payload):
    # over the LFS threshold (1 KiB in this harness), under the multipart one: the LFS batch route and one presigned PUT
    await _run(api.upload_file, path_or_fileobj=payload, path_in_repo=PATH, repo_id=REPO, commit_message="checkpoint")


async def test_list_then_delete_the_old_versions_of_a_checkpoint(api):
    versions = [os.urandom(50_000 + i) for i in range(3)]
    for payload in versions:
        await _push(api, payload)
    oids = [hashlib.sha256(payload).hexdigest() for payload in versions]

    mine = [f for f in await _run(lambda: list(api.list_lfs_files(REPO))) if f.filename == PATH]
    assert [f.oid for f in mine] == oids[::-1]  # newest first
    assert [f.file_oid for f in mine] == oids[::-1]
    assert [f.size for f in mine] == [len(v) for v in versions][::-1]
    assert all(f.ref == "refs/heads/main" and f.pushed_at.tzinfo is not None for f in mine)

    old = [f for f in mine if f.oid != oids[2]]
    # the default of the client, rewriting history, is not something a Cheesecave can do
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(api.permanently_delete_lfs_files, REPO, old)
    assert caught.value.response.status_code == 501
    assert "rewrite_history=False" in str(caught.value)

    # the uploads are still within their grace period
    with pytest.raises(HfHubHTTPError) as busy:
        await _run(api.permanently_delete_lfs_files, REPO, old, rewrite_history=False)
    assert busy.value.response.status_code == 409

    _grace_over()
    await _run(api.permanently_delete_lfs_files, REPO, old, rewrite_history=False)
    left = [f for f in await _run(lambda: list(api.list_lfs_files(REPO))) if f.filename == PATH]
    assert [f.oid for f in left] == [oids[2]]

    # what main links is refused, and nothing else in the same request goes either
    with pytest.raises(HfHubHTTPError) as in_use:
        await _run(api.permanently_delete_lfs_files, REPO, left, rewrite_history=False)
    assert in_use.value.response.status_code == 409
    assert in_use.value.response.json()["inUse"] == {oids[2]: "head"}

    # the head still downloads; deleting again is a no-op
    assert (await _run(api.get_paths_info, REPO, [PATH]))[0].size == len(versions[2])
    await _run(api.permanently_delete_lfs_files, REPO, old, rewrite_history=False)


async def test_listing_a_repository_of_someone_else_is_refused(live_server_url):
    stranger = HfApi(endpoint=live_server_url, token=None)
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(lambda: list(stranger.list_lfs_files(REPO)))
    assert caught.value.response.status_code in (401, 403, 404)
