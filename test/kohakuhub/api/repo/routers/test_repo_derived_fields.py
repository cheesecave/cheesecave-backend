"""What the files of a model say about it, through the whole app (#63).

Real database, real LakeFS: the files are committed through the API like any upload, and the
answers are read the way ``huggingface_hub`` and a browser read them.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.backend_per_test

BERT = json.dumps({"model_type": "bert", "architectures": ["BertForMaskedLM"], "hidden_size": 8})
TOKENIZER = json.dumps({"bos_token": "<s>", "model_max_length": 512})


async def _create(owner_client, name, repo_type="model"):
    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": name, "private": False})).raise_for_status()


async def _commit(owner_client, name, files: dict[str, str | bytes | None], repo_type="model"):
    lines = [json.dumps({"key": "header", "value": {"summary": "files"}})]
    for path, content in files.items():
        if content is None:
            lines.append(json.dumps({"key": "deletedFile", "value": {"path": path}}))
        else:
            raw = content if isinstance(content, bytes) else content.encode()
            lines.append(json.dumps({"key": "file", "value": {"path": path, "content": base64.b64encode(raw).decode(), "encoding": "base64"}}))
    response = await owner_client.post(f"/api/{repo_type}s/owner/{name}/commit/main", content="\n".join(lines), headers={"content-type": "application/x-ndjson"})
    response.raise_for_status()


@pytest.fixture(autouse=True)
def tiny_weights_are_plain_files(monkeypatch):
    """The default rules send every weight file through LFS; a few bytes can be committed as they are."""
    monkeypatch.setattr(sys.modules["kohakuhub.config"].cfg.app, "lfs_suffix_rules_default", [])


@pytest_asyncio.fixture
async def anonymous(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
        yield session


def _discovery():
    return sys.modules["kohakuhub.repository_discovery"]


async def _settled():
    """Every background index, and the weights summary that follows it, this process started has finished."""
    discovery = _discovery()
    while tasks := [*discovery._running.values(), *discovery._st_running.values()]:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _item(anonymous, name):
    items = (await anonymous.get("/api/models", params={"author": "owner", "fallback": "false"})).json()
    return next(i for i in items if i["id"] == f"owner/{name}")


async def test_a_transformers_repository_is_described_by_its_files(anonymous, owner_client):
    await _create(owner_client, "bert")
    await _commit(owner_client, "bert", {"config.json": BERT, "model.safetensors": "x", "tokenizer_config.json": TOKENIZER, "README.md": "---\nlicense: mit\ntags:\n- exbert\n---\n"})

    body = (await anonymous.get("/api/models/owner/bert")).json()

    assert body["library_name"] == "transformers" and body["pipeline_tag"] == "fill-mask"
    assert body["tags"] == ["transformers", "safetensors", "bert", "fill-mask", "exbert", "license:mit", "region:us"]
    assert body["config"] == {"model_type": "bert", "architectures": ["BertForMaskedLM"], "tokenizer_config": {"bos_token": "<s>"}}


async def test_a_repository_with_no_readme_is_still_described_by_its_files(anonymous, owner_client):
    await _create(owner_client, "bare-bert")
    await _commit(owner_client, "bare-bert", {"config.json": BERT, "pytorch_model.bin": "x"})

    body = (await anonymous.get("/api/models/owner/bare-bert")).json()

    assert body["library_name"] == "transformers" and "cardData" not in body
    assert body["tags"] == ["transformers", "pytorch", "bert", "fill-mask", "region:us"]


async def test_what_the_card_declares_wins_over_the_files(anonymous, owner_client):
    await _create(owner_client, "declared")
    await _commit(owner_client, "declared", {"config.json": BERT, "model.safetensors": "x", "README.md": "---\nlibrary_name: timm\npipeline_tag: image-classification\n---\n"})

    body = (await anonymous.get("/api/models/owner/declared")).json()

    assert (body["library_name"], body["pipeline_tag"]) == ("timm", "image-classification")


async def test_nothing_is_said_where_the_files_do_not_say_it(anonymous, owner_client):
    await _create(owner_client, "unclear")
    config = json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]})
    await _commit(owner_client, "unclear", {"config.json": config})  # no weights: not recognisably transformers

    body = (await anonymous.get("/api/models/owner/unclear")).json()

    assert body["library_name"] is None and body["pipeline_tag"] is None and body["tags"] == ["llama", "region:us"]  # the model type is a fact too
    assert body["config"] == {"model_type": "llama", "architectures": ["LlamaForCausalLM"]}  # the config itself is a fact


async def test_a_language_model_generates_text_unless_its_card_says_translation(anonymous, owner_client):
    llama = json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]})
    await _create(owner_client, "llm")
    await _commit(owner_client, "llm", {"config.json": llama, "model.safetensors": "x"})
    await _create(owner_client, "mt")
    await _commit(owner_client, "mt", {"config.json": llama, "model.safetensors": "x", "README.md": "---\ntags:\n- translation\n---\n"})

    llm = (await anonymous.get("/api/models/owner/llm")).json()
    mt = (await anonymous.get("/api/models/owner/mt")).json()

    assert llm["pipeline_tag"] == "text-generation" and "text-generation" in llm["tags"]
    assert mt["pipeline_tag"] == "translation" and "text-generation" not in mt["tags"]


async def test_a_diffusers_repository_names_its_pipeline_class(anonymous, owner_client):
    await _create(owner_client, "diffuse")
    await _commit(owner_client, "diffuse", {"model_index.json": json.dumps({"_class_name": "FluxPipeline"}), "unet/diffusion_pytorch_model.safetensors": "x"})

    body = (await anonymous.get("/api/models/owner/diffuse")).json()

    assert body["library_name"] == "diffusers"
    assert body["tags"] == ["diffusers", "safetensors", "diffusers:FluxPipeline", "region:us"]
    assert body["config"] == {"diffusers": {"_class_name": "FluxPipeline"}}


async def test_a_commit_that_changes_the_files_changes_the_next_info(anonymous, owner_client):
    await _create(owner_client, "growing")
    await _commit(owner_client, "growing", {"config.json": BERT})
    first = (await anonymous.get("/api/models/owner/growing")).json()

    await _commit(owner_client, "growing", {"model.safetensors": "x"})
    second = (await anonymous.get("/api/models/owner/growing")).json()
    await _commit(owner_client, "growing", {"model.safetensors": None, "config.json": None})
    third = (await anonymous.get("/api/models/owner/growing")).json()

    assert first["library_name"] is None and second["library_name"] == "transformers"
    assert third["library_name"] is None and "config" not in third


async def test_expand_config_answers_the_config_and_only_it(anonymous, owner_client):
    await _create(owner_client, "expanded")
    await _commit(owner_client, "expanded", {"config.json": BERT, "model.safetensors": "x"})

    body = (await anonymous.get("/api/models/owner/expanded", params=[("expand", "config"), ("expand", "library_name")])).json()

    assert set(body) == {"_id", "id", "config", "library_name"}
    assert body["config"]["model_type"] == "bert" and body["library_name"] == "transformers"


async def test_expand_config_is_null_for_a_repository_without_one(anonymous, owner_client):
    await _create(owner_client, "no-config")

    assert (await anonymous.get("/api/models/owner/no-config", params={"expand": "config"})).json()["config"] is None


# --- the lists and the facets ------------------------------------------------


async def test_a_commit_is_indexed_without_anyone_asking(anonymous, owner_client):
    """The list shows the derived fields without any info request: the commit scheduled the index."""
    await _create(owner_client, "unasked")
    await _commit(owner_client, "unasked", {"config.json": BERT, "model.safetensors": "x"})
    await _settled()

    item = await _item(anonymous, "unasked")

    assert item["library_name"] == "transformers" and item["pipeline_tag"] == "fill-mask"
    assert item["tags"][:4] == ["transformers", "safetensors", "bert", "fill-mask"]


async def test_the_facets_offer_what_the_files_say(anonymous, owner_client):
    await _create(owner_client, "faceted")
    await _commit(owner_client, "faceted", {"config.json": BERT, "model.safetensors": "x"})
    await _settled()

    body = (await anonymous.get("/api/models/discover", params={"library": "transformers", "task": "fill-mask", "tag": "bert"})).json()

    assert "owner/faceted" in [item["id"] for item in body["items"]]


async def test_a_list_shows_what_was_known_while_the_new_commit_is_indexed(anonymous, owner_client, monkeypatch):
    await _create(owner_client, "stale")
    await _commit(owner_client, "stale", {"config.json": BERT, "model.safetensors": "x"})
    await _settled()
    monkeypatch.setattr(_discovery(), "schedule_repository", lambda repo_id: None)  # the next index does not happen yet

    await _commit(owner_client, "stale", {"README.md": "---\nlicense: mit\n---\n"})
    item = await _item(anonymous, "stale")

    assert item["library_name"] == "transformers"  # the last known, not nothing


async def test_the_info_follows_the_head_even_when_an_invalidation_was_lost(anonymous, owner_client, monkeypatch):
    await _create(owner_client, "lost")
    await _commit(owner_client, "lost", {"config.json": BERT})
    assert (await anonymous.get("/api/models/owner/lost")).json()["library_name"] is None
    monkeypatch.setattr(_discovery(), "mark_dirty", lambda repo_id: None)  # the commit cannot even say it moved
    monkeypatch.setattr(_discovery(), "schedule_repository", lambda repo_id: None)

    await _commit(owner_client, "lost", {"model.safetensors": "x"})
    body = (await anonymous.get("/api/models/owner/lost")).json()

    assert body["library_name"] == "transformers"


async def test_a_move_indexes_the_repository_under_its_new_name(anonymous, owner_client):
    await _create(owner_client, "before")
    await _commit(owner_client, "before", {"config.json": BERT, "model.safetensors": "x"})
    scheduled = []
    real = _discovery().schedule_repository

    def spy(repo_id):
        scheduled.append(repo_id)
        real(repo_id)

    sys.modules["kohakuhub.repository_discovery"].schedule_repository = spy
    try:
        response = await owner_client.post("/api/repos/move", json={"fromRepo": "owner/before", "toRepo": "owner/after", "type": "model"})
        response.raise_for_status()
        await _settled()
    finally:
        sys.modules["kohakuhub.repository_discovery"].schedule_repository = real

    assert len(scheduled) == 1
    assert (await anonymous.get("/api/models/owner/after")).json()["library_name"] == "transformers"


# --- the summary of the weights (#63, part 3) ----------------------------------


def _weights(**arrays):
    """A real safetensors file (the library writes it), ``name=(dtype, shape)``."""
    np = pytest.importorskip("numpy")
    from safetensors.numpy import save

    return save({name: np.zeros(shape, dtype=dtype) for name, (dtype, shape) in arrays.items()})


async def test_the_info_carries_the_parameter_count_of_the_weights(anonymous, owner_client):
    await _create(owner_client, "counted")
    await _commit(owner_client, "counted", {"config.json": BERT, "model.safetensors": _weights(a=("float32", (4, 5)), b=("float16", (7,)), s=("float32", ()))})
    await _settled()

    body = (await anonymous.get("/api/models/owner/counted")).json()

    assert body["safetensors"] == {"parameters": {"F32": 20, "F16": 7}, "total": 27}


async def test_expand_safetensors_answers_it_and_only_it(anonymous, owner_client):
    await _create(owner_client, "expanded-st")
    await _commit(owner_client, "expanded-st", {"config.json": BERT, "model.safetensors": _weights(a=("float32", (3,)))})
    await _settled()

    body = (await anonymous.get("/api/models/owner/expanded-st", params={"expand": "safetensors"})).json()

    assert body == {"_id": body["_id"], "id": "owner/expanded-st", "safetensors": {"parameters": {"F32": 3}, "total": 3}}


async def test_the_shards_of_a_model_are_summed_together(anonymous, owner_client):
    await _create(owner_client, "sharded")
    index = json.dumps({"metadata": {}, "weight_map": {}})
    await _commit(owner_client, "sharded", {
        "config.json": BERT,
        "model.safetensors.index.json": index,
        "model-00001-of-00002.safetensors": _weights(a=("float32", (10,))),
        "model-00002-of-00002.safetensors": _weights(b=("float32", (5,)), c=("float16", (2,))),
    })
    await _settled()

    body = (await anonymous.get("/api/models/owner/sharded")).json()

    assert body["safetensors"] == {"parameters": {"F32": 15, "F16": 2}, "total": 17}


async def test_a_commit_that_changes_the_weights_changes_the_summary(anonymous, owner_client):
    await _create(owner_client, "changing-st")
    await _commit(owner_client, "changing-st", {"config.json": BERT, "model.safetensors": _weights(a=("float32", (3,)))})
    await _settled()
    first = (await anonymous.get("/api/models/owner/changing-st")).json()["safetensors"]

    await _commit(owner_client, "changing-st", {"model.safetensors": _weights(a=("float32", (3,)), b=("float32", (4,)))})
    await _settled()
    second = (await anonymous.get("/api/models/owner/changing-st")).json()["safetensors"]
    await _commit(owner_client, "changing-st", {"model.safetensors": None})
    await _settled()
    third = (await anonymous.get("/api/models/owner/changing-st")).json()

    assert first["total"] == 3 and second["total"] == 7
    assert "safetensors" not in third


async def test_no_summary_is_better_than_a_wrong_one(anonymous, owner_client):
    quantized = json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"], "quantization": {"bits": 4}})
    await _create(owner_client, "quant")
    await _commit(owner_client, "quant", {"config.json": quantized, "model.safetensors": _weights(a=("uint32", (8,)))})
    await _create(owner_client, "two-sets")
    await _commit(owner_client, "two-sets", {"config.json": BERT, "model.safetensors": _weights(a=("float32", (3,))), "model-00001-of-00001.safetensors": _weights(a=("float32", (3,)))})
    await _create(owner_client, "broken")
    await _commit(owner_client, "broken", {"config.json": BERT, "model.safetensors": b"this is not a safetensors file at all"})
    await _settled()

    for name in ("quant", "two-sets", "broken"):
        body = (await anonymous.get(f"/api/models/owner/{name}")).json()
        assert "safetensors" not in body and body["library_name"] == "transformers", name


async def test_the_summary_does_not_flicker_when_the_index_runs_again(anonymous, owner_client):
    await _create(owner_client, "steady")
    await _commit(owner_client, "steady", {"config.json": BERT, "model.safetensors": _weights(a=("float32", (3,)))})
    await _settled()
    discovery = _discovery()
    repo = discovery.Repository.get(discovery.Repository.full_id == "owner/steady")

    discovery.mark_dirty(repo.id)  # what the scheduled refresh does
    await discovery.index_repository(repo.id)

    assert (await anonymous.get("/api/models/owner/steady")).json()["safetensors"]["total"] == 3


async def test_two_hundred_shards_are_summed_in_the_background_without_holding_anything_up(anonymous, owner_client):
    import time

    count = 200
    shards = {f"model-{i:05}-of-{count:05}.safetensors": _weights(**{f"w{i}": ("float32", (i + 1,))}) for i in range(1, count + 1)}
    await _create(owner_client, "big")
    started = time.perf_counter()
    await _commit(owner_client, "big", {"config.json": BERT, **shards})
    committed = time.perf_counter() - started

    answered = time.perf_counter()
    body = (await anonymous.get("/api/models/owner/big")).json()
    info_seconds = time.perf_counter() - answered
    await _settled()
    summed = time.perf_counter() - started
    final = (await anonymous.get("/api/models/owner/big")).json()

    print(f"PERF 200 shards: commit {committed:.2f}s, info answered in {info_seconds:.2f}s (no wait for the summary), summary done {summed:.2f}s after the commit began")
    assert info_seconds < 4  # an info request never waits for the headers
    assert final["safetensors"]["total"] == sum(range(2, count + 2))
    assert summed - committed < 60
