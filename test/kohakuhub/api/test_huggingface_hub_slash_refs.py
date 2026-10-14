"""`huggingface_hub` with branches and tags whose names have a `/` (#67).

`create_branch("feature/x")`, `upload_file(revision="feature/x")`, `hf_hub_download(revision="feature/x")`,
`snapshot_download`, `list_repo_refs`, `create_tag("release/v1.0")`: what a team workflow does. The client sends the
name percent-encoded in the path (`feature%2Fx`) on every route. Real LakeFS, MinIO and database behind the live server.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from huggingface_hub import (
    CommitOperationAdd,
    HfApi,
    get_hf_file_metadata,
    hf_hub_download,
    hf_hub_url,
    snapshot_download,
)
from huggingface_hub.utils import HfHubHTTPError

REPO = "owner/demo-model"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    async def clear():
        refs = await _run(api.list_repo_refs, REPO)
        for branch in refs.branches:
            if branch.name != "main":
                await _run(api.delete_branch, REPO, branch=branch.name)
        for tag in refs.tags:
            await _run(api.delete_tag, REPO, tag=tag.name)

    await clear()
    yield api
    await clear()


async def test_a_team_workflow_on_a_feature_branch(api, live_server_url, hf_api_token, tmp_path):
    await _run(api.create_branch, REPO, branch="feature/x")
    refs = await _run(api.list_repo_refs, REPO)
    assert "feature/x" in [b.name for b in refs.branches]

    commit = await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd("dir/a.txt", b"hello from a branch with a slash")],
        commit_message="add a.txt",
        revision="feature/x",
    )
    sha = commit.oid
    assert len(sha) == 40

    info = await _run(api.model_info, REPO, revision="feature/x")
    assert info.sha == sha
    assert (await _run(api.model_info, REPO)).sha != sha  # main did not move

    path = await _run(
        hf_hub_download, REPO, "dir/a.txt", revision="feature/x", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path
    )
    assert open(path, "rb").read() == b"hello from a branch with a slash"
    assert sha in path  # the cache folder is the commit, not the branch name

    meta = await _run(
        get_hf_file_metadata,
        hf_hub_url(REPO, "dir/a.txt", revision="feature/x", endpoint=live_server_url),
        token=hf_api_token,
    )
    assert meta.commit_hash == sha and meta.size == 32

    assert await _run(api.file_exists, REPO, "dir/a.txt", revision="feature/x")
    assert not await _run(api.file_exists, REPO, "dir/missing.txt", revision="feature/x")
    assert not await _run(api.file_exists, REPO, "dir/a.txt")  # main has no such file

    tree = await _run(lambda: [i.path for i in api.list_repo_tree(REPO, revision="feature/x", recursive=True)])
    assert "dir/a.txt" in tree
    sub = await _run(lambda: [i.path for i in api.list_repo_tree(REPO, path_in_repo="dir", revision="feature/x")])
    assert sub == ["dir/a.txt"]
    infos = await _run(api.get_paths_info, REPO, ["dir/a.txt"], revision="feature/x")
    assert [i.path for i in infos] == ["dir/a.txt"]

    # the full ref name the client quotes as refs%2Fheads%2Ffeature%2Fx
    assert (await _run(api.model_info, REPO, revision="refs/heads/feature/x")).sha == sha
    full = await _run(
        hf_hub_download, REPO, "dir/a.txt", revision="refs/heads/feature/x", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path / "full"
    )
    assert open(full, "rb").read() == b"hello from a branch with a slash"

    commits = await _run(api.list_repo_commits, REPO, revision="feature/x")
    assert commits[0].commit_id == sha and len(commits[0].commit_id) == 40

    snapshot = await _run(snapshot_download, REPO, revision="feature/x", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path / "snap")
    assert os.path.basename(snapshot) == sha
    assert open(os.path.join(snapshot, "dir", "a.txt"), "rb").read() == b"hello from a branch with a slash"

    # a commit on top of the head it was made against, and a stale parent refused
    again = await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd("b.txt", b"b")],
        commit_message="add b",
        revision="feature/x",
        parent_commit=sha,
    )
    assert again.oid != sha
    with pytest.raises(HfHubHTTPError) as stale:
        await _run(
            api.create_commit,
            REPO,
            operations=[CommitOperationAdd("c.txt", b"c")],
            commit_message="add c",
            revision="feature/x",
            parent_commit=sha,
        )
    assert stale.value.response.status_code == 412

    await _run(api.delete_branch, REPO, branch="feature/x")
    assert "feature/x" not in [b.name for b in (await _run(api.list_repo_refs, REPO)).branches]


async def test_a_release_tag_with_a_slash(api, live_server_url, hf_api_token, tmp_path):
    await _run(api.create_branch, REPO, branch="release/prep")
    await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd("VERSION", b"1.0")],
        commit_message="version",
        revision="release/prep",
    )
    head = (await _run(api.model_info, REPO, revision="release/prep")).sha
    await _run(api.create_tag, REPO, tag="release/v1.0", revision="release/prep")

    refs = await _run(api.list_repo_refs, REPO)
    assert {t.name: t.target_commit for t in refs.tags}["release/v1.0"] == head
    assert (await _run(api.model_info, REPO, revision="release/v1.0")).sha == head
    path = await _run(
        hf_hub_download, REPO, "VERSION", revision="release/v1.0", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path
    )
    assert open(path, "rb").read() == b"1.0"
    with pytest.raises(HfHubHTTPError):  # the tag exists
        await _run(api.create_tag, REPO, tag="release/v1.0", revision="release/prep")

    await _run(api.delete_tag, REPO, tag="release/v1.0")
    assert "release/v1.0" not in [t.name for t in (await _run(api.list_repo_refs, REPO)).tags]


async def test_a_branch_from_a_slash_branch_and_names_git_refuses(api):
    await _run(api.create_branch, REPO, branch="feature/one")
    await _run(api.create_branch, REPO, branch="feature/two", revision="feature/one")
    names = [b.name for b in (await _run(api.list_repo_refs, REPO)).branches]
    assert {"feature/one", "feature/two"} <= set(names)

    for bad in ("a..b", "feature/x.lock", "a b", "feature//x"):
        with pytest.raises(HfHubHTTPError) as caught:
            await _run(api.create_branch, REPO, branch=bad)
        assert caught.value.response.status_code == 400, bad
    with pytest.raises(HfHubHTTPError) as conflict:  # `feature` would hold `feature/one`
        await _run(api.create_branch, REPO, branch="feature")
    assert conflict.value.response.status_code == 409


async def test_an_unknown_slash_revision_is_a_missing_revision_not_a_missing_file(api, live_server_url, hf_api_token, tmp_path):
    from huggingface_hub.utils import RevisionNotFoundError

    with pytest.raises(RevisionNotFoundError):
        await _run(
            hf_hub_download, REPO, "README.md", revision="no/such/branch", endpoint=live_server_url, token=hf_api_token, cache_dir=tmp_path
        )
    with pytest.raises(RevisionNotFoundError):
        await _run(api.model_info, REPO, revision="no/such/branch")
