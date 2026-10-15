"""A recursive listing names its directories, for ``huggingface_hub`` and ``HfFileSystem`` (found by #16).

``datasets`` and ``pandas`` (``hf://``) resolve patterns through ``HfFileSystem``, which builds its
directory cache from the directory entries of the Hub's recursive tree. Without them a ``glob("**")``
followed by ``glob("**/*")`` found only the files at the top of a repository.
"""

from __future__ import annotations

import asyncio

import pytest
from huggingface_hub import CommitOperationAdd, HfApi, HfFileSystem

FILES = ["README.md", "a/one.txt", "a/b/two.txt", "a/b/c/three.txt", "z/four.txt"]
DIRECTORIES = ["a", "a/b", "a/b/c", "z"]


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
async def nested(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = "owner/nested-directories"
    await _run(api.create_repo, repo, exist_ok=True)
    await _run(
        api.create_commit,
        repo,
        operations=[CommitOperationAdd(path, path.encode()) for path in FILES],
        commit_message="nested",
    )
    yield api, repo
    await _run(api.delete_repo, repo)


@pytest.mark.hf_client
async def test_a_recursive_tree_has_every_directory_and_every_file(nested):
    api, repo = nested
    entries = await _run(lambda: list(api.list_repo_tree(repo, recursive=True)))
    kinds = {type(entry).__name__: sorted(e.path for e in entries if type(e).__name__ == type(entry).__name__) for entry in entries}
    assert kinds["RepoFolder"] == DIRECTORIES
    assert kinds["RepoFile"] == sorted(FILES)


@pytest.mark.hf_client
async def test_a_tree_of_one_level_is_as_before(nested):
    api, repo = nested
    entries = await _run(lambda: list(api.list_repo_tree(repo)))
    assert sorted((type(e).__name__, e.path) for e in entries) == [
        ("RepoFile", "README.md"),
        ("RepoFolder", "a"),
        ("RepoFolder", "z"),
    ]


@pytest.mark.hf_client
async def test_a_glob_after_a_glob_still_finds_the_files_below_the_top(nested, live_server_url):
    _, repo = nested
    HfFileSystem.clear_instance_cache()
    fs = HfFileSystem(endpoint=live_server_url)
    # the order datasets uses: the whole repository first, then by pattern
    everything = await _run(fs.glob, f"{repo}/**")
    assert sorted(everything) == sorted([repo] + [f"{repo}/{p}" for p in FILES + DIRECTORIES])
    assert sorted(await _run(fs.glob, f"{repo}/**/*.txt")) == sorted(f"{repo}/{p}" for p in FILES if p.endswith(".txt"))
    assert sorted(await _run(fs.find, repo)) == sorted(f"{repo}/{p}" for p in FILES)
    assert sorted(await _run(fs.ls, f"{repo}/a", detail=False)) == [f"{repo}/a/b", f"{repo}/a/one.txt"]


@pytest.mark.hf_client
async def test_a_listing_below_a_path_names_the_directories_below_it(nested):
    api, repo = nested
    entries = await _run(lambda: list(api.list_repo_tree(repo, path_in_repo="a", recursive=True)))
    assert sorted((type(e).__name__, e.path) for e in entries) == [
        ("RepoFile", "a/b/c/three.txt"),
        ("RepoFile", "a/b/two.txt"),
        ("RepoFile", "a/one.txt"),
        ("RepoFolder", "a/b"),
        ("RepoFolder", "a/b/c"),
    ]
