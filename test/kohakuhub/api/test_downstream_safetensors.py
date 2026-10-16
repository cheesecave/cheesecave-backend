"""``safetensors`` files of real Hub repositories, read through the live server.

The corpus keeps only the header of a weights file (the tensor data is never kept). Two kinds of case:

- a header is read over HTTP ranges (``get_safetensors_metadata``), as the Hub's model pages do, and its
  tensors and parameter counts must be those the header says;
- where the whole file is small, a file of the same header with zeros for data is put on the server and
  ``safetensors`` itself opens it: names, dtypes and shapes must be those of the real header.
"""

from __future__ import annotations

import json
import math
import struct

import pytest

pytest.importorskip("safetensors")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, run, seeded  # noqa: E402,F401

FILLED_LIMIT = 6 * 1024 * 1024  # a file of zeros this big or smaller is put on the server
NUMPY_DTYPES = {"F16", "F32", "F64", "I8", "I16", "I32", "I64", "U8", "U16", "U32", "U64", "BOOL"}
MODELS = [r for r in hub_corpus.repos("model") if any(f.get("header_only") for f in r.files)]
WHOLE = [
    r
    for r in MODELS
    if sum(f["full_size"] for f in r.files if f["path"].endswith(".safetensors")) <= FILLED_LIMIT
]
HUB_NAMES = {"model.safetensors", "model.safetensors.index.json"}


def _header(repo, path):
    data = (repo.directory / path).read_bytes()
    (size,) = struct.unpack("<Q", data[:8])
    header = json.loads(data[8 : 8 + size])
    header.pop("__metadata__", None)
    return header


def _weight_files(repo):
    """The files ``get_safetensors_metadata`` reads: ``model.safetensors``, else those of the index."""
    if "model.safetensors" in repo.paths():
        return ["model.safetensors"]
    index = json.loads((repo.directory / "model.safetensors.index.json").read_bytes())
    return sorted(set(index["weight_map"].values()))


def _counts(repo, paths):
    counts: dict[str, int] = {}
    for path in paths:
        for info in _header(repo, path).values():
            counts[info["dtype"]] = counts.get(info["dtype"], 0) + math.prod(info["shape"])
    return counts


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", [r for r in MODELS if HUB_NAMES & set(r.paths())], ids=hub_corpus.param_id)
async def test_a_header_read_over_ranges_is_the_header_in_the_file(repo, seeded):
    from huggingface_hub import get_safetensors_metadata

    source = await seeded(repo)
    meta = await run(get_safetensors_metadata, source)
    assert sorted(meta.files_metadata) == sorted(_weight_files(repo))
    for path, file_meta in meta.files_metadata.items():
        header = _header(repo, path)
        assert {n: (t.dtype, list(t.shape), list(t.data_offsets)) for n, t in file_meta.tensors.items()} == {
            n: (i["dtype"], i["shape"], i["data_offsets"]) for n, i in header.items()
        }
    assert dict(meta.parameter_count) == _counts(repo, _weight_files(repo))


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", [r for r in MODELS if r.hub.get("safetensors")], ids=hub_corpus.param_id)
async def test_the_parameter_count_is_never_a_different_one_than_the_hub_gave(repo, seeded, endpoint, hf_api_token):
    from huggingface_hub import HfApi

    source = await seeded(repo)
    info = await run(HfApi(endpoint=endpoint, token=hf_api_token).model_info, source)
    reported = getattr(info, "safetensors", None)
    if reported is None:
        return  # the server leaves what it cannot be sure of unsaid
    total = reported["total"] if isinstance(reported, dict) else reported.total
    assert total == repo.hub["safetensors"]["total"]


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", WHOLE, ids=hub_corpus.param_id)
async def test_safetensors_opens_a_file_with_the_header_of_the_real_one(repo, endpoint, hf_api_token, tmp_path):
    from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
    from safetensors import safe_open

    api = HfApi(endpoint=endpoint, token=hf_api_token)
    target = f"{hub_corpus.NAMESPACE}/{repo.alias}__zeros"
    weights = [f for f in repo.files if f["path"].endswith(".safetensors") and "/" not in f["path"]]
    await run(api.create_repo, target, exist_ok=True)
    operations = []
    for entry in weights:
        stored = (repo.directory / entry["path"]).read_bytes()
        operations.append(
            CommitOperationAdd(entry["path"], stored + bytes(entry["full_size"] - len(stored)))
        )
    existing = await run(api.list_repo_files, target)
    if sorted(existing) != sorted(op.path_in_repo for op in operations):
        await run(api.create_commit, target, operations=operations, commit_message="zeros")

    for entry in weights:
        path = await run(hf_hub_download, target, entry["path"], cache_dir=tmp_path / "cache")
        header = _header(repo, entry["path"])
        with safe_open(path, framework="np") as handle:
            assert sorted(handle.keys()) == sorted(header)
            for name, info in header.items():
                piece = handle.get_slice(name)
                assert (piece.get_dtype(), piece.get_shape()) == (info["dtype"], info["shape"])
                if info["dtype"] in NUMPY_DTYPES:
                    tensor = handle.get_tensor(name)
                    assert tensor.shape == tuple(info["shape"]) and not tensor.any()
