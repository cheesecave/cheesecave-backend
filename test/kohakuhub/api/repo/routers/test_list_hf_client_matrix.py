"""The repository lists as ``huggingface_hub`` uses them, for every repository type.

One scenario table, run against whichever client version is installed (the CI matrix installs
0.20.3 .. latest). Parameters a client version does not have are dropped from the call and a
scenario that needs one is skipped, so an old client never fails for a feature it lacks. 0.20 is the oldest version this project promises.

Real database, real app over HTTP; the fallback source is a Hugging Face shaped server
(``support/list_hf_server.py``) over real HTTP.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import warnings

import pytest

from test.kohakuhub.support.list_hf_server import build_list_hf_app, make_item
from test.kohakuhub.support.live_server import start_live_server, stop_live_server

pytestmark = [pytest.mark.backend_per_test, pytest.mark.hf_client]

TYPES = [
    pytest.param("model", "list_models", id="models"),
    pytest.param("dataset", "list_datasets", id="datasets"),
    pytest.param("space", "list_spaces", id="spaces"),
]
EXTERNAL = 2300  # more than two Hub pages


def _hf():
    import huggingface_hub

    return huggingface_hub


def _call(api, method, skip_if_missing=(), **kwargs):
    """Call ``api.<method>`` with the parameters this client version has; skip when a needed one is missing."""
    function = getattr(api, method)
    accepted = set(inspect.signature(function).parameters)
    for name in skip_if_missing:
        if name not in accepted:
            pytest.skip(f"{method} of huggingface_hub {_hf().__version__} has no '{name}'")
    with warnings.catch_warnings():  # the suite turns warnings into errors; a client's own deprecation notices are not ours
        warnings.simplefilter("ignore", FutureWarning)
        return list(function(**{k: v for k, v in kwargs.items() if k in accepted or k in skip_if_missing}))


def _ids(items):
    return [item.id for item in items]


async def _list(api, method, **kwargs):
    return await asyncio.to_thread(lambda: _call(api, method, **kwargs))


def _live_db():
    return sys.modules["kohakuhub.db"]


async def _create(owner_client, repo_type, name, private=False):
    (await owner_client.post("/api/repos/create", json={"type": repo_type, "name": name, "private": private})).raise_for_status()


def _tag(repo_type, name, *facets):
    """Index ``facets`` for the repository the way discovery does, so list filters can match it."""
    db = _live_db()
    repo = db.Repository.get(
        (db.Repository.repo_type == repo_type) & (db.Repository.namespace == "owner") & (db.Repository.name == name)
    )
    for key, value in facets:
        db.RepositoryFacet.create(repository=repo, key=key, value=value)
    db.RepositoryMetadata.get_or_create(repository=repo, defaults={"state": "ready"})
    db.RepositoryMetadata.update(state="ready").where(db.RepositoryMetadata.repository == repo).execute()
    return repo


@pytest.fixture
def api(live_server_url, hf_api_token):
    return _hf().HfApi(endpoint=live_server_url, token=hf_api_token)


@pytest.fixture
def anonymous(live_server_url):
    return _hf().HfApi(endpoint=live_server_url, token=False)


@pytest.fixture
def source(prepared_backend_test_state, monkeypatch):
    cfg = prepared_backend_test_state.modules.config_module.cfg
    items = {
        kind: [make_item(kind, f"ext/{kind}{i:04d}", likes=i, trendingScore=i) for i in range(EXTERNAL)]
        for kind in ("model", "dataset", "space")
    }
    app = build_list_hf_app(items)
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


# --- what a client reads from this hub alone --------------------------------


@pytest.mark.parametrize("kind,method", TYPES)
async def test_the_list_parses_into_the_clients_info_objects(api, owner_client, kind, method):
    await _create(owner_client, kind, "plain")

    items = await _list(api, method, author="owner")

    mine = [i for i in items if i.id == "owner/plain"]
    assert mine and mine[0].private is False
    assert all(i.id.startswith("owner/") for i in items)


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_list_item_has_the_ids_and_the_trending_score_the_hub_gives(api, owner_client, kind, method):
    """On the wire (``_id`` a string, ``modelId`` for models, ``trendingScore``) and as the client reads it."""
    await _create(owner_client, kind, "ided")

    wire = next(i for i in (await owner_client.get(f"/api/{kind}s", params={"author": "owner", "fallback": "false"})).json() if i["id"] == "owner/ided")
    item = next(i for i in await _list(api, method, author="owner") if i.id == "owner/ided")

    assert wire["_id"].isdigit() and ("modelId" in wire) == (kind == "model") and wire["trendingScore"] == 0
    assert item.id == "owner/ided"
    if hasattr(item, "trending_score"):  # the attribute the clients that know the field read it into
        assert item.trending_score == 0


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_limit_is_honoured(api, owner_client, kind, method):
    for i in range(4):
        await _create(owner_client, kind, f"lim{i}")

    assert len(await _list(api, method, author="owner", limit=2)) == 2
    assert len(await _list(api, method, author="owner", limit=1)) == 1
    assert len(await _list(api, method, author="owner")) >= 4


@pytest.mark.parametrize(
    "kind,method,sort",
    [("model", "list_models", s) for s in ("likes", "downloads", "last_modified", "created_at", "trending_score", "trending", "lastModified", "createdAt")]
    + [(k, m, s) for k, m in (("dataset", "list_datasets"), ("space", "list_spaces")) for s in ("likes", "last_modified")],
)
async def test_every_sort_a_client_can_send_is_answered(api, owner_client, kind, method, sort):
    await _create(owner_client, kind, "srt")

    try:
        items = await _list(api, method, author="owner", sort=sort)
    except ValueError as error:  # the client itself refuses names it does not know
        pytest.skip(str(error)[:120])

    assert "owner/srt" in _ids(items)


@pytest.mark.parametrize("kind,method", TYPES)
async def test_direction_reverses_the_order(api, owner_client, kind, method):
    for name in ("d1", "d2", "d3"):
        await _create(owner_client, kind, name)

    try:
        down = _ids(await _list(api, method, author="owner", sort="created_at", direction=-1, skip_if_missing=("direction",)))
        up = _ids(await _list(api, method, author="owner", sort="created_at", direction=1, skip_if_missing=("direction",)))
    except ValueError as error:
        pytest.skip(str(error)[:120])

    assert up == list(reversed(down)) and len(down) >= 3


@pytest.mark.parametrize("kind,method", TYPES)
async def test_search_and_author_select(api, owner_client, kind, method):
    await _create(owner_client, kind, "Needle-x")
    await _create(owner_client, kind, "other")

    found = _ids(await _list(api, method, search="needle", skip_if_missing=("search",)))
    assert found == ["owner/Needle-x"]
    assert "owner/other" not in _ids(await _list(api, method, author="owner", search="needle"))


@pytest.mark.parametrize("kind,method", TYPES)
async def test_extra_parameters_the_hub_clients_send_are_harmless(api, owner_client, kind, method):
    await _create(owner_client, kind, "extras")

    items = await _list(api, method, author="owner", full=True, cardData=True, fetch_config=True, gated=False, inference=None)

    assert "owner/extras" in _ids(items)


async def test_expand_is_harmless(api, owner_client):
    await _create(owner_client, "model", "expanded")

    items = await _list(api, "list_models", author="owner", expand=["downloads", "likes", "createdAt"], skip_if_missing=("expand",))

    assert "owner/expanded" in _ids(items)


# --- filters ---------------------------------------------------------------


async def test_model_filters_select_by_the_indexed_facets(api, owner_client):
    await _create(owner_client, "model", "bert")
    await _create(owner_client, "model", "gpt")
    _tag("model", "bert", ("task", "fill-mask"), ("library", "transformers"), ("language", "en"), ("license", "mit"))
    _tag("model", "gpt", ("task", "text-generation"), ("library", "transformers"), ("license", "apache-2.0"))

    assert _ids(await _list(api, "list_models", pipeline_tag="fill-mask", skip_if_missing=("pipeline_tag",))) == ["owner/bert"]
    assert sorted(_ids(await _list(api, "list_models", library="transformers", skip_if_missing=("library",)))) == ["owner/bert", "owner/gpt"]
    assert _ids(await _list(api, "list_models", language="en", skip_if_missing=("language",))) == ["owner/bert"]
    assert _ids(await _list(api, "list_models", filter="license:mit", skip_if_missing=("filter",))) == ["owner/bert"]
    assert _ids(await _list(api, "list_models", filter=["license:mit", "license:apache-2.0"], skip_if_missing=("filter",))) == []


async def test_dataset_filters_select_by_the_indexed_facets(api, owner_client):
    await _create(owner_client, "dataset", "small")
    await _create(owner_client, "dataset", "big")
    _tag("dataset", "small", ("task", "text-classification"), ("size", "1k"), ("language", "en"))
    _tag("dataset", "big", ("task", "translation"), ("size", "1m"), ("language", "fr"))

    assert _ids(await _list(api, "list_datasets", task_categories="text-classification", skip_if_missing=("task_categories",))) == ["owner/small"]
    assert _ids(await _list(api, "list_datasets", size_categories="1m", skip_if_missing=("size_categories",))) == ["owner/big"]
    assert _ids(await _list(api, "list_datasets", language="fr", skip_if_missing=("language",))) == ["owner/big"]


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_tag_this_hub_does_not_index_matches_nothing_and_is_not_an_error(api, owner_client, kind, method):
    await _create(owner_client, kind, "anything")

    assert await _list(api, method, filter="base_model:finetune:x", skip_if_missing=("filter",)) == []


# --- visibility ------------------------------------------------------------


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_private_repository_is_listed_for_its_owner_only(api, anonymous, owner_client, kind, method):
    await _create(owner_client, kind, "hidden", private=True)

    assert "owner/hidden" in _ids(await _list(api, method, author="owner"))
    assert "owner/hidden" not in _ids(await _list(anonymous, method, author="owner"))


# --- a list that spans this hub and a source -------------------------------


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_list_over_this_hub_and_a_source_is_complete_and_has_no_duplicates(api, source, owner_client, kind, method):
    await _create(owner_client, kind, "local")

    ids = _ids(await _list(api, method))

    external = [i for i in ids if i.startswith("ext/")]
    assert len(external) == EXTERNAL and len(set(ids)) == len(ids)
    assert ids.index("owner/local") < ids.index(external[0])  # this hub's own first


@pytest.mark.parametrize(
    "kind,method,limit",
    [("model", "list_models", n) for n in (1, 999, 1000, 1001, 2300)]
    + [(k, m, 1001) for k, m in (("dataset", "list_datasets"), ("space", "list_spaces"))],
)
async def test_a_limit_across_the_hub_and_a_source_is_exact(api, source, owner_client, kind, method, limit):
    await _create(owner_client, kind, "local")

    ids = _ids(await _list(api, method, limit=limit))

    assert len(ids) == min(limit, EXTERNAL + 1) and len(set(ids)) == len(ids)


@pytest.mark.parametrize("sort", ["likes", "last_modified", "created_at", "trending_score"])
async def test_the_sort_reaches_the_source_under_the_hubs_own_name(api, source, sort):
    try:
        await _list(api, "list_models", sort=sort, limit=5)
    except ValueError as error:
        pytest.skip(str(error)[:120])

    sent = dict(source.state.calls[0]["query"])
    assert sent["sort"] in {"likes", "lastModified", "createdAt", "trendingScore"} and sent["direction"] == "-1"


async def test_search_author_and_filters_reach_the_source(api, source):
    await _list(api, "list_models", author="ext", search="m00", filter="license:mit", limit=3, skip_if_missing=("filter",))

    query = source.state.calls[0]["query"]
    assert ("author", "ext") in query and ("search", "m00") in query and ("filter", "license:mit") in query


async def test_no_credential_of_this_hub_reaches_the_source(api, source, hf_api_token):
    await _list(api, "list_models", limit=3)

    assert source.state.calls and all(call["authorization"] is None for call in source.state.calls)


@pytest.mark.parametrize("kind,method", TYPES)
async def test_a_source_failing_once_does_not_show_in_the_list(api, source, kind, method):
    source.state.failures.append(503)

    ids = _ids(await _list(api, method, limit=1200))

    assert len(ids) == 1200


async def test_a_source_that_stays_down_fails_the_call_instead_of_returning_a_short_list(api, source):
    source.state.failures.extend([503] * 40)

    with pytest.raises(Exception) as failed:  # HfHubHTTPError, whichever module the version raises it from
        await _list(api, "list_models", limit=50)

    assert "503" in str(failed.value)
