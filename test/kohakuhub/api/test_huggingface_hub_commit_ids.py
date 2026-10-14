"""``huggingface_hub`` with a commit id in the Hub's form (40 hex) or abbreviated, wherever the client sends one (#12).

The Hub says 40 hex and takes any prefix of 5 to 40 as a revision or a ``parent_commit``; LakeFS says 64.
Every route here takes all of those. Real client, real server (the live server and its own event loop).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from huggingface_hub import CommitOperationAdd, HfApi, get_hf_file_metadata, hf_hub_download, hf_hub_url
from huggingface_hub.utils import HfHubHTTPError

REPO = "owner/demo-model"


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _api(url, token):
    return HfApi(endpoint=url, token=token)


def _head(api):
    """The head of main as ``model_info`` says it (the id a client has in hand), and as LakeFS has it."""
    return api.model_info(REPO).sha, api.model_info(REPO, revision="main").sha


def _forms(api):
    hub, full = _head(api)
    assert full.startswith(hub)
    return {"hub 40": hub[:40], "abbreviated 12": full[:12], "short 7": full[:7], "full": full}


@pytest.mark.hf_client
async def test_every_form_is_a_revision_everywhere_the_client_sends_one(live_server_url, hf_api_token, tmp_path):
    api = _api(live_server_url, hf_api_token)
    forms = await _run(_forms, api)
    reference = await _run(lambda: sorted(api.list_repo_files(REPO, revision="main")))
    for name, rev in forms.items():
        info = await _run(api.model_info, REPO, revision=rev)
        assert info.sha.startswith(forms["abbreviated 12"]), name
        assert await _run(lambda: sorted(api.list_repo_files(REPO, revision=rev))) == reference, name
        tree = await _run(lambda: sorted(t.path for t in api.list_repo_tree(REPO, revision=rev, recursive=True)))
        assert {"README.md", "config.json"} <= set(tree), name
        paths = await _run(api.get_paths_info, REPO, ["README.md", "config.json"], revision=rev)
        assert sorted(p.path for p in paths) == ["README.md", "config.json"], name
        commits = await _run(lambda: [c.commit_id for c in api.list_repo_commits(REPO, revision=rev)])
        assert commits and commits[0].startswith(forms["abbreviated 12"]), name
        path = await _run(
            hf_hub_download, REPO, "README.md", revision=rev, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path / name
        )
        assert open(path, "rb").read() == b"# Demo Model\n\nseed data\n", name
        url = hf_hub_url(REPO, "README.md", revision=rev, endpoint=live_server_url)
        meta = await _run(get_hf_file_metadata, url, token=hf_api_token)
        assert meta.commit_hash.startswith(forms["abbreviated 12"]) and meta.size == 24, name


@pytest.mark.hf_client
async def test_every_form_is_a_parent_commit_and_a_stale_one_is_refused(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    forms = await _run(_forms, api)
    for name, parent in forms.items():
        if name == "full":
            continue  # the client itself refuses 64 hex as a parent_commit; the server takes it (see the route tests)
        before = await _run(_forms, api)
        wanted = before["hub 40"] if name == "hub 40" else before["full"][: len(parent)]
        await _run(
            api.create_commit,
            REPO,
            [CommitOperationAdd(f"parent-{uuid.uuid4().hex[:8]}.txt", b"x")],
            commit_message=f"parent {name}",
            parent_commit=wanted,
        )
        # it landed on top of the head the form named: the branch moved
        assert (await _run(_forms, api))["full"] != before["full"], name
    stale = forms["hub 40"]
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(
            api.create_commit,
            REPO,
            [CommitOperationAdd("stale.txt", b"x")],
            commit_message="stale",
            parent_commit=stale,
        )
    assert caught.value.response.status_code == 412


@pytest.mark.hf_client
async def test_a_branch_and_a_tag_can_start_at_a_commit_in_any_form(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    forms = await _run(_forms, api)
    suffix = uuid.uuid4().hex[:6]
    for name, rev in forms.items():
        label = name.split()[-1]
        await _run(api.create_branch, REPO, branch=f"at-{label}-{suffix}", revision=rev)
        await _run(api.create_tag, REPO, tag=f"t-{label}-{suffix}", revision=rev)
    refs = await _run(api.list_repo_refs, REPO)
    heads = {b.name: b.target_commit for b in refs.branches}
    tags = {t.name: t.target_commit for t in refs.tags}
    for name in forms:
        label = name.split()[-1]
        assert heads[f"at-{label}-{suffix}"].startswith(forms["abbreviated 12"])
        assert tags[f"t-{label}-{suffix}"].startswith(forms["abbreviated 12"])


@pytest.mark.hf_client
async def test_the_commit_page_a_browser_opens_shows_the_author_in_every_form(live_server_url, hf_api_token):
    """What the web client calls, with the id it holds: the author is the database's, not LakeFS's committer."""
    api = _api(live_server_url, hf_api_token)
    forms = await _run(_forms, api)
    headers = {"Authorization": f"Bearer {hf_api_token}"}
    async with httpx.AsyncClient(headers=headers, timeout=60) as client:
        base = f"{live_server_url}/api/models/{REPO}"
        reference = (await client.get(f"{base}/commit/{forms['full']}")).json()
        assert reference["author"] == "owner"
        for name, rev in forms.items():
            detail = (await client.get(f"{base}/commit/{rev}")).json()
            assert detail == reference, name
            diff = (await client.get(f"{base}/commit/{rev}/diff")).json()
            assert diff["author"] == "owner" and diff["commit_id"] == forms["full"], name
