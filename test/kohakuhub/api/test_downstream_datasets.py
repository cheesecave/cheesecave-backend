"""``datasets`` loading real Hub dataset repositories from the live server.

The reference is ``load_dataset`` on the sampled directory: the same builder, the same files; what comes
from the server must have the same splits, features and rows, whether loaded or streamed.
"""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("datasets")
pytest.importorskip("pyarrow")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, reference, run, seeded  # noqa: E402,F401

DATASETS = hub_corpus.repos("dataset")
HEAD = 5  # rows compared one by one; the whole split goes into the digest


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _describe(source, cache_dir):
    from datasets import load_dataset

    loaded = load_dataset(source, cache_dir=str(cache_dir))
    return {
        split: {
            "rows": ds.num_rows,
            "columns": ds.column_names,
            "features": str(ds.features),
            "head": [ds[i] for i in range(min(HEAD, ds.num_rows))],
            "digest": _digest(ds.to_dict()),
        }
        for split, ds in sorted(loaded.items())
    }


def _stream(source):
    from datasets import load_dataset

    loaded = load_dataset(source, streaming=True)
    out = {}
    for split, ds in sorted(loaded.items()):
        out[split] = [row for _, row in zip(range(HEAD), ds)]
    return out


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", DATASETS, ids=hub_corpus.param_id)
async def test_a_dataset_loads_with_the_same_splits_features_and_rows(repo, seeded, tmp_path):
    expected = reference(_describe, str(repo.directory), tmp_path / "local")
    source = await seeded(repo)
    assert await run(_describe, source, tmp_path / "server") == expected


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", DATASETS, ids=hub_corpus.param_id)
async def test_a_dataset_streams_the_same_first_rows(repo, seeded):
    expected = reference(_stream, str(repo.directory))
    source = await seeded(repo)
    assert await run(_stream, source) == expected


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", DATASETS, ids=hub_corpus.param_id)
async def test_the_configs_and_splits_are_the_same(repo, seeded):
    from datasets import get_dataset_config_names, get_dataset_split_names

    def names(source):
        return {config: get_dataset_split_names(source, config) for config in get_dataset_config_names(source)}

    expected = reference(names, str(repo.directory))
    source = await seeded(repo)
    assert await run(names, source) == expected
