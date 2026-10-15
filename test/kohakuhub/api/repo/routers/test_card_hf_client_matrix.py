"""What ``huggingface_hub`` reads from a repository's README card, for every promised version (#62).

Whole app over HTTP, real database and LakeFS, the real client. A call or field a client version
does not have is skipped. The cards include the ones that break strict clients: odd keys, tags that
are not a list, dates, NaN.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import warnings

import httpx
import pytest

pytestmark = [pytest.mark.backend_per_test, pytest.mark.hf_client]


def _hf():
    import huggingface_hub

    return huggingface_hub


def _validation_goes_to_the_endpoint():
    """``huggingface_hub`` 2.1.x writes the Hub's own address into ``RepoCard.validate`` (and so into
    ``push_to_hub`` and ``metadata_update``): they cannot be pointed at another hub."""
    import inspect

    return "https://huggingface.co/api/validate-yaml" not in inspect.getsource(_hf().repocard.RepoCard.validate)


def _need_card_validation():
    if not _validation_goes_to_the_endpoint():
        pytest.skip(f"huggingface_hub {_hf().__version__} validates cards against huggingface.co whatever the endpoint")


def _need(obj, name):
    if not hasattr(obj, name):
        pytest.skip(f"huggingface_hub {_hf().__version__} has no {name}")
    return getattr(obj, name)


async def _run(function, *args, **kwargs):
    def call():
        with warnings.catch_warnings():  # the suite turns warnings into errors; a client's own notices are not ours
            warnings.simplefilter("ignore", FutureWarning)
            return function(*args, **kwargs)

    return await asyncio.to_thread(call)


@pytest.fixture(autouse=True)
def global_endpoint(live_server_url, monkeypatch):
    """Card calls (``ModelCard.load``, ``push_to_hub``, ``validate``) read the module-level endpoint."""
    importlib.import_module("huggingface_hub.repocard")
    for name, module in list(sys.modules.items()):  # everything that took the endpoint when it was imported
        if not name.startswith("huggingface_hub"):
            continue
        if hasattr(module, "ENDPOINT"):
            monkeypatch.setattr(module, "ENDPOINT", live_server_url)
        if hasattr(module, "HUGGINGFACE_CO_URL_TEMPLATE"):  # hf_hub_url builds download addresses from it
            monkeypatch.setattr(module, "HUGGINGFACE_CO_URL_TEMPLATE", live_server_url + "/{repo_id}/resolve/{revision}/{filename}")
        shared = getattr(module, "api", None)  # the HfApi that upload_file & co are bound to
        if isinstance(shared, _hf().HfApi):
            monkeypatch.setattr(shared, "endpoint", live_server_url)


@pytest.fixture
def api(live_server_url, hf_api_token):
    return _hf().HfApi(endpoint=live_server_url, token=hf_api_token)


@pytest.fixture
def base(live_server_url):
    return live_server_url


async def _repo(api, repo_type, name, readme=None, raw=False):
    """A repository with ``readme``; ``raw`` commits it without the client's own check (it refuses invalid cards)."""
    kwargs = {"space_sdk": "gradio"} if repo_type == "space" else {}
    await _run(api.create_repo, f"owner/{name}", repo_type=repo_type, exist_ok=True, **kwargs)
    if readme is not None and raw:
        lines = [
            json.dumps({"key": "header", "value": {"summary": "readme"}}),
            json.dumps({"key": "file", "value": {"path": "README.md", "content": base64.b64encode(readme.encode()).decode(), "encoding": "base64"}}),
        ]
        response = httpx.post(
            f"{api.endpoint}/api/{repo_type}s/owner/{name}/commit/main",
            content="\n".join(lines),
            headers={"content-type": "application/x-ndjson", "authorization": f"Bearer {api.token}"},
        )
        response.raise_for_status()
    elif readme is not None:
        await _run(api.upload_file, path_or_fileobj=readme.encode(), path_in_repo="README.md", repo_id=f"owner/{name}", repo_type=repo_type)
    return f"owner/{name}"


MODEL_CARD = "---\nlicense: apache-2.0\nlanguage:\n- en\ntags:\n- exbert\nlibrary_name: transformers\npipeline_tag: text-generation\ndatasets:\n- bookcorpus\nbase_model: org/base\n---\n# A model\n"


# --- what model_info / dataset_info / space_info read -----------------------------


async def test_model_info_reads_the_declared_fields(api):
    rid = await _repo(api, "model", "carded", MODEL_CARD)

    info = await _run(api.model_info, rid)

    assert info.pipeline_tag == "text-generation" and info.library_name == "transformers"
    assert info.tags[0] == "transformers" and "license:apache-2.0" in info.tags and "exbert" in info.tags and "dataset:bookcorpus" in info.tags
    assert info.card_data is not None and info.card_data.license == "apache-2.0" and info.card_data.pipeline_tag == "text-generation"


async def test_dataset_info_reads_its_card_and_tags(api):
    rid = await _repo(api, "dataset", "corpus", "---\nlicense: cc-by-4.0\ntask_categories:\n- translation\nlanguage:\n- fr\npaperswithcode_id: wmt\n---\n")

    info = await _run(api.dataset_info, rid)

    assert "task_categories:translation" in info.tags and "language:fr" in info.tags and "license:cc-by-4.0" in info.tags
    assert info.card_data is not None and info.card_data.license == "cc-by-4.0"
    assert getattr(info, "paperswithcode_id", "wmt") == "wmt"


async def test_space_info_reads_the_sdk_and_tags(api):
    rid = await _repo(api, "space", "demo", "---\nsdk: gradio\ntitle: Demo\ntags:\n- leaderboard\n---\n")

    info = await _run(api.space_info, rid)

    assert info.sdk == "gradio" and "gradio" in info.tags and "leaderboard" in info.tags
    assert info.card_data is not None and info.card_data.to_dict()["title"] == "Demo"


@pytest.mark.parametrize("repo_type,reader", [("model", "model_info"), ("dataset", "dataset_info"), ("space", "space_info")])
async def test_a_repository_without_a_card_reads_cleanly(api, repo_type, reader):
    if repo_type == "space":
        # the client always sends an sdk, which now gives the Space its card (#95): a Space without one
        # comes from a create call that has none
        response = httpx.post(f"{api.endpoint}/api/repos/create", json={"type": "space", "name": "bare", "private": False}, headers={"authorization": f"Bearer {api.token}"})
        response.raise_for_status()
        rid = "owner/bare"
    else:
        rid = await _repo(api, repo_type, "bare")

    info = await _run(getattr(api, reader), rid)

    assert info.card_data is None and "region:us" in info.tags and info.id == rid


NASTY_CARDS = {
    "non-string keys": "---\n1: one\n2.5: two\ntrue: yes\nlicense: mit\n---\n",
    "hf_hub option key": "---\nignore_metadata_errors: true\nlicense: mit\n---\n",
    "tags as a string": "---\ntags: just-one\nlicense: mit\n---\n",
    "tags null": "---\ntags:\nlicense: mit\n---\n",
    "tags with numbers": "---\ntags: [a, 1, 2.5, null]\nlicense: mit\n---\n",
    "dates": "---\ncreated: 2020-01-02\nwhen: 2020-01-02 03:04:05\nlicense: mit\n---\n",
    "not a number": "---\nscore: .nan\nbig: .inf\nlicense: mit\n---\n",
    "nested lists": "---\nmodel-index:\n- name: m\n  results:\n  - task: {type: text-classification}\n    metrics: [{type: f1, value: 0.9, verified: true}]\n---\n",
    "unicode": "---\ntitle: \"模型 🧀\"\nlicense: mit\n---\n",
    "license list": "---\nlicense: [mit, apache-2.0]\n---\n",
    "empty front matter": "---\n---\n# x\n",
    "broken yaml": "---\nlicense: [broken\n  bad: : yaml\n---\n# x\n",
    "list at the top": "---\n- a\n- b\n---\n",
    "no closing line": "---\nlicense: mit\n# x\n",
}


# Every odd card on a model (the card is read the same for every type), a few on the other two
_ODD_ON_OTHERS = ("non-string keys", "tags as a string", "dates")
ODD = [("model", "model_info", name) for name in sorted(NASTY_CARDS)] + [
    (kind, reader, name) for kind, reader in (("dataset", "dataset_info"), ("space", "space_info")) for name in _ODD_ON_OTHERS
]


@pytest.mark.parametrize("repo_type,reader,name", ODD, ids=[f"{k}-{n}" for k, _, n in ODD])
async def test_no_card_however_odd_breaks_the_info_call(api, name, repo_type, reader):
    slug = "".join(c for c in name.lower() if c.isalnum())[:20]
    rid = await _repo(api, repo_type, slug, NASTY_CARDS[name], raw=True)

    info = await _run(getattr(api, reader), rid)

    assert info.id == rid and isinstance(info.tags, list) and all(isinstance(t, str) for t in info.tags)


async def test_hf_hub_marks_nothing_verified_that_a_readme_says_is(api):
    rid = await _repo(api, "model", "claims", NASTY_CARDS["nested lists"], raw=True)

    info = await _run(api.model_info, rid)

    index = getattr(info, "model_index", None) or info.card_data.to_dict().get("model-index")
    assert index and index[0]["results"][0]["metrics"][0]["verified"] is False


# --- expand ---------------------------------------------------------------


async def test_expand_by_the_client_returns_what_was_asked(api):
    rid = await _repo(api, "model", "expanded", MODEL_CARD)
    if "expand" not in __import__("inspect").signature(api.model_info).parameters:
        pytest.skip("this huggingface_hub has no expand")

    info = await _run(api.model_info, rid, expand=["cardData", "tags", "pipeline_tag", "library_name"])

    assert info.pipeline_tag == "text-generation" and info.library_name == "transformers" and "exbert" in info.tags and info.card_data.license == "apache-2.0"


@pytest.mark.parametrize("query", ["expand=cardData&expand=tags", "expand[]=cardData&expand[]=tags"])
async def test_expand_in_either_spelling_on_the_wire(api, base, query):
    rid = await _repo(api, "model", "wire", MODEL_CARD)

    body = httpx.get(f"{base}/api/models/{rid}?{query}").json()

    assert sorted(body) == ["_id", "cardData", "id", "tags"]


# --- lists ------------------------------------------------------------------


async def test_lists_carry_the_declared_fields_once_the_repositories_were_read(api):
    for kind, reader, name, card in (
        ("model", "model_info", "m1", MODEL_CARD),
        ("dataset", "dataset_info", "d1", "---\ntask_categories: [translation]\n---\n"),
        ("space", "space_info", "s1", "---\nsdk: streamlit\n---\n"),
    ):
        rid = await _repo(api, kind, name, card)
        await _run(getattr(api, reader), rid)  # the first read indexes it

    models = await _run(lambda: list(api.list_models(author="owner")))
    datasets = await _run(lambda: list(api.list_datasets(author="owner")))
    spaces = await _run(lambda: list(api.list_spaces(author="owner")))

    model = next(m for m in models if m.id == "owner/m1")
    assert model.pipeline_tag == "text-generation" and model.library_name == "transformers" and "exbert" in model.tags
    assert "task_categories:translation" in next(d for d in datasets if d.id == "owner/d1").tags
    assert next(s for s in spaces if s.id == "owner/s1").sdk == "streamlit"


async def test_a_list_with_the_card_has_it_in_every_item(api):
    rid = await _repo(api, "model", "listcard", MODEL_CARD)
    await _run(api.model_info, rid)
    if "cardData" not in __import__("inspect").signature(api.list_models).parameters:
        pytest.skip("this huggingface_hub cannot ask for card data in a list")

    models = await _run(lambda: list(api.list_models(author="owner", cardData=True)))

    assert next(m for m in models if m.id == rid).card_data.license == "apache-2.0"


# --- cards the way users push them ----------------------------------------------


async def test_a_card_pushed_with_the_client_is_visible_in_the_next_model_info(api, hf_api_token):
    _need_card_validation()
    ModelCard = _need(_hf(), "ModelCard")
    ModelCardData = _need(_hf(), "ModelCardData")
    rid = await _repo(api, "model", "pushed")

    card = ModelCard.from_template(ModelCardData(license="mit", pipeline_tag="fill-mask", library_name="transformers", tags=["pushed"]))
    await _run(card.push_to_hub, rid, token=hf_api_token)
    info = await _run(api.model_info, rid)

    assert info.pipeline_tag == "fill-mask" and "license:mit" in info.tags and "pushed" in info.tags


async def test_a_card_is_read_back_with_the_client(api):
    ModelCard = _need(_hf(), "ModelCard")
    rid = await _repo(api, "model", "readback", MODEL_CARD)

    card = await _run(ModelCard.load, rid)

    assert card.data.license == "apache-2.0" and card.text.strip() == "# A model"


async def test_the_client_validates_a_card_through_the_hub(api):
    """``RepoCard.validate`` asks ``/api/validate-yaml``. (A card the Hub refuses cannot even be built
    by the client; what it refuses on upload is the test below.)"""
    _need_card_validation()
    ModelCard = _need(_hf(), "ModelCard")

    await _run(ModelCard(MODEL_CARD).validate, "model")  # no error
    await _run(ModelCard("# no metadata at all\n").validate, "model")  # a warning on the Hub, not an error


@pytest.mark.parametrize("card", ["---\nlicense: [broken\n  bad: : yaml\n---\n# x\n", "---\n- a\n- b\n---\n"])
async def test_uploading_a_readme_with_unreadable_metadata_is_refused_by_the_client(api, card):
    _need(api, "_validate_yaml")  # older clients upload a README without asking the Hub
    rid = await _repo(api, "model", "refused")

    with pytest.raises(ValueError) as refused:
        await _run(api.upload_file, path_or_fileobj=card.encode(), path_in_repo="README.md", repo_id=rid)

    assert "Invalid metadata in README.md" in str(refused.value)


async def test_metadata_update_through_the_client_changes_the_card_and_the_info(api, hf_api_token):
    _need_card_validation()
    metadata_update = _need(_hf(), "metadata_update")
    rid = await _repo(api, "model", "updated", "---\nlicense: mit\n---\n# x\n")

    await _run(metadata_update, rid, {"pipeline_tag": "text-classification"}, token=hf_api_token)
    info = await _run(api.model_info, rid)

    assert info.pipeline_tag == "text-classification" and "license:mit" in info.tags


# --- what the files say, read by the client (#63) -------------------------------------

BERT_CONFIG = {"model_type": "bert", "architectures": ["BertForMaskedLM"], "hidden_size": 8}


async def _model_with_files(api, name, files: dict[str, bytes], readme=None):
    rid = await _repo(api, "model", name, readme)
    for path, content in files.items():
        await _run(api.upload_file, path_or_fileobj=content, path_in_repo=path, repo_id=rid)
    return rid


async def test_the_client_reads_the_library_task_tags_and_config_the_files_give(api):
    rid = await _model_with_files(api, "fromfiles", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": b"weights"})

    info = await _run(api.model_info, rid)

    assert info.library_name == "transformers" and info.pipeline_tag == "fill-mask"
    assert info.tags[:4] == ["transformers", "safetensors", "bert", "fill-mask"]
    if hasattr(info, "config"):
        assert info.config == {"model_type": "bert", "architectures": ["BertForMaskedLM"]}
    assert getattr(info, "transformers_info", None) is None  # not claimed: it is only right 9 times in 10


async def test_what_the_card_says_beats_the_files_for_the_client_too(api):
    rid = await _model_with_files(
        api, "cardwins", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": b"w"}, "---\nlibrary_name: timm\npipeline_tag: image-classification\n---\n"
    )

    info = await _run(api.model_info, rid)

    assert (info.library_name, info.pipeline_tag) == ("timm", "image-classification")


async def test_the_client_reads_no_library_where_the_files_do_not_settle_it(api):
    rid = await _model_with_files(api, "unsettled", {"config.json": json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}).encode()})

    info = await _run(api.model_info, rid)

    assert info.library_name is None and info.pipeline_tag is None  # config.json alone: no weights, not recognisably transformers


async def test_the_client_reads_text_generation_for_a_language_model_which_transformers_needs(api):
    """``transformers.pipeline(model=id)`` asks the task of the repository: without it the call raises."""
    rid = await _model_with_files(api, "llm", {"config.json": json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}).encode(), "model.safetensors": b"w"})

    info = await _run(api.model_info, rid)

    assert info.library_name == "transformers" and info.pipeline_tag == "text-generation"


async def test_expand_config_by_the_client(api):
    if "expand" not in __import__("inspect").signature(api.model_info).parameters:
        pytest.skip("this huggingface_hub cannot expand model info")
    rid = await _model_with_files(api, "expandcfg", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": b"w"})

    info = await _run(api.model_info, rid, expand=["config", "library_name"])

    assert info.library_name == "transformers"
    if hasattr(info, "config"):
        assert info.config["model_type"] == "bert"


async def test_a_list_shows_the_derived_fields_after_a_commit_without_any_info_call(api):
    rid = await _model_with_files(api, "listed-files", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": b"w"})

    item = None
    for _ in range(40):  # the commit scheduled the index in the background
        models = await _run(lambda: list(api.list_models(author="owner")))
        item = next(m for m in models if m.id == rid)
        if item.library_name:
            break
        await asyncio.sleep(0.25)

    assert item.library_name == "transformers" and item.pipeline_tag == "fill-mask" and "bert" in item.tags


async def test_the_client_sees_the_files_change_with_the_next_commit(api):
    rid = await _model_with_files(api, "changing", {"config.json": json.dumps(BERT_CONFIG).encode()})
    first = await _run(api.model_info, rid)

    await _run(api.upload_file, path_or_fileobj=b"w", path_in_repo="model.safetensors", repo_id=rid)
    second = await _run(api.model_info, rid)

    assert first.library_name is None and second.library_name == "transformers"


# --- the summary of the weights, read by the client (#63, part 3) ------------------------


def _weights(**arrays):
    np = pytest.importorskip("numpy")
    from safetensors.numpy import save

    return save({name: np.zeros(shape, dtype=dtype) for name, (dtype, shape) in arrays.items()})


def _st(summary):
    """``(total, parameters)`` of ``ModelInfo.safetensors``: a dataclass in newer clients, a plain dict in 0.20."""
    if isinstance(summary, dict):
        return summary["total"], dict(summary["parameters"])
    return summary.total, dict(summary.parameters)


async def _summary_of(api, rid):
    """``model_info(...).safetensors``, waited for: the commit sums the weights in the background."""
    info = None
    for _ in range(60):
        info = await _run(api.model_info, rid)
        if info.safetensors is not None:
            break
        await asyncio.sleep(0.25)
    return info


async def test_the_client_reads_the_parameter_count_of_the_weights(api):
    rid = await _model_with_files(api, "counted-st", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": _weights(a=("float32", (4, 5)), b=("float16", (7,)))})

    info = await _summary_of(api, rid)

    assert _st(info.safetensors) == (27, {"F32": 20, "F16": 7})


async def test_the_summary_is_what_the_client_computes_from_the_files_itself(api):
    """``get_safetensors_metadata`` reads the headers through the resolve route and adds them up: the
    two must agree, or one of them reads the files wrongly."""
    rid = await _model_with_files(api, "crosscheck", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": _weights(a=("float32", (6, 6)), b=("float16", (10,)))})

    info = await _summary_of(api, rid)
    metadata = await _run(api.get_safetensors_metadata, rid)

    total, parameters = _st(info.safetensors)
    assert total == sum(metadata.parameter_count.values()) and parameters == dict(metadata.parameter_count)


async def test_a_sharded_model_is_summed_for_the_client_too(api):
    files = {
        "config.json": json.dumps(BERT_CONFIG).encode(),
        # the client finds the shards through the index, as every loader does
        "model.safetensors.index.json": json.dumps({"metadata": {}, "weight_map": {"a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}).encode(),
        "model-00001-of-00002.safetensors": _weights(a=("float32", (10,))),
        "model-00002-of-00002.safetensors": _weights(b=("float32", (5,))),
    }
    rid = await _model_with_files(api, "sharded-st", files)

    info = await _summary_of(api, rid)
    metadata = await _run(api.get_safetensors_metadata, rid)

    assert _st(info.safetensors)[0] == 15 == sum(metadata.parameter_count.values())


async def test_expand_safetensors_by_the_client(api):
    if "expand" not in __import__("inspect").signature(api.model_info).parameters:
        pytest.skip("this huggingface_hub cannot expand model info")
    rid = await _model_with_files(api, "expand-st", {"config.json": json.dumps(BERT_CONFIG).encode(), "model.safetensors": _weights(a=("float32", (3,)))})
    await _summary_of(api, rid)

    info = await _run(api.model_info, rid, expand=["safetensors", "library_name"])

    assert _st(info.safetensors)[0] == 3 and info.library_name == "transformers"


async def test_a_model_the_rules_do_not_sum_has_no_summary_and_the_client_copes(api):
    rid = await _model_with_files(api, "quant-st", {"config.json": json.dumps({**BERT_CONFIG, "quantization_config": {"bits": 4}}).encode(), "model.safetensors": _weights(a=("uint32", (8,)))})
    await asyncio.sleep(2)  # the summary would have been written by now

    info = await _run(api.model_info, rid)

    assert info.safetensors is None and info.library_name == "transformers"


# --- a Space created through the client has its card (#95, A) ---------------------------------


@pytest.mark.parametrize("sdk", ["gradio", "streamlit", "docker", "static"])
async def test_a_space_created_with_space_sdk_reads_back_with_its_sdk(api, sdk):
    """``create_repo(repo_type="space", space_sdk=...)``, then what ``space_info`` reads."""
    await _run(api.create_repo, f"owner/sp-{sdk}", repo_type="space", space_sdk=sdk)

    info = await _run(api.space_info, f"owner/sp-{sdk}")

    assert info.sdk == sdk and info.tags[0] == sdk
    assert info.card_data is not None and info.card_data.to_dict()["title"] == f"sp-{sdk}"


async def test_the_card_of_a_new_space_is_a_file_the_client_lists_and_reads(api):
    await _run(api.create_repo, "owner/sp-files", repo_type="space", space_sdk="gradio")

    files = await _run(api.list_repo_files, "owner/sp-files", repo_type="space")
    path = await _run(api.hf_hub_download, "owner/sp-files", "README.md", repo_type="space")

    assert files == ["README.md"] and "sdk: gradio" in open(path).read()


async def test_a_space_in_the_list_has_its_sdk_without_anyone_asking_for_its_info(api):
    await _run(api.create_repo, "owner/sp-listed", repo_type="space", space_sdk="streamlit")

    item = None
    for _ in range(40):  # the commit scheduled the index in the background
        spaces = await _run(lambda: list(api.list_spaces(author="owner")))
        item = next(s for s in spaces if s.id == "owner/sp-listed")
        if item.sdk:
            break
        await asyncio.sleep(0.25)

    assert item.sdk == "streamlit"


async def test_a_card_pushed_to_a_new_space_replaces_the_default_one(api):
    await _run(api.create_repo, "owner/sp-replaced", repo_type="space", space_sdk="static")
    await _run(api.upload_file, path_or_fileobj=b"---\nsdk: gradio\ntitle: Mine\n---\n", path_in_repo="README.md", repo_id="owner/sp-replaced", repo_type="space")

    info = await _run(api.space_info, "owner/sp-replaced")

    assert info.sdk == "gradio" and info.card_data.to_dict()["title"] == "Mine"
