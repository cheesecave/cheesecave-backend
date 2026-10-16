"""``HfFileSystem`` (fsspec) over real Hub repositories on the live server: listing, info and byte ranges."""

from __future__ import annotations

import pytest

pytest.importorskip("fsspec")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, run, seeded  # noqa: E402,F401

REPOS = hub_corpus.repos("model") + hub_corpus.repos("dataset")


def _root(repo, source):
    return f"{repo.type}s/{source}" if repo.type == "dataset" else source


def _fs(endpoint):
    from huggingface_hub import HfFileSystem

    HfFileSystem.clear_instance_cache()
    return HfFileSystem(endpoint=endpoint)


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", REPOS, ids=hub_corpus.param_id)
async def test_the_listing_and_sizes_are_those_of_the_files(repo, seeded, endpoint):
    source = await seeded(repo)
    root = _root(repo, source)
    top = await run(_fs(endpoint).ls, root, detail=False)
    assert sorted(p[len(root) + 1 :] for p in top) == sorted({f["path"].split("/")[0] for f in repo.files})

    found = await run(_fs(endpoint).find, root, detail=True)
    assert {p[len(root) + 1 :]: info["size"] for p, info in found.items()} == {f["path"]: f["size"] for f in repo.files}


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", REPOS, ids=hub_corpus.param_id)
async def test_a_range_of_a_file_is_those_bytes(repo, seeded, endpoint):
    source = await seeded(repo)
    root = _root(repo, source)
    fs = _fs(endpoint)
    entry = max(repo.files, key=lambda f: f["size"])
    data = (repo.directory / entry["path"]).read_bytes()
    start, end = entry["size"] // 3, entry["size"] // 3 + 200
    assert await run(fs.cat_file, f"{root}/{entry['path']}", start=start, end=end) == data[start:end]
    assert await run(fs.cat_file, f"{root}/{entry['path']}") == data

    def seek_read():
        with fs.open(f"{root}/{entry['path']}", "rb") as handle:
            handle.seek(start)
            return handle.read(200)

    assert await run(seek_read) == data[start:end]


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", REPOS, ids=hub_corpus.param_id)
async def test_a_pattern_finds_the_matching_files_only(repo, seeded, endpoint):
    source = await seeded(repo)
    root = _root(repo, source)
    fs = _fs(endpoint)
    suffix = "." + max(repo.files, key=lambda f: f["size"])["path"].rsplit(".", 1)[-1]
    expected = sorted(f"{root}/{f['path']}" for f in repo.files if f["path"].endswith(suffix))
    assert sorted(await run(fs.glob, f"{root}/**/*{suffix}")) == expected
