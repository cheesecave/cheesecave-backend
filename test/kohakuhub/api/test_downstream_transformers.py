"""``transformers`` reading real Hub repositories (config, tokenizer, files, task) from the live server.

Every case is a real repository sampled from the Hub (see ``test/fixtures/hub_corpus``): the library
reads it from the server, and the result must equal what the same library makes of the sampled files on
disk. What the installed version cannot read from disk (a newer architecture, a package it does not have)
is skipped with the reason: it says nothing about the server.
"""

from __future__ import annotations

import pytest

pytest.importorskip("transformers")

# The libraries are tested as they are: their own deprecation warnings (an old transformers passing an
# argument a new huggingface_hub deprecated) say nothing about the server, and the suite makes warnings errors.
pytestmark = [
    pytest.mark.filterwarnings("ignore::FutureWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from test.kohakuhub.support import hub_corpus  # noqa: E402
from test.kohakuhub.support.downstream import endpoint, only_this_host, reference, run, seeded  # noqa: E402,F401

MODELS = hub_corpus.repos("model")
TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Hello, world! 你好,世界!こんにちは 안녕하세요",
    "def f(x):\n    return x ** 2  # 1,234.5",
    "Ünïcödé — naïve café 🚀",
]
TOKENIZER_FILES = {"tokenizer.json", "tokenizer_config.json", "vocab.txt", "vocab.json", "spiece.model", "tokenizer.model"}


def _names(repo):
    """The files at the top of the repository: the ones a library looks for by name."""
    return {path for path in repo.paths() if "/" not in path}


def _has(repo, *names):
    return bool(set(names) & _names(repo))


def _config(source, cache_dir=None):
    from transformers import AutoConfig

    config = AutoConfig.from_pretrained(source, cache_dir=cache_dir).to_dict()
    config.pop("_name_or_path", None)  # the place it was read from
    # some versions name the repository in front of a remote-code file when it is read from the Hub
    config["auto_map"] = {k: v.split("--", 1)[-1] for k, v in config.get("auto_map", {}).items()} or None
    return config


def _tokenizer(source, cache_dir=None):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(source, cache_dir=cache_dir)
    return {
        "type": type(tokenizer).__name__,
        "ids": tokenizer(TEXTS)["input_ids"],
        "vocab": sorted(tokenizer.get_vocab().items()),
        "special": {k: str(v) for k, v in tokenizer.special_tokens_map.items()},
        "length": len(tokenizer),
    }


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", [r for r in MODELS if "config.json" in _names(r)], ids=hub_corpus.param_id)
async def test_a_config_is_what_the_files_say(repo, seeded, tmp_path):
    expected = reference(_config, str(repo.directory))
    source = await seeded(repo)
    assert await run(_config, source, tmp_path / "cache") == expected


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", [r for r in MODELS if _has(r, *TOKENIZER_FILES)], ids=hub_corpus.param_id)
async def test_a_tokenizer_encodes_as_it_does_from_the_files(repo, seeded, tmp_path):
    expected = reference(_tokenizer, str(repo.directory))
    source = await seeded(repo)
    assert await run(_tokenizer, source, tmp_path / "cache") == expected


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", MODELS, ids=hub_corpus.param_id)
async def test_the_files_of_a_repo_are_found_and_the_missing_ones_are_not(repo, seeded, tmp_path):
    from transformers.utils.hub import cached_file, has_file

    source = await seeded(repo)
    for name in sorted(_names(repo) & {"config.json", "tokenizer_config.json", "generation_config.json"}):
        path = await run(cached_file, source, name, cache_dir=tmp_path / "cache")
        assert open(path, "rb").read() == (repo.directory / name).read_bytes()
        assert await run(has_file, source, name)
    assert not await run(has_file, source, "no-such-file.bin")


@pytest.mark.hf_client
@pytest.mark.parametrize("repo", [r for r in MODELS if r.hub.get("pipeline_tag")], ids=hub_corpus.param_id)
async def test_the_task_is_never_a_different_one_than_the_hub_gave(repo, seeded, endpoint, hf_api_token):
    from huggingface_hub import HfApi

    source = await seeded(repo)
    info = await run(HfApi(endpoint=endpoint, token=hf_api_token).model_info, source)
    assert info.pipeline_tag in (None, repo.hub["pipeline_tag"])
    if info.pipeline_tag and info.library_name == "transformers":
        from transformers.pipelines import get_task

        # get_task refuses a model of another library, on the Hub too
        assert await run(get_task, source) == info.pipeline_tag
