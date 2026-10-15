"""A Space is created with its README card (#95, A): ``create_repo(space_sdk=...)`` through the whole app.

Real database, real LakeFS: the card is a commit like any other (author, usage, index), read back through
the routes a client reads it from.
"""

from __future__ import annotations

import sys

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.backend_per_test


@pytest_asyncio.fixture
async def anonymous(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
        yield session


async def _create(client, name, **extra):
    return await client.post("/api/repos/create", json={"type": "space", "name": name, "private": False, **extra})


async def _files(client, name, repo_type="space"):
    response = await client.get(f"/api/{repo_type}s/owner/{name}/tree/main")
    response.raise_for_status()
    return [item["path"] for item in response.json()]


async def _wait_indexed():
    discovery = sys.modules["kohakuhub.repository_discovery"]
    import asyncio

    while tasks := [*discovery._running.values(), *discovery._st_running.values()]:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("sdk", ["gradio", "streamlit", "docker", "static"])
async def test_a_space_created_with_an_sdk_has_its_card(anonymous, owner_client, sdk):
    assert (await _create(owner_client, "carded", sdk=sdk)).status_code == 200

    info = (await anonymous.get("/api/spaces/owner/carded")).json()

    assert await _files(owner_client, "carded") == ["README.md"]
    assert info["sdk"] == sdk and info["tags"][0] == sdk
    assert info["cardData"] == {"title": "carded", "emoji": "🚀", "colorFrom": "blue", "colorTo": "green", "sdk": sdk, "pinned": False}


async def test_the_card_is_the_first_commit_by_the_one_who_created_the_space(owner_client):
    await _create(owner_client, "authored", sdk="static")

    db = sys.modules["kohakuhub.db"]

    commits = list(db.Commit.select().join(db.Repository).where(db.Repository.full_id == "owner/authored"))

    assert [(c.message, c.username, c.branch) for c in commits] == [("initial commit", "owner", "main")]


async def test_a_space_in_an_organization_gets_its_card_too(anonymous, owner_client):
    assert (await _create(owner_client, "orgspace", sdk="gradio", organization="acme-labs")).status_code == 200

    info = (await anonymous.get("/api/spaces/acme-labs/orgspace")).json()

    assert info["sdk"] == "gradio"


async def test_the_card_counts_in_the_usage_of_the_space(owner_client):
    await _create(owner_client, "counted", sdk="static")
    db = sys.modules["kohakuhub.db"]

    row = db.Repository.get(db.Repository.full_id == "owner/counted")

    assert row.used_bytes > 0 and row.used_bytes == db.File.select().where(db.File.repository == row).get().size


@pytest.mark.parametrize("extra", [{}, {"sdk": None}, {"sdk": "flask"}, {"sdk": ""}])
async def test_a_space_without_a_known_sdk_has_no_card_as_before(anonymous, owner_client, extra):
    assert (await _create(owner_client, "plain", **extra)).status_code == 200

    info = (await anonymous.get("/api/spaces/owner/plain")).json()

    assert await _files(owner_client, "plain") == []
    assert info["sdk"] is None and "cardData" not in info


@pytest.mark.parametrize("repo_type", ["model", "dataset"])
async def test_only_a_space_has_an_sdk_card(owner_client, repo_type):
    response = await owner_client.post("/api/repos/create", json={"type": repo_type, "name": "other", "private": False, "sdk": "gradio"})

    assert response.status_code == 200 and await _files(owner_client, "other", repo_type) == []


async def test_a_card_that_cannot_be_written_does_not_stop_the_space_from_being_created(anonymous, owner_client, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("no commit")

    monkeypatch.setattr(sys.modules["kohakuhub.api.repo.routers.crud"], "commit_files", broken)

    response = await _create(owner_client, "nocard", sdk="gradio")

    assert response.status_code == 200
    assert (await anonymous.get("/api/spaces/owner/nocard")).status_code == 200 and await _files(owner_client, "nocard") == []


async def test_creating_the_same_space_again_does_not_write_a_second_card(owner_client):
    await _create(owner_client, "twice", sdk="gradio")
    db = sys.modules["kohakuhub.db"]
    count = lambda: db.Commit.select().join(db.Repository).where(db.Repository.full_id == "owner/twice").count()
    before = count()

    again = await _create(owner_client, "twice", sdk="static")

    assert again.status_code == 409 and before == 1 and count() == before


async def test_a_readme_uploaded_afterwards_replaces_the_card_and_the_info_follows(anonymous, owner_client):
    import base64
    import json

    await _create(owner_client, "replaced", sdk="static")
    lines = [
        json.dumps({"key": "header", "value": {"summary": "own card"}}),
        json.dumps({"key": "file", "value": {"path": "README.md", "content": base64.b64encode(b"---\nsdk: streamlit\ntitle: Mine\n---\n").decode(), "encoding": "base64"}}),
    ]
    (await owner_client.post("/api/spaces/owner/replaced/commit/main", content="\n".join(lines), headers={"content-type": "application/x-ndjson"})).raise_for_status()

    info = (await anonymous.get("/api/spaces/owner/replaced")).json()

    assert info["sdk"] == "streamlit" and info["cardData"]["title"] == "Mine"


async def test_the_list_and_the_facet_know_the_sdk_without_anyone_asking_for_the_info(anonymous, owner_client):
    await _create(owner_client, "listed-sp", sdk="gradio")
    await _wait_indexed()

    items = (await anonymous.get("/api/spaces", params={"author": "owner", "fallback": "false"})).json()
    discover = (await anonymous.get("/api/spaces/discover", params={"sdk": "gradio"})).json()

    assert next(i for i in items if i["id"] == "owner/listed-sp")["sdk"] == "gradio"
    assert "owner/listed-sp" in [i["id"] for i in discover["items"]]
