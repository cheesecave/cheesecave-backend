"""``pandas`` reading real tables of the Hub with ``hf://`` paths from the live server."""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
pytest.importorskip("fsspec")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, reference, run, seeded  # noqa: E402,F401

READERS = {
    ".parquet": pd.read_parquet,
    ".csv": pd.read_csv,
    ".tsv": lambda path, **kw: pd.read_csv(path, sep="\t", **kw),
    ".jsonl": lambda path, **kw: pd.read_json(path, lines=True, **kw),
    ".ndjson": lambda path, **kw: pd.read_json(path, lines=True, **kw),
    ".json": pd.read_json,
}


def _tables():
    """Every tabular file of every dataset, at most three a repository (the first of each format)."""
    cases = []
    for repo in hub_corpus.repos("dataset"):
        seen = set()
        for path in repo.paths():
            suffix = "." + path.rsplit(".", 1)[-1].lower()
            if suffix in READERS and suffix not in seen and "readme" not in path.lower():
                seen.add(suffix)
                cases.append(pytest.param(repo, path, id=f"{len(cases):03d}-{hub_corpus.param_id(repo)[:30]}"))
    return cases


@pytest.mark.hf_client
@pytest.mark.parametrize("repo, path", _tables())
async def test_a_table_read_through_hf_is_the_table_on_disk(repo, path, seeded):
    read = READERS["." + path.rsplit(".", 1)[-1].lower()]
    expected = reference(read, str(repo.directory / path))
    source = await seeded(repo)
    got = await run(read, f"hf://datasets/{source}/{path}")
    pd.testing.assert_frame_equal(got, expected)
