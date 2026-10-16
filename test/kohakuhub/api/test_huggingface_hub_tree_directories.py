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


# ---- every other way a client lists a nested repository -------------------------------------------------------


def _txt(repo):
    return sorted(f"{repo}/{p}" for p in FILES if p.endswith(".txt"))


@pytest.mark.hf_client
async def test_list_repo_files_is_the_files_and_only_the_files(nested):
    api, repo = nested
    assert sorted(await _run(api.list_repo_files, repo)) == sorted(FILES)


@pytest.mark.hf_client
async def test_paths_info_of_a_folder_and_of_files(nested):
    api, repo = nested
    info = await _run(api.get_paths_info, repo, ["a", "a/b/two.txt", "README.md", "no/such"])
    assert sorted((type(i).__name__, i.path) for i in info) == [
        ("RepoFile", "README.md"),
        ("RepoFile", "a/b/two.txt"),
        ("RepoFolder", "a"),
    ]


@pytest.mark.hf_client
async def test_snapshot_download_has_every_nested_file_and_obeys_patterns(nested, live_server_url, tmp_path):
    from pathlib import Path

    from huggingface_hub import snapshot_download

    api, repo = nested
    everything = await _run(snapshot_download, repo, endpoint=live_server_url, token=api.token, cache_dir=tmp_path / "all")
    assert sorted(str(p.relative_to(everything)) for p in Path(everything).rglob("*") if p.is_file()) == sorted(FILES)

    only_a = await _run(
        snapshot_download, repo, endpoint=live_server_url, token=api.token, cache_dir=tmp_path / "a", allow_patterns=["a/**"]
    )
    assert sorted(str(p.relative_to(only_a)) for p in Path(only_a).rglob("*") if p.is_file()) == sorted(
        p for p in FILES if p.startswith("a/")
    )


# What a client does first must not change what it finds next: ``datasets`` globs everything, then by pattern;
# ``pandas`` and others call find, walk, du, ls or info. Each starts from a fresh file system.
FIRST_CALLS = {
    "glob-all": lambda fs, repo: fs.glob(f"{repo}/**"),
    "glob-pattern": lambda fs, repo: fs.glob(f"{repo}/**/*.txt"),
    "find": lambda fs, repo: fs.find(repo),
    "find-detail": lambda fs, repo: fs.find(repo, detail=True),
    "walk": lambda fs, repo: list(fs.walk(repo)),
    "du": lambda fs, repo: fs.du(repo),
    "ls-root": lambda fs, repo: fs.ls(repo, detail=False),
    "ls-sub": lambda fs, repo: fs.ls(f"{repo}/a", detail=True),
    "info-dir": lambda fs, repo: fs.info(f"{repo}/a/b"),
    "isdir": lambda fs, repo: fs.isdir(f"{repo}/a/b/c"),
    "exists": lambda fs, repo: fs.exists(f"{repo}/z/four.txt"),
    "expand": lambda fs, repo: fs.expand_path(repo, recursive=True),
}


@pytest.mark.hf_client
@pytest.mark.parametrize("first", sorted(FIRST_CALLS))
async def test_what_a_file_system_finds_does_not_depend_on_what_it_did_first(first, nested, live_server_url):
    _, repo = nested
    HfFileSystem.clear_instance_cache()
    fs = HfFileSystem(endpoint=live_server_url)
    await _run(FIRST_CALLS[first], fs, repo)
    assert sorted(await _run(fs.glob, f"{repo}/**/*.txt")) == _txt(repo)
    assert sorted(await _run(fs.find, repo)) == sorted(f"{repo}/{p}" for p in FILES)
    assert sorted(await _run(fs.glob, f"{repo}/a/**")) == sorted(
        [f"{repo}/a"] + [f"{repo}/{p}" for p in FILES + DIRECTORIES if p.startswith("a/")]
    )


@pytest.mark.hf_client
async def test_a_file_system_walks_directories_files_and_sizes(nested, live_server_url):
    _, repo = nested
    HfFileSystem.clear_instance_cache()
    fs = HfFileSystem(endpoint=live_server_url)
    walked = await _run(lambda: {d: (sorted(ds), sorted(fl)) for d, ds, fl in fs.walk(repo)})
    assert walked == {
        repo: (["a", "z"], ["README.md"]),
        f"{repo}/a": (["b"], ["one.txt"]),
        f"{repo}/a/b": (["c"], ["two.txt"]),
        f"{repo}/a/b/c": ([], ["three.txt"]),
        f"{repo}/z": ([], ["four.txt"]),
    }
    assert await _run(fs.du, repo) == sum(len(p) for p in FILES)
    assert (await _run(fs.isdir, f"{repo}/a/b"), await _run(fs.isfile, f"{repo}/a/b/two.txt")) == (True, True)
    assert not await _run(fs.exists, f"{repo}/a/missing")


@pytest.mark.hf_client
async def test_a_recursive_listing_over_several_pages_names_each_directory_once(live_server_url, hf_api_token):
    """An expanded page holds 50 entries: 130 files in four directories make a listing of several pages."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    repo = "owner/nested-pages"
    names = [f"d{d}/f{i:03d}.txt" for d in range(4) for i in range(33)]
    await _run(api.create_repo, repo, exist_ok=True)
    try:
        await _run(
            api.create_commit, repo, operations=[CommitOperationAdd(n, n.encode()) for n in names], commit_message="many"
        )
        entries = await _run(lambda: list(api.list_repo_tree(repo, recursive=True, expand=True)))
        folders = [e.path for e in entries if type(e).__name__ == "RepoFolder"]
        files = [e.path for e in entries if type(e).__name__ == "RepoFile"]
        assert sorted(folders) == ["d0", "d1", "d2", "d3"]  # each once, though the pages are several
        assert sorted(files) == sorted(names)
    finally:
        await _run(api.delete_repo, repo)


@pytest.mark.hf_client
async def test_a_folder_is_named_only_while_it_has_files(nested):
    api, repo = nested
    await _run(api.delete_folder, "a/b", repo)
    entries = await _run(lambda: list(api.list_repo_tree(repo, recursive=True)))
    assert sorted(e.path for e in entries if type(e).__name__ == "RepoFolder") == ["a", "z"]
    assert sorted(await _run(api.list_repo_files, repo)) == ["README.md", "a/one.txt", "z/four.txt"]
