"""The fields the Hub puts in every list item, through the whole app (#95, B)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.backend_per_test

KINDS = [("model", "models"), ("dataset", "datasets"), ("space", "spaces")]


async def _create(client, kind, name):
    (await client.post("/api/repos/create", json={"type": kind, "name": name, "private": False, **({"sdk": "static"} if kind == "space" else {})})).raise_for_status()


async def _items(client, plural, **params):
    response = await client.get(f"/api/{plural}", params={"author": "owner", "fallback": "false", **params})
    response.raise_for_status()
    return response.json()


@pytest.mark.parametrize("kind,plural", KINDS)
async def test_every_item_has_its_id_as_a_string_and_a_trending_score(owner_client, kind, plural):
    await _create(owner_client, kind, "ided")

    item = next(i for i in await _items(owner_client, plural) if i["id"] == "owner/ided")

    assert isinstance(item["_id"], str) and item["_id"].isdigit()
    assert isinstance(item["trendingScore"], (int, float)) and item["trendingScore"] >= 0


async def test_a_model_item_has_its_model_id_and_the_other_kinds_do_not(owner_client):
    for kind, plural in KINDS:
        await _create(owner_client, kind, "named")

    items = {plural: next(i for i in await _items(owner_client, plural) if i["id"] == "owner/named") for _, plural in KINDS}

    assert items["models"]["modelId"] == "owner/named"
    assert "modelId" not in items["datasets"] and "modelId" not in items["spaces"]
    assert items["datasets"]["disabled"] is False and "disabled" not in items["models"]


async def test_the_scores_of_the_items_are_in_the_order_of_the_trending_sort(owner_client):
    for name in ("a", "b", "c"):
        await _create(owner_client, "model", name)

    scores = [i["trendingScore"] for i in await _items(owner_client, "models", sort="trendingScore")]

    assert scores == sorted(scores, reverse=True)
