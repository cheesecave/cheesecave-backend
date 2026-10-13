"""One page of an external source's repository list, over real HTTP.

A Hugging Face shaped server (``support/list_hf_server.py``) plays the source: pages of at most
``page_cap`` items, ``Link: <...cursor...>; rel="next"``, and failures on demand. The fetch
returns the page and the query string that source gave for its next page; it fails loudly when
the source could not answer, because a list that silently lacks a source is incomplete without
saying so.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import kohakuhub.api.fallback.operations as fallback_ops
from test.kohakuhub.support.list_hf_server import build_list_hf_app, make_item
from test.kohakuhub.support.live_server import start_live_server, stop_live_server


@pytest.fixture(scope="module")
def upstream():
    app = build_list_hf_app(
        {"model": [make_item("model", f"acme/m{i:02d}", likes=i, trendingScore=100 - i) for i in range(12)]},
        page_cap=5,
    )
    handle = start_live_server(app)
    try:
        yield handle.base_url, app
    finally:
        stop_live_server(handle)


@pytest.fixture
def source(upstream):
    url, app = upstream
    app.state.calls.clear()
    app.state.failures.clear()
    app.state.delay = 0.0
    yield {"name": "MockHF", "url": url, "source_type": "huggingface", "priority": 1}
    if app.state.delay:  # requests the client gave up on are still sleeping in the server
        time.sleep(app.state.delay + 0.2)


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(fallback_ops, "LIST_RETRY_WAIT", 0.0)


def _fetch(source, params=(), limit=5, next_query=None, repo_type="model"):
    return asyncio.run(
        fallback_ops.fetch_external_page(source, repo_type, list(params), limit=limit, next_query=next_query)
    )


def test_a_page_comes_back_tagged_with_its_source_and_the_query_of_the_next(source, upstream):
    _, app = upstream

    page = _fetch(source, [("author", "acme"), ("sort", "trendingScore")], limit=5)

    assert [item["id"] for item in page.items] == [f"acme/m{i:02d}" for i in range(5)]
    assert {item["_source"] for item in page.items} == {"MockHF"}
    assert {item["_source_url"] for item in page.items} == {source["url"]}
    assert "cursor=" in page.next_query and "author=acme" in page.next_query
    assert app.state.calls[0]["query"] == [("author", "acme"), ("sort", "trendingScore"), ("limit", "5")]


def test_the_next_page_is_asked_with_the_query_the_source_gave_and_the_new_limit(source, upstream):
    _, app = upstream
    first = _fetch(source, [("author", "acme")], limit=5)

    second = _fetch(source, limit=2, next_query=first.next_query)

    assert [item["id"] for item in second.items] == ["acme/m05", "acme/m06"]
    sent = dict(app.state.calls[-1]["query"])
    assert sent["limit"] == "2" and sent["author"] == "acme" and "cursor" in sent
    assert [n for n, _ in app.state.calls[-1]["query"]].count("limit") == 1


def test_following_the_next_queries_reads_everything_once_and_the_last_page_has_no_next(source):
    ids, query, params = [], None, [("author", "acme")]
    while True:
        page = _fetch(source, params, limit=100, next_query=query)
        assert len(page.items) <= 5  # the source's own cap, not the limit asked for
        ids += [item["id"] for item in page.items]
        query, params = page.next_query, ()
        if query is None:
            break

    assert ids == [f"acme/m{i:02d}" for i in range(12)]


def test_repeated_parameters_reach_the_source_in_order(source, upstream):
    _, app = upstream

    _fetch(source, [("filter", "a"), ("filter", "b")], limit=1)

    assert app.state.calls[0]["query"][:2] == [("filter", "a"), ("filter", "b")]


def test_a_cheesecave_source_is_asked_for_the_same_path(upstream):
    url, app = upstream
    app.state.calls.clear()
    kohaku = {"name": "Other", "url": url, "source_type": "kohakuhub", "priority": 2}

    _fetch(kohaku, [("sort", "likes")], limit=1)

    assert app.state.calls[0]["plural"] == "models"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_a_source_that_fails_once_is_asked_again(source, upstream, status):
    _, app = upstream
    app.state.failures.extend([status])

    page = _fetch(source, [("author", "acme")], limit=2)

    assert len(page.items) == 2 and len(app.state.calls) == 2


def test_a_source_that_keeps_failing_is_reported_not_skipped(source, upstream):
    _, app = upstream
    app.state.failures.extend([503, 503, 503, 503])

    with pytest.raises(fallback_ops.ExternalListError) as failed:
        _fetch(source, [("author", "acme")], limit=2)

    assert failed.value.source == "MockHF" and "503" in failed.value.reason
    assert len(app.state.calls) == 3  # the first try and two more


def test_a_source_that_does_not_answer_in_time_is_reported(source, upstream, monkeypatch):
    _, app = upstream
    monkeypatch.setattr(fallback_ops.cfg.fallback, "timeout_seconds", 0.2)
    app.state.delay = 0.6

    with pytest.raises(fallback_ops.ExternalListError) as failed:
        _fetch(source, limit=2)

    assert "Timeout" in failed.value.reason or "timed out" in failed.value.reason.lower()


def test_a_source_nobody_listens_on_is_reported(source):
    dead = dict(source, url="http://127.0.0.1:9")

    with pytest.raises(fallback_ops.ExternalListError) as failed:
        _fetch(dead, limit=2)

    assert failed.value.source == "MockHF"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_source_that_refuses_the_query_has_nothing_to_list_and_is_not_asked_again(source, upstream, status):
    _, app = upstream
    app.state.failures.extend([status])

    page = _fetch(source, limit=2)

    assert page.items == [] and page.next_query is None and len(app.state.calls) == 1


@pytest.mark.parametrize("failure", ["object", "garbage"])
def test_an_answer_that_is_not_a_list_is_a_failure(source, upstream, failure):
    _, app = upstream
    app.state.failures.extend([failure, failure, failure])

    with pytest.raises(fallback_ops.ExternalListError):
        _fetch(source, limit=2)


def test_entries_that_are_not_repositories_are_dropped(source, upstream):
    _, app = upstream
    app.state.failures.append("mixed")

    page = _fetch(source, limit=100)

    assert [item["id"] for item in page.items] == ["acme/ok"]
