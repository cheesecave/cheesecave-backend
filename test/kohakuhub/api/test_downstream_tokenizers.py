"""``tokenizers`` reading the ``tokenizer.json`` of real Hub repositories through the live server."""

from __future__ import annotations

import pytest

pytest.importorskip("tokenizers")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, reference, run, seeded  # noqa: E402,F401

REPOS = [r for r in hub_corpus.repos("model") if "tokenizer.json" in r.paths()]
TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Hello, world! 你好,世界!こんにちは 안녕하세요",
    "def f(x):\n    return x ** 2  # 1,234.5",
    "Ünïcödé — naïve café 🚀",
    "",
]


def _read(path):
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(path))
    encodings = tokenizer.encode_batch(TEXTS)
    return {
        "text": tokenizer.to_str(),
        "ids": [e.ids for e in encodings],
        "tokens": [e.tokens for e in encodings],
        "offsets": [e.offsets for e in encodings],
        "vocab": sorted(tokenizer.get_vocab().items()),
        "decoded": [tokenizer.decode(e.ids) for e in encodings],
    }


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", REPOS, ids=hub_corpus.param_id)
async def test_a_tokenizer_json_is_read_and_encodes_as_the_original(repo, seeded, tmp_path):
    from huggingface_hub import hf_hub_download

    expected = reference(_read, repo.directory / "tokenizer.json")
    source = await seeded(repo)
    path = await run(hf_hub_download, source, "tokenizer.json", cache_dir=tmp_path / "cache")
    assert await run(_read, path) == expected


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", REPOS, ids=hub_corpus.param_id)
async def test_a_tokenizer_comes_from_the_hub_by_its_name(repo, seeded, tmp_path):
    from tokenizers import Tokenizer

    expected = reference(_read, repo.directory / "tokenizer.json")
    source = await seeded(repo)
    tokenizer = await run(Tokenizer.from_pretrained, source)
    assert tokenizer.to_str() == expected["text"]
    assert [e.ids for e in tokenizer.encode_batch(TEXTS)] == expected["ids"]
