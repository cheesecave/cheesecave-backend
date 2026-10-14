"""``GET /api/{type}s/{id}`` and the lists answer what the README card declares (#62).

Whole app, real database, real LakeFS: the README is committed through the API like any file.
"""

from __future__ import annotations

import base64
import json

import pytest
import pytest_asyncio

pytestmark = pytest.mark.backend_per_test

CARD = (
    "---\nlicense: MIT\nlibrary_name: transformers\npipeline_tag: text-generation\n"
    "tags:\n- exbert\nlanguage: en\ndatasets:\n- bookcorpus\nmodel-index:\n- name: m\n  results:\n  - metrics:\n    - type: acc\n      value: 1\n---\n# Model\n"
)


async def _create(owner_client, repo_type, name):
    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": name, "private": False})).raise_for_status()


async def _commit(owner_client, repo_type, name, readme: str | None, message="readme"):
    lines = [json.dumps({"key": "header", "value": {"summary": message}})]
    if readme is not None:
        lines.append(json.dumps({"key": "file", "value": {"path": "README.md", "content": base64.b64encode(readme.encode()).decode(), "encoding": "base64"}}))
    else:
        lines.append(json.dumps({"key": "deletedFile", "value": {"path": "README.md"}}))
    response = await owner_client.post(f"/api/{repo_type}s/owner/{name}/commit/main", content="\n".join(lines), headers={"content-type": "application/x-ndjson"})
    response.raise_for_status()


@pytest_asyncio.fixture
async def anonymous(app):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
        yield session


async def test_a_models_info_answers_its_card_at_once(anonymous, owner_client):
    await _create(owner_client, "model", "carded")
    await _commit(owner_client, "model", "carded", CARD)

    body = (await anonymous.get("/api/models/owner/carded")).json()

    assert body["library_name"] == "transformers" and body["pipeline_tag"] == "text-generation"
    assert body["tags"] == ["transformers", "text-generation", "exbert", "en", "dataset:bookcorpus", "license:mit", "region:us"]
    assert body["cardData"]["license"] == "MIT" and body["cardData"]["tags"] == ["exbert"]
    assert body["model-index"][0]["results"][0]["metrics"] == [{"type": "acc", "value": 1, "verified": False}]
    assert body["_id"].isdigit() and body["modelId"] == "owner/carded"


async def test_a_readme_changed_by_a_commit_shows_in_the_next_info(anonymous, owner_client):
    await _create(owner_client, "model", "moving")
    await _commit(owner_client, "model", "moving", "---\nlicense: mit\n---\n")
    first = (await anonymous.get("/api/models/owner/moving")).json()

    await _commit(owner_client, "model", "moving", "---\nlicense: apache-2.0\npipeline_tag: fill-mask\n---\n")
    second = (await anonymous.get("/api/models/owner/moving")).json()

    assert "license:mit" in first["tags"] and first["pipeline_tag"] is None
    assert "license:apache-2.0" in second["tags"] and second["pipeline_tag"] == "fill-mask"


async def test_removing_the_readme_removes_the_card(anonymous, owner_client):
    await _create(owner_client, "model", "bare")
    await _commit(owner_client, "model", "bare", "---\nlicense: mit\n---\n")
    assert "cardData" in (await anonymous.get("/api/models/owner/bare")).json()

    await _commit(owner_client, "model", "bare", None)
    body = (await anonymous.get("/api/models/owner/bare")).json()

    assert "cardData" not in body and body["tags"] == ["region:us"] and body["pipeline_tag"] is None


async def test_a_repository_without_a_readme_has_no_card_fields(anonymous, owner_client):
    await _create(owner_client, "model", "empty")

    body = (await anonymous.get("/api/models/owner/empty")).json()

    assert "cardData" not in body and "model-index" not in body and body["tags"] == ["region:us"]


async def test_a_dataset_and_a_space_have_their_own_shapes(anonymous, owner_client):
    await _create(owner_client, "dataset", "corpus")
    await _commit(owner_client, "dataset", "corpus", "---\ntask_categories: [translation]\nlanguage: fr\npaperswithcode_id: wmt\nsize_categories: [1K<n<10K]\n---\n")
    await _create(owner_client, "space", "demo")
    await _commit(owner_client, "space", "demo", "---\nsdk: gradio\ntags: [leaderboard]\n---\n")

    dataset = (await anonymous.get("/api/datasets/owner/corpus")).json()
    space = (await anonymous.get("/api/spaces/owner/demo")).json()

    assert dataset["tags"] == ["task_categories:translation", "language:fr", "region:us"]
    assert dataset["paperswithcode_id"] == "wmt" and "modelId" not in dataset and "pipeline_tag" not in dataset
    assert space["sdk"] == "gradio" and space["tags"] == ["gradio", "leaderboard", "region:us"] and "downloads" not in space


@pytest.mark.parametrize("query", ["expand=cardData&expand=tags", "expand[]=cardData&expand[]=tags", "expand=cardData&expand[]=tags"])
async def test_expand_is_read_in_both_spellings(anonymous, owner_client, query):
    await _create(owner_client, "model", "spelled")
    await _commit(owner_client, "model", "spelled", "---\nlicense: mit\n---\n")

    body = (await anonymous.get(f"/api/models/owner/spelled?{query}")).json()

    assert sorted(body) == ["_id", "cardData", "id", "tags"]
    assert body["cardData"] == {"license": "mit"}


async def test_an_expand_that_does_not_need_the_card_does_not_read_it(anonymous, owner_client, monkeypatch):
    import sys

    await _create(owner_client, "model", "plain")
    discovery = sys.modules["kohakuhub.repository_discovery"]
    reads = []
    real = discovery.info_card

    async def counting(repo, head=None):
        reads.append(repo.id)
        return await real(repo, head)

    monkeypatch.setattr(sys.modules["kohakuhub.api.repo.routers.info"].discovery, "info_card", counting)

    await anonymous.get("/api/models/owner/plain?expand=sha&expand=downloads")
    assert reads == []
    await anonymous.get("/api/models/owner/plain?expand=pipeline_tag")
    assert len(reads) == 1


async def test_an_unreadable_card_does_not_fail_the_info(anonymous, owner_client, monkeypatch):
    import sys

    await _create(owner_client, "model", "unknown")

    async def nothing(repo, head=None):
        return None

    monkeypatch.setattr(sys.modules["kohakuhub.api.repo.routers.info"].discovery, "info_card", nothing)

    response = await anonymous.get("/api/models/owner/unknown")

    assert response.status_code == 200 and response.json()["tags"] == [] and "cardData" not in response.json()


# --- the lists --------------------------------------------------------------


async def test_list_items_carry_the_declared_fields_once_indexed(anonymous, owner_client):
    import sys

    await _create(owner_client, "model", "listed")
    await _commit(owner_client, "model", "listed", CARD)
    await anonymous.get("/api/models/owner/listed")  # the info route indexes it

    items = (await anonymous.get("/api/models", params={"author": "owner", "fallback": "false"})).json()
    item = next(i for i in items if i["id"] == "owner/listed")

    assert item["tags"][:2] == ["transformers", "text-generation"] and item["pipeline_tag"] == "text-generation"
    assert item["library_name"] == "transformers" and "cardData" not in item


@pytest.mark.parametrize("flag", ["cardData=true", "full=true"])
async def test_the_list_gives_the_card_when_asked(anonymous, owner_client, flag):
    await _create(owner_client, "model", "withcard")
    await _commit(owner_client, "model", "withcard", "---\nlicense: mit\n---\n")
    await anonymous.get("/api/models/owner/withcard")

    items = (await anonymous.get(f"/api/models?author=owner&fallback=false&{flag}")).json()

    assert next(i for i in items if i["id"] == "owner/withcard")["cardData"] == {"license": "mit"}


async def test_list_items_of_repositories_not_indexed_yet_have_empty_declared_fields(anonymous, owner_client):
    await _create(owner_client, "dataset", "raw")

    item = next(i for i in (await anonymous.get("/api/datasets", params={"author": "owner", "fallback": "false"})).json() if i["id"] == "owner/raw")

    assert item["tags"] == [] and "pipeline_tag" not in item


async def test_space_list_items_carry_the_sdk(anonymous, owner_client):
    await _create(owner_client, "space", "listedspace")
    await _commit(owner_client, "space", "listedspace", "---\nsdk: streamlit\n---\n")
    await anonymous.get("/api/spaces/owner/listedspace")

    item = next(i for i in (await anonymous.get("/api/spaces", params={"author": "owner", "fallback": "false"})).json() if i["id"] == "owner/listedspace")

    assert item["sdk"] == "streamlit" and item["tags"] == ["streamlit", "region:us"]
