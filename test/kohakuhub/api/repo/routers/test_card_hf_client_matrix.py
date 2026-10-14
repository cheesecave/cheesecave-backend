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
