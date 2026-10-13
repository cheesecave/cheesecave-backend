"""``huggingface_hub`` against ``resolve``: revision errors (#58) and conditional requests (#59).

What a downstream library sees. ``transformers`` / ``diffusers`` / ``timm`` branch on the
exception class: ``RevisionNotFoundError`` means "wrong branch", ``EntryNotFoundError`` means
"no such file here, try another name". Both come from ``X-Error-Code``; the reason shown to the
user comes from ``X-Error-Message``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from huggingface_hub import HfApi, get_hf_file_metadata, hf_hub_download, hf_hub_url

# The version-portable import path (the CI matrix includes old clients).
from huggingface_hub.utils import EntryNotFoundError, RevisionNotFoundError

REPO = "owner/demo-model"


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def dev_branch(live_server_url, hf_api_token):
    # Through the live server: its LakeFS client belongs to the server's event loop.
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    await _run(api.create_branch, REPO, branch="dev")
    yield "dev"
    await _run(api.delete_branch, REPO, branch="dev")


def _download(live_server_url, token, tmp_path, filename="README.md", revision=None):
    return hf_hub_download(
        repo_id=REPO,
        filename=filename,
        revision=revision,
        endpoint=live_server_url,
        token=token,
        cache_dir=tmp_path,
    )


@pytest.mark.hf_client
async def test_download_of_an_unknown_revision_raises_revision_not_found(
    live_server_url, hf_api_token, tmp_path
):
    with pytest.raises(RevisionNotFoundError) as caught:
        await _run(_download, live_server_url, hf_api_token, tmp_path, revision="nope")
    assert not isinstance(caught.value, EntryNotFoundError)
    assert "nope" in str(caught.value)  # the reason comes through X-Error-Message


@pytest.mark.hf_client
async def test_download_of_a_missing_file_raises_entry_not_found(
    live_server_url, hf_api_token, tmp_path
):
    with pytest.raises(EntryNotFoundError) as caught:
        await _run(_download, live_server_url, hf_api_token, tmp_path, filename="no/such.bin")
    assert not isinstance(caught.value, RevisionNotFoundError)
    assert "no/such.bin" in str(caught.value)


@pytest.mark.hf_client
async def test_file_metadata_of_an_unknown_revision_raises_revision_not_found(
    live_server_url, hf_api_token
):
    url = hf_hub_url(REPO, "README.md", revision="nope", endpoint=live_server_url)
    with pytest.raises(RevisionNotFoundError):
        await _run(get_hf_file_metadata, url, token=hf_api_token)


@pytest.mark.hf_client
async def test_a_full_ref_name_is_a_revision_everywhere_the_client_sends_one(
    live_server_url, hf_api_token, tmp_path, dev_branch
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    short = await _run(api.model_info, REPO, revision="dev")
    full = await _run(api.model_info, REPO, revision="refs/heads/dev")
    assert full.sha == short.sha

    assert sorted(await _run(api.list_repo_files, REPO, revision="refs/heads/dev")) == sorted(
        await _run(api.list_repo_files, REPO, revision="dev")
    )
    tree = await _run(lambda: list(api.list_repo_tree(REPO, revision="refs/heads/dev")))
    assert {item.path for item in tree} >= {"README.md", "weights"}

    path = await _run(_download, live_server_url, hf_api_token, tmp_path, "README.md", "refs/heads/dev")
    assert open(path, "rb").read() == b"# Demo Model\n\nseed data\n"
    assert short.sha in path  # the snapshot folder is the commit, not the ref name

    url = hf_hub_url(REPO, "README.md", revision="refs/heads/dev", endpoint=live_server_url)
    assert "refs%2Fheads%2Fdev" in url  # what the client really sends
    meta = await _run(get_hf_file_metadata, url, token=hf_api_token)
    assert meta.commit_hash == short.sha


@pytest.mark.hf_client
async def test_a_full_ref_name_of_a_missing_branch_raises_revision_not_found(
    live_server_url, hf_api_token, tmp_path
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(RevisionNotFoundError):
        await _run(api.model_info, REPO, revision="refs/heads/nope")
    with pytest.raises(RevisionNotFoundError):
        await _run(_download, live_server_url, hf_api_token, tmp_path, "README.md", "refs/heads/nope")


@pytest.mark.hf_client
async def test_download_still_caches_by_etag(live_server_url, hf_api_token, tmp_path):
    """The client's own cache check is HEAD plus an etag compare; 304 support must not disturb it."""
    first = await _run(_download, live_server_url, hf_api_token, tmp_path)
    second = await _run(_download, live_server_url, hf_api_token, tmp_path)
    assert first == second
    lfs = await _run(_download, live_server_url, hf_api_token, tmp_path, "weights/model.safetensors")
    assert open(lfs, "rb").read() == b"safe tensor payload"


@pytest.mark.hf_client
@pytest.mark.parametrize("filename", ["README.md", "weights/model.safetensors"])
async def test_a_client_revalidating_with_the_etag_gets_304(
    live_server_url, hf_api_token, filename
):
    """``curl -z``, browsers, proxies and ``httpx`` caches revalidate with ``If-None-Match``
    (``hf_hub_download`` itself does not; it compares the HEAD etag)."""
    url = hf_hub_url(REPO, filename, endpoint=live_server_url)
    headers = {"Authorization": f"Bearer {hf_api_token}"}
    meta = await _run(get_hf_file_metadata, url, token=hf_api_token)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for validator in (meta.etag, f'"{meta.etag}"', f'W/"{meta.etag}"'):
            response = await client.get(url, headers={**headers, "If-None-Match": validator})
            assert response.status_code == 304
            assert response.headers["x-repo-commit"] == meta.commit_hash
            assert "location" not in response.headers
        stale = await client.get(url, headers={**headers, "If-None-Match": '"0000"'})
        assert stale.status_code == 302
