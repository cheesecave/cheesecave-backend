"""``huggingface_hub`` with a commit id in the Hub's form (40 hex) or abbreviated, wherever the client sends one (#12).

The Hub says 40 hex and takes any prefix of 5 to 40 as a revision or a ``parent_commit``; LakeFS says 64.
Every route here takes all of those. Real client, real server (the live server and its own event loop).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid

import httpx
import pytest
from huggingface_hub import CommitOperationAdd, HfApi, get_hf_file_metadata, hf_hub_download, hf_hub_url, snapshot_download
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


# ----- what the server says: one form, wherever a client reads a commit (#12) -----


@pytest.fixture
def commit_id_format(monkeypatch):
    cfg = importlib.import_module("kohakuhub.config").cfg

    def set_to(value):
        monkeypatch.setattr(cfg.app, "commit_id_format", value)

    return set_to


def _every_source(api, url, token):
    """The id of the head of main from every place ``huggingface_hub`` reads one."""
    return {
        "model_info().sha": api.model_info(REPO).sha,
        "model_info(revision=main).sha": api.model_info(REPO, revision="main").sha,
        "list_repo_commits()[0]": api.list_repo_commits(REPO)[0].commit_id,
        "list_repo_refs main": next(b.target_commit for b in api.list_repo_refs(REPO).branches if b.name == "main"),
        "X-Repo-Commit (get_hf_file_metadata)": get_hf_file_metadata(
            hf_hub_url(REPO, "README.md", endpoint=url), token=token
        ).commit_hash,
    }


@pytest.mark.hf_client
async def test_every_place_says_the_same_commit_in_the_hubs_form(live_server_url, hf_api_token, commit_id_format):
    commit_id_format("hub")
    api = _api(live_server_url, hf_api_token)
    said = await _run(_every_source, api, live_server_url, hf_api_token)
    assert len(set(said.values())) == 1, said  # not 40 here and 64 there
    assert {len(v) for v in said.values()} == {40}


@pytest.mark.hf_client
async def test_every_commit_a_client_receives_is_a_parent_commit_it_may_send(live_server_url, hf_api_token, commit_id_format):
    """The client refuses a parent_commit longer than 40 hex before it sends anything: chains of commits, and a
    parent taken from any place, only work when every place says 40."""
    commit_id_format("hub")
    api = _api(live_server_url, hf_api_token)

    def chain():
        oids = []
        previous = api.model_info(REPO).sha
        for step in range(3):  # each commit's parent is the oid the last one returned
            info = api.create_commit(
                REPO,
                [CommitOperationAdd(f"chain-{uuid.uuid4().hex[:8]}.txt", b"x")],
                commit_message=f"chain {step}",
                parent_commit=previous,
            )
            oids.append(info.oid)
            previous = info.oid
        for source in (
            lambda: api.model_info(REPO, revision="main").sha,
            lambda: api.list_repo_commits(REPO)[0].commit_id,
            lambda: next(b.target_commit for b in api.list_repo_refs(REPO).branches if b.name == "main"),
        ):
            api.create_commit(
                REPO,
                [CommitOperationAdd(f"from-source-{uuid.uuid4().hex[:8]}.txt", b"x")],
                commit_message="parent from another place",
                parent_commit=source(),
            )
        return oids

    oids = await _run(chain)
    assert {len(o) for o in oids} == {40}


@pytest.mark.hf_client
async def test_snapshot_download_and_hf_hub_download_keep_their_files_in_one_folder(
    live_server_url, hf_api_token, tmp_path, commit_id_format
):
    """diffusers' from_pretrained: hf_hub_download for one file, snapshot_download for the rest, one cache."""
    commit_id_format("hub")
    api = _api(live_server_url, hf_api_token)
    sha = await _run(lambda: api.model_info(REPO).sha)

    def work():
        one = hf_hub_download(REPO, "README.md", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
        snap = snapshot_download(REPO, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
        pinned = snapshot_download(REPO, revision=sha, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
        offline = snapshot_download(REPO, revision=sha, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path, local_files_only=True)
        return one, snap, pinned, offline

    one, snap, pinned, offline = await _run(work)
    assert os.path.basename(os.path.dirname(one)) == sha  # snapshots/<sha>/README.md
    assert snap == pinned == offline == os.path.dirname(one)
    assert os.path.isdir(pinned) and os.path.isfile(os.path.join(pinned, "config.json"))
    assert len(sha) == 40


@pytest.mark.hf_client
async def test_the_full_form_is_one_form_too_and_the_client_refuses_it_as_a_parent(
    live_server_url, hf_api_token, tmp_path, commit_id_format
):
    """The transitional form: every place says 64, none says 40 (the mix is what broke diffusers). huggingface_hub
    itself refuses 64 hex as a parent_commit, which is why this form is not the default."""
    commit_id_format("full")
    api = _api(live_server_url, hf_api_token)
    said = await _run(_every_source, api, live_server_url, hf_api_token)
    assert len(set(said.values())) == 1, said
    assert {len(v) for v in said.values()} == {64}
    sha = said["model_info().sha"]

    def work():
        pinned = snapshot_download(REPO, revision=sha, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
        main = snapshot_download(REPO, endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path)
        return pinned, main

    pinned, main = await _run(work)
    assert pinned == main and os.path.basename(pinned) == sha and os.path.isdir(pinned)
    with pytest.raises(ValueError):
        await _run(api.create_commit, REPO, [CommitOperationAdd("nope.txt", b"x")], commit_message="x", parent_commit=sha)


# ----- the oid of an LFS file (#12 B) -----


def _lfs(info, name):
    """``RepoFile.lfs`` is a dict in the older clients (0.20 to 0.36) and takes attributes in the newer ones."""
    return getattr(info, name) if hasattr(info, name) else info[name]


def _pointer_blob_id(sha256, size):
    import hashlib

    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha256}\nsize {size}\n".encode()
    return hashlib.sha1(b"blob %d\0" % len(pointer) + pointer).hexdigest()


@pytest.mark.hf_client
async def test_the_oid_of_an_lfs_file_is_the_git_blob_id_of_its_pointer_as_on_the_hub(live_server_url, hf_api_token):
    """``RepoFile.blob_id`` is the oid; ``lfs.sha256`` is the content. The Hub's blob id of an LFS file is the one of the
    pointer git stores, what ``model_info(files_metadata=True)`` already said; the tree said the sha256."""
    api = _api(live_server_url, hf_api_token)

    def read():
        tree = {t.path: t for t in api.list_repo_tree(REPO, recursive=True, expand=True) if hasattr(t, "blob_id")}
        paths = {p.path: p for p in api.get_paths_info(REPO, ["weights/model.safetensors", "README.md"])}
        siblings = {s.rfilename: s for s in api.model_info(REPO, files_metadata=True).siblings}
        return tree, paths, siblings

    tree, paths, siblings = await _run(read)
    lfs_path = "weights/model.safetensors"
    for source in (tree, paths):
        item = source[lfs_path]
        assert item.lfs is not None and len(_lfs(item.lfs, "sha256")) == 64
        assert item.blob_id == _pointer_blob_id(_lfs(item.lfs, "sha256"), _lfs(item.lfs, "size")), "the pointer's blob id"
        assert item.blob_id != _lfs(item.lfs, "sha256")
    assert tree[lfs_path].blob_id == paths[lfs_path].blob_id == siblings[lfs_path].blob_id  # one oid everywhere
    # a regular file's oid is its own git blob id, as it was
    assert tree["README.md"].blob_id == paths["README.md"].blob_id == siblings["README.md"].blob_id
    assert tree["README.md"].lfs is None and len(tree["README.md"].blob_id) == 40
