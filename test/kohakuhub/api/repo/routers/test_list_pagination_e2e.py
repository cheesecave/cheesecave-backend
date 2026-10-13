"""``GET /api/models`` as the Hub's clients use it: pages, ``Link``, filters, sorts, sources.

Real database, real app. The fallback source is a Hugging Face shaped server over real HTTP
(``support/list_hf_server.py``), so a list that spans this hub and a source is read by the real
``huggingface_hub`` client, which follows ``Link`` by itself.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlsplit

import pytest

from test.kohakuhub.support.list_hf_server import build_list_hf_app, make_item
from test.kohakuhub.support.live_server import start_live_server, stop_live_server

pytestmark = pytest.mark.backend_per_test

EXTERNAL = 2500  # more than two Hub pages


@pytest.fixture
def source(prepared_backend_test_state, monkeypatch):
    cfg = prepared_backend_test_state.modules.config_module.cfg
    app = build_list_hf_app(
        {"model": [make_item("model", f"ext/m{i:04d}", likes=i, trendingScore=i) for i in range(EXTERNAL)]}
    )
    handle = start_live_server(app)
    monkeypatch.setattr(cfg.fallback, "enabled", True)
    monkeypatch.setattr(
        cfg.fallback,
        "sources",
        [{"name": "MockHF", "url": handle.base_url, "source_type": "huggingface", "priority": 1}],
    )
    try:
        yield app
    finally:
        stop_live_server(handle)


async def _create(owner_client, names, repo_type="model"):
    for name in names:
        (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": name, "private": False})).raise_for_status()


async def _follow(client, url, params):
    """Every page of a list, following ``Link`` the way a client does."""
    pages = []
    while url:
        response = await client.get(url, params=params)
        response.raise_for_status()
        pages.append(response)
        link = response.links.get("next")
        url, params = (urlsplit(link["url"]).path + "?" + urlsplit(link["url"]).query, None) if link else (None, None)
    return pages


async def test_a_list_is_cut_into_pages_by_link_and_the_last_has_none(client, owner_client):
    await _create(owner_client, [f"page-{i}" for i in range(5)])

    pages = await _follow(client, "/api/models", {"author": "owner", "limit": 2, "fallback": "false"})

    ids = [item["id"] for page in pages for item in page.json()]
    assert len(ids) == len(set(ids)) >= 6  # the seed's model and the five
    assert all(len(page.json()) <= 2 for page in pages)
    assert all("next" in page.links for page in pages[:-1]) and "next" not in pages[-1].links
    assert pages[0].links["next"]["url"].startswith("http")  # absolute, like the Hub's


async def test_the_next_link_keeps_the_query_and_repeated_parameters(client, owner_client):
    await _create(owner_client, ["a1", "a2", "a3"])

    first = await client.get(
        "/api/models",
        params=[("author", "owner"), ("limit", "1"), ("fallback", "false"), ("expand", "x"), ("expand", "y")],
    )

    query = parse_qsl(urlsplit(first.links["next"]["url"]).query)
    assert [v for n, v in query if n == "expand"] == ["x", "y"] and ("author", "owner") in query
    assert sum(1 for n, _ in query if n == "cursor") == 1


@pytest.mark.parametrize("hub_name,ours", [("lastModified", "updated"), ("createdAt", "recent"), ("trendingScore", "trending"), ("likes", "likes")])
async def test_hub_sort_names_are_the_same_order_as_ours(client, owner_client, hub_name, ours):
    await _create(owner_client, ["s1", "s2"])

    theirs = await client.get("/api/models", params={"sort": hub_name, "fallback": "false"})
    mine = await client.get("/api/models", params={"sort": ours, "fallback": "false"})

    assert theirs.status_code == 200 and theirs.json() == mine.json()


async def test_direction_flips_the_order(client, owner_client):
    await _create(owner_client, ["d1", "d2"])

    down = await client.get("/api/models", params={"sort": "createdAt", "direction": -1, "fallback": "false"})
    up = await client.get("/api/models", params={"sort": "createdAt", "direction": 1, "fallback": "false"})

    assert [i["id"] for i in up.json()] == [i["id"] for i in reversed(down.json())]


async def test_search_matches_a_part_of_the_name_and_ignores_case(client, owner_client):
    await _create(owner_client, ["Needle-one", "haystack"])

    response = await client.get("/api/models", params={"search": "needle", "fallback": "false"})

    assert [i["id"] for i in response.json()] == ["owner/Needle-one"]


@pytest.mark.parametrize(
    "params",
    [{"sort": "nonsense"}, {"direction": 2}, {"cursor": "garbage"}, {"cursor": "a.b"}],
)
async def test_bad_parameters_are_refused_with_400(client, params):
    response = await client.get("/api/models", params={**params, "fallback": "false"})

    assert response.status_code == 400 and response.headers.get("x-error-message")


async def test_a_cursor_of_another_query_is_refused(client, owner_client):
    await _create(owner_client, ["c1", "c2"])
    first = await client.get("/api/models", params={"limit": 1, "fallback": "false"})
    cursor = dict(parse_qsl(urlsplit(first.links["next"]["url"]).query))["cursor"]

    other = await client.get("/api/models", params={"limit": 1, "author": "elsewhere", "cursor": cursor, "fallback": "false"})

    assert other.status_code == 400


async def test_unknown_parameters_are_ignored_like_the_hub_does(client):
    response = await client.get("/api/models", params={"full": "true", "config": "true", "fallback": "false"})

    assert response.status_code == 200


async def test_a_private_repository_is_not_listed_or_counted_for_others(client, owner_client):
    (await owner_client.post("/api/repos/create", json={"type": "model", "name": "hidden", "private": True})).raise_for_status()
    seen = await _follow(owner_client, "/api/models", {"author": "owner", "limit": 1, "fallback": "false"})
    assert "owner/hidden" in {i["id"] for p in seen for i in p.json()}

    await owner_client.post("/api/auth/logout")
    anonymous = await _follow(client, "/api/models", {"author": "owner", "limit": 1, "fallback": "false"})

    assert "owner/hidden" not in {i["id"] for p in anonymous for i in p.json()}


async def test_local_items_are_marked_when_a_source_is_configured(client, source):
    response = await client.get("/api/models", params={"author": "owner", "limit": 1})

    first = response.json()[0]
    assert first["_source"] == "local" and first["_source_url"]


async def test_a_list_over_this_hub_and_a_source_is_complete_over_hf_hub(live_server_url, hf_api_token, source, owner_client):
    from huggingface_hub import HfApi

    await _create(owner_client, ["mix-a", "mix-b"])
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    everything = await asyncio.to_thread(lambda: [m.id for m in api.list_models(sort="likes")])

    external = [i for i in everything if i.startswith("ext/")]
    assert len(external) == EXTERNAL and len(set(everything)) == len(everything)
    assert everything.index("owner/mix-a") < everything.index(external[0])  # this hub's own come first
    assert external == sorted(external, reverse=True)  # the source's order is kept


async def test_a_limit_is_honoured_across_the_hub_and_a_source(live_server_url, hf_api_token, source):
    from huggingface_hub import HfApi

    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    got = await asyncio.to_thread(lambda: [m.id for m in api.list_models(limit=1500)])

    assert len(got) == 1500 and len(set(got)) == 1500


async def test_a_source_failing_once_is_retried_and_the_list_is_whole(client, source):
    source.state.failures.extend([503])

    pages = await _follow(client, "/api/models", {"author": "ext", "limit": 1000})

    assert sum(len(p.json()) for p in pages) == EXTERNAL


async def test_a_source_that_stays_down_is_a_503_not_a_short_list(client, source):
    source.state.failures.extend([503] * 20)

    response = await client.get("/api/models", params={"author": "ext", "limit": 10})

    assert response.status_code == 503 and response.headers["retry-after"]


async def test_partial_ok_skips_the_failed_source_and_says_so(client, source):
    source.state.failures.extend([503] * 20)

    response = await client.get("/api/models", params={"author": "ext", "limit": 10, "partial": "ok"})

    assert response.status_code == 200 and response.json() == []
    assert response.headers["x-cheesecave-incomplete"] == "MockHF"


async def test_fallback_false_never_asks_a_source(client, source):
    response = await client.get("/api/models", params={"author": "ext", "fallback": "false"})

    assert response.json() == [] and source.state.calls == []


async def test_a_source_is_never_sent_a_credential(live_server_url, hf_api_token, source):
    from huggingface_hub import HfApi

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    await asyncio.to_thread(lambda: list(api.list_models(author="ext", limit=5)))

    assert source.state.calls and all(c["query"] for c in source.state.calls)
