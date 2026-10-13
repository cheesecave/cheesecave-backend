"""``auth-check`` and ``*-tags-by-type``: the Hub's preflight and tag vocabulary routes.

Whole app, real database, real LakeFS: repositories are created through the API, and the
tag vocabulary is read from what discovery indexed.
"""

from __future__ import annotations

import sys

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.backend_per_test

PLURALS = [("model", "models"), ("dataset", "datasets"), ("space", "spaces")]


@pytest_asyncio.fixture
async def anonymous(app):
    """No cookie: ``owner_client`` is the shared ``client`` logged in, so it cannot be used as the stranger."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
        yield session


async def _create(owner_client, repo_type, name, private=False):
    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": name, "private": private})).raise_for_status()


def _index(repo_type, name, *facets):
    db = sys.modules["kohakuhub.db"]
    repo = db.Repository.get(
        (db.Repository.repo_type == repo_type) & (db.Repository.namespace == "owner") & (db.Repository.name == name)
    )
    for key, value in facets:
        db.RepositoryFacet.create(repository=repo, key=key, value=value)
    db.RepositoryMetadata.get_or_create(repository=repo, defaults={"state": "ready"})
    db.RepositoryMetadata.update(state="ready").where(db.RepositoryMetadata.repository == repo).execute()


@pytest.mark.parametrize("kind,plural", PLURALS)
async def test_a_readable_repository_passes_the_preflight_for_anyone(anonymous, owner_client, kind, plural):
    await _create(owner_client, kind, "open")

    assert (await anonymous.get(f"/api/{plural}/owner/open/auth-check")).status_code == 200
    assert (await owner_client.get(f"/api/{plural}/owner/open/auth-check")).status_code == 200


@pytest.mark.parametrize("kind,plural", PLURALS)
async def test_a_private_repository_passes_for_its_owner_and_is_not_found_for_others(
    anonymous, owner_client, outsider_client, kind, plural
):
    await _create(owner_client, kind, "closed", private=True)

    assert (await owner_client.get(f"/api/{plural}/owner/closed/auth-check")).status_code == 200
    for other in (anonymous, outsider_client):
        denied = await other.get(f"/api/{plural}/owner/closed/auth-check")
        assert denied.status_code == 404 and denied.headers["x-error-code"] == "RepoNotFound"


async def test_a_missing_repository_is_not_found_with_the_hubs_code(anonymous):
    missing = await anonymous.get("/api/models/owner/nothing/auth-check")

    assert missing.status_code == 404 and missing.headers["x-error-code"] == "RepoNotFound"


async def test_a_missing_repository_falls_back_to_the_sources_like_the_other_routes(anonymous, monkeypatch):
    calls = []

    async def source_has_it(repo_type, namespace, name, *args, **kwargs):
        calls.append((repo_type, namespace, name))
        return {"id": f"{namespace}/{name}"}

    from kohakuhub.api.fallback import decorators

    monkeypatch.setattr(decorators, "try_fallback_info", source_has_it)
    monkeypatch.setattr(decorators.cfg.fallback, "enabled", True)
    monkeypatch.setattr(decorators, "get_enabled_sources", lambda *a, **k: [{"name": "S", "url": "http://s", "source_type": "huggingface"}])

    response = await anonymous.get("/api/models/google/bert-base/auth-check")

    assert response.status_code == 200 and calls == [("model", "google", "bert-base")]


async def test_the_model_tag_vocabulary_is_what_this_hub_indexed_in_the_hubs_shape(anonymous, owner_client):
    await _create(owner_client, "model", "bert")
    await _create(owner_client, "model", "hidden", private=True)
    _index("model", "bert", ("task", "fill-mask"), ("library", "transformers"), ("language", "en"), ("license", "mit"), ("tag", "bert"))
    _index("model", "hidden", ("task", "secret-task"), ("license", "proprietary"))

    body = (await anonymous.get("/api/models-tags-by-type")).json()

    assert body["pipeline_tag"] == [{"id": "fill-mask", "label": "fill-mask", "type": "pipeline_tag"}]
    assert [t["id"] for t in body["library"]] == ["transformers"] and [t["id"] for t in body["language"]] == ["en"]
    assert [t["id"] for t in body["license"]] == ["mit"]  # the private repository's task and license are not leaked
    assert [t["id"] for t in body["other"]] == ["bert"]
    assert body["dataset"] == body["region"] == body["deploy"] == []


async def test_the_dataset_tag_vocabulary_uses_the_dataset_names(anonymous, owner_client):
    await _create(owner_client, "dataset", "corpus")
    _index("dataset", "corpus", ("task", "translation"), ("size", "1k"), ("language", "fr"), ("license", "cc-by-4.0"))

    body = (await anonymous.get("/api/datasets-tags-by-type")).json()

    assert [t["id"] for t in body["task_categories"]] == ["translation"]
    assert [t["id"] for t in body["size_categories"]] == ["1k"] and [t["id"] for t in body["language"]] == ["fr"]
    assert [t["id"] for t in body["license"]] == ["cc-by-4.0"]
    assert body["benchmark"] == body["language_creators"] == body["multilinguality"] == body["task_ids"] == []


async def test_the_owner_sees_the_vocabulary_of_the_private_repositories_too(owner_client):
    await _create(owner_client, "model", "hidden", private=True)
    _index("model", "hidden", ("license", "proprietary"))

    body = (await owner_client.get("/api/models-tags-by-type")).json()

    assert [t["id"] for t in body["license"]] == ["proprietary"]


async def test_an_empty_site_has_an_empty_vocabulary(anonymous):
    body = (await anonymous.get("/api/models-tags-by-type")).json()

    assert set(body) >= {"pipeline_tag", "library", "language", "license"} and not any(body.values())
