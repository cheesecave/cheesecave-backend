"""Collections through the real huggingface_hub client (#134).

``create_collection``, ``get_collection``, ``list_collections``, ``add_collection_item``, ``update_collection_item``,
``update_collection_metadata``, ``delete_collection_item`` and ``delete_collection`` must work as they do against Hugging Face.
The REST side (shapes, paging, limits, privacy) is covered by ``test_collections.py``; this module is the client half.

Conventions used by this module:

* scratch repos live under ``owner/hf-col-*`` and every test makes its collections under a title of its own.
* `asyncio.to_thread` wraps every blocking ``huggingface_hub`` call so the async test runner does not block.
* collections arrived in huggingface_hub 0.20.3, the oldest client of the matrix; ``_api`` skips a client without them.
* the seeded users: ``owner`` and ``member`` are admins of the organization ``acme-labs``, ``visitor`` is its visitor and
  ``outsider`` belongs to nobody.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from huggingface_hub import HfApi

# `huggingface_hub.errors` landed around v0.22; v0.20.3 keeps the exceptions under `huggingface_hub.utils`.
from huggingface_hub.utils import HfHubHTTPError

ORG = "acme-labs"
MODEL, DATASET, SPACE, PRIVATE = (f"owner/hf-col-{name}" for name in ("model", "dataset", "space", "private"))
THEMES = ("orange", "blue", "green", "purple", "pink", "indigo")


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(lambda: func(*args, **kwargs))


async def _create_hf_token(client, name: str) -> str:
    response = await client.post("/api/auth/tokens/create", json={"name": name})
    response.raise_for_status()
    return response.json()["token"]


def _api(endpoint, token):
    api = HfApi(endpoint=endpoint, token=token)
    if not callable(getattr(api, "create_collection", None)):
        pytest.skip("this huggingface_hub has no collections")
    return api


async def _status_of(func, *args, **kwargs):
    try:
        await _run(func, *args, **kwargs)
        return 200
    except HfHubHTTPError as err:
        return err.response.status_code


async def _repos(api):
    """The four repositories the collections hold: model, dataset, space and a private model."""
    for repo_id, repo_type, private in (
        (MODEL, "model", False),
        (DATASET, "dataset", False),
        (SPACE, "space", False),
        (PRIVATE, "model", True),
    ):
        extra = {"space_sdk": "static"} if repo_type == "space" else {}
        await _run(api.create_repo, repo_id, repo_type=repo_type, private=private, exist_ok=True, **extra)


@pytest.fixture
async def member_hf_api_token(member_client):
    return await _create_hf_token(member_client, "hf-col-member")


@pytest.fixture
async def visitor_hf_api_token(visitor_client):
    return await _create_hf_token(visitor_client, "hf-col-visitor")


@pytest.fixture
async def outsider_hf_api_token(outsider_client):
    return await _create_hf_token(outsider_client, "hf-col-outsider")


@pytest.mark.hf_client
async def test_create_collection_returns_a_canonical_slug_and_a_duplicate_is_409(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    col = await _run(api.create_collection, "Hf col create", description="made by the client", private=False)
    assert re.match(r"^owner/hf-col-create-[0-9a-f]{24}$", col.slug), col.slug
    assert col.theme in THEMES and col.items == [] and col.private is False and col.upvotes == 0
    assert col.url.endswith("/collections/" + col.slug), col.url
    assert await _status_of(api.create_collection, "Hf col create") == 409
    assert (await _run(api.create_collection, "Hf col create", exists_ok=True)).slug == col.slug
    assert (await _run(api.get_collection, col.slug)).slug == col.slug


@pytest.mark.hf_client
async def test_add_collection_item_of_every_type_and_what_the_client_is_told_when_it_cannot(
    live_server_url, hf_api_token, outsider_hf_api_token
):
    api = _api(live_server_url, hf_api_token)
    outsider = _api(live_server_url, outsider_hf_api_token)
    await _repos(api)
    col = await _run(api.create_collection, "Hf col items")
    child = await _run(api.create_collection, "Hf col child")
    await _run(api.add_collection_item, col.slug, MODEL, "model", note="Main model, see https://example.org")
    await _run(api.add_collection_item, col.slug, DATASET, "dataset")
    await _run(api.add_collection_item, col.slug, SPACE, "space")
    col = await _run(api.add_collection_item, col.slug, child.slug, "collection", note="A reference")
    assert [i.item_type for i in col.items] == ["model", "dataset", "space", "collection"]
    assert [i.position for i in col.items] == [0, 1, 2, 3]
    assert col.items[0].note == "Main model, see https://example.org"
    assert col.items[3].item_id == child.slug  # the id of a collection item is its slug
    assert all(i.item_object_id for i in col.items)

    assert await _status_of(api.add_collection_item, col.slug, MODEL, "model") == 409
    again = await _run(api.add_collection_item, col.slug, MODEL, "model", exists_ok=True)
    assert len(again.items) == 4
    assert await _status_of(api.add_collection_item, col.slug, "owner/hf-col-missing", "model") == 404
    assert await _status_of(api.add_collection_item, col.slug, "2307.09288", "paper") == 501
    assert await _status_of(api.add_collection_item, col.slug, col.slug, "collection") == 400

    theirs = await _run(outsider.create_collection, "Hf col theirs")  # a private repository does not exist for them
    assert await _status_of(outsider.add_collection_item, theirs.slug, PRIVATE, "model") == 404


@pytest.mark.hf_client
async def test_update_collection_item_and_metadata_and_the_old_slug_still_answers(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    await _repos(api)
    col = await _run(api.create_collection, "Hf col update")
    for repo_id, repo_type in ((MODEL, "model"), (DATASET, "dataset"), (SPACE, "space")):
        col = await _run(api.add_collection_item, col.slug, repo_id, repo_type)
    last = col.items[-1]
    await _run(api.update_collection_item, col.slug, last.item_object_id, note="Moved to the front", position=0)
    got = await _run(api.get_collection, col.slug)
    assert got.items[0].item_object_id == last.item_object_id and got.items[0].note == "Moved to the front"
    assert [i.position for i in got.items] == [0, 1, 2]

    old = col.slug
    renamed = await _run(
        api.update_collection_metadata, old, title="Hf col renamed", description="new text", theme="pink", private=True
    )
    assert renamed.theme == "pink" and renamed.private is True and renamed.description == "new text"
    assert renamed.slug.endswith(old[-24:]) and renamed.slug != old  # the id stays, the slug follows the title
    assert (await _run(api.get_collection, old)).slug == renamed.slug  # the old slug answers 307 to the new one


@pytest.mark.hf_client
async def test_a_private_collection_is_only_for_its_owner(live_server_url, hf_api_token, outsider_hf_api_token):
    api = _api(live_server_url, hf_api_token)
    outsider = _api(live_server_url, outsider_hf_api_token)
    anonymous = HfApi(endpoint=live_server_url, token=False)
    col = await _run(api.create_collection, "Hf col hidden", private=True)
    assert await _status_of(anonymous.get_collection, col.slug) == 404
    assert await _status_of(outsider.get_collection, col.slug) == 404
    assert (await _run(api.get_collection, col.slug)).private is True
    listed = await _run(lambda: [c.slug for c in anonymous.list_collections(owner="owner", limit=100)])
    assert col.slug not in listed
    mine = await _run(lambda: [c.slug for c in api.list_collections(owner="owner", limit=100)])
    assert col.slug in mine


@pytest.mark.hf_client
async def test_organization_members_write_and_a_visitor_reads_but_cannot_write(
    live_server_url, hf_api_token, member_hf_api_token, visitor_hf_api_token, outsider_hf_api_token
):
    api = _api(live_server_url, hf_api_token)
    member = _api(live_server_url, member_hf_api_token)
    visitor = _api(live_server_url, visitor_hf_api_token)
    outsider = _api(live_server_url, outsider_hf_api_token)
    await _repos(api)
    private = await _run(member.create_collection, "Hf col org", namespace=ORG, private=True)
    assert (await _run(visitor.get_collection, private.slug)).title == "Hf col org"
    assert await _status_of(outsider.get_collection, private.slug) == 404
    assert await _status_of(visitor.add_collection_item, private.slug, MODEL, "model") == 403
    await _run(member.add_collection_item, private.slug, MODEL, "model")
    item = (await _run(member.get_collection, private.slug)).items[0]
    await _run(member.delete_collection_item, private.slug, item.item_object_id)
    assert await _status_of(visitor.create_collection, "Hf col visitor", namespace=ORG) == 403
    assert await _status_of(outsider.create_collection, "Hf col outsider", namespace=ORG) == 403


@pytest.mark.hf_client
async def test_list_collections_filters_sorts_and_follows_the_link_header(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    anonymous = HfApi(endpoint=live_server_url, token=False)
    await _repos(api)
    for n in range(12):  # more than the 10 of a page: the client has to follow the Link header
        col = await _run(api.create_collection, f"Hf col paging {n:02d}")
        if n == 0:
            await _run(api.add_collection_item, col.slug, MODEL, "model")
    mine = await _run(lambda: list(api.list_collections(owner="owner", limit=100)))
    assert len(mine) >= 12 and all(c.owner for c in mine)
    assert all(len(c.items) <= 4 for c in mine)  # a list shows 4 items at most
    hits = await _run(lambda: list(anonymous.list_collections(item=f"models/{MODEL}", limit=100)))
    assert hits and any(c.title == "Hf col paging 00" for c in hits)
    assert len(await _run(lambda: list(anonymous.list_collections(sort="upvotes", limit=2)))) == 2
    assert len(await _run(lambda: list(anonymous.list_collections(limit=3)))) == 3
    assert await _status_of(lambda: list(anonymous.list_collections(sort="newest"))) == 400
    seen = await _run(lambda: [c.slug for c in anonymous.list_collections(owner="owner")])  # the pages of the server
    assert len(seen) == len(set(seen)) >= 12, len(seen)


@pytest.mark.hf_client
async def test_the_item_filter_does_not_reveal_a_private_repository(
    live_server_url, hf_api_token, outsider_hf_api_token
):
    api = _api(live_server_url, hf_api_token)
    outsider = _api(live_server_url, outsider_hf_api_token)
    anonymous = HfApi(endpoint=live_server_url, token=False)
    await _repos(api)
    leak = await _run(api.create_collection, "Hf col leak", private=False)  # public, and holds a private repository
    await _run(api.add_collection_item, leak.slug, PRIVATE, "model")
    item = f"models/{PRIVATE}"
    assert leak.slug not in await _run(lambda: [c.slug for c in anonymous.list_collections(item=item)])
    assert leak.slug not in await _run(lambda: [c.slug for c in outsider.list_collections(item=item)])
    assert leak.slug in await _run(lambda: [c.slug for c in api.list_collections(item=item)])
    assert (await _run(anonymous.get_collection, leak.slug)).items == []  # and the repository is not in it for them


@pytest.mark.hf_client
async def test_delete_collection_item_and_delete_collection_with_missing_ok(live_server_url, hf_api_token):
    api = _api(live_server_url, hf_api_token)
    await _repos(api)
    col = await _run(api.create_collection, "Hf col delete")
    col = await _run(api.add_collection_item, col.slug, MODEL, "model")
    gone = col.items[0].item_object_id
    await _run(api.delete_collection_item, col.slug, gone)
    assert gone not in [i.item_object_id for i in (await _run(api.get_collection, col.slug)).items]
    assert await _status_of(api.delete_collection_item, col.slug, gone) == 404
    await _run(api.delete_collection_item, col.slug, gone, missing_ok=True)
    await _run(api.delete_collection, col.slug)
    assert await _status_of(api.get_collection, col.slug) == 404
    await _run(api.delete_collection, col.slug, missing_ok=True)
