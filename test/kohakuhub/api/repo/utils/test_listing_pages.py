"""Pages cut from the stream: local rows first, then each external source, nothing lost, nothing twice.

Real database rows, and Hugging Face shaped sources over real HTTP (``support/list_hf_server.py``).
A page is built from a cursor state and gives the state to continue from; following the states
until there is none must read every item of every source exactly once.
"""

from __future__ import annotations

import asyncio

import pytest

import kohakuhub.api.fallback.operations as fallback_ops
from kohakuhub.api.repo.utils import listing
from kohakuhub.db import Repository
from test.kohakuhub.support.factories import make_repo, make_user
from test.kohakuhub.support.list_hf_server import build_list_hf_app, make_item
from test.kohakuhub.support.live_server import start_live_server, stop_live_server

pytestmark = pytest.mark.usefixtures("db_dual")


@pytest.fixture(scope="module")
def upstreams():
    """Two sources: A holds a0..a6, B holds b0..b3; each answers at most 2 items a page."""
    servers = []
    for name, count in (("a", 7), ("b", 4)):
        app = build_list_hf_app(
            {"model": [make_item("model", f"{name}/{name}{i}", trendingScore=100 - i) for i in range(count)]},
            page_cap=2,
        )
        servers.append((start_live_server(app), app))
    try:
        yield [{"name": n.upper(), "url": h.base_url, "source_type": "huggingface", "priority": i} for i, (n, (h, _)) in enumerate(zip("ab", servers))], [a for _, a in servers]
    finally:
        for handle, _ in servers:
            stop_live_server(handle)


@pytest.fixture
def sources(upstreams):
    configured, apps = upstreams
    for app in apps:
        app.state.calls.clear()
        app.state.failures.clear()
        app.state.ignore_limit = False
    return configured


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(fallback_ops, "LIST_RETRY_WAIT", 0.0)


async def render(ids):
    rows = {row.id: row for row in Repository.select().where(Repository.id.in_(ids))}
    return [{"id": rows[i].full_id, "_source": "local"} for i in ids]


def _page(query, state, sources, user=None, **kwargs):
    return asyncio.run(listing.build_page(query, user, state=state, sources=sources, render_local=render, **kwargs))


def _everything(query, sources, user=None, max_pages=50, **kwargs):
    """Follow the states to the end: the pages, as lists of ids."""
    pages, state = [], None
    for _ in range(max_pages):
        page = _page(query, state, sources, user, **kwargs)
        pages.append([item["id"] for item in page.items])
        state = page.next_state
        if state is None:
            return pages
    raise AssertionError("the listing does not end")


def _local(count, owner="loc"):
    user = make_user(owner)
    return [make_repo(user, f"m{i}") for i in range(count)]


def _query(**kwargs):
    return listing.ListQuery(repo_type="model", sort="recent", **kwargs)


def test_local_rows_alone_are_cut_into_pages_and_read_once(sources):
    _local(7)

    pages = _everything(_query(page_size=3), [])

    assert [len(p) for p in pages] == [3, 3, 1]
    assert sorted(sum(pages, [])) == sorted(f"loc/m{i}" for i in range(7))


def test_a_listing_that_fits_one_page_has_no_next_state(sources):
    _local(2)

    page = _page(_query(), None, [])

    assert len(page.items) == 2 and page.next_state is None and page.incomplete == []


def test_the_local_rows_come_first_then_each_source_in_turn(sources):
    _local(2)

    pages = _everything(_query(page_size=4), sources)

    flat = sum(pages, [])
    assert flat[:2] == ["loc/m1", "loc/m0"]  # newest first
    assert flat[2:] == [f"a/a{i}" for i in range(7)] + [f"b/b{i}" for i in range(4)]
    assert [len(p) for p in pages] == [4, 4, 4, 1]


def test_a_page_is_filled_across_several_requests_to_a_source_that_answers_in_small_pages(sources, upstreams):
    _, apps = upstreams
    _local(1)

    page = _page(_query(page_size=6), None, sources)

    assert [i["id"] for i in page.items] == ["loc/m0"] + [f"a/a{i}" for i in range(5)]
    assert len(apps[0].state.calls) == 3  # 2 + 2 + 1 items asked for, the source's pages being 2
    assert [dict(c["query"])["limit"] for c in apps[0].state.calls] == ["5", "3", "1"]


def test_a_page_that_ends_exactly_where_the_local_rows_end_continues_at_the_first_source(sources):
    _local(3)

    first = _page(_query(page_size=3), None, sources)
    second = _page(_query(page_size=3), first.next_state, sources)

    assert len(first.items) == 3 and first.next_state is not None
    assert [i["id"] for i in second.items] == ["a/a0", "a/a1", "a/a2"]


def test_a_page_that_ends_exactly_where_a_source_ends_continues_at_the_next_source(sources):
    pages = _everything(_query(page_size=7), sources)

    assert pages == [[f"a/a{i}" for i in range(7)], [f"b/b{i}" for i in range(4)]]


def test_a_page_that_ends_where_the_last_source_ends_is_the_last(sources):
    pages = _everything(_query(page_size=4), [sources[1]])

    assert pages == [[f"b/b{i}" for i in range(4)]]


def test_a_repository_the_hub_has_itself_is_listed_once_from_here(sources, upstreams):
    _, apps = upstreams
    owner = make_user("a")
    make_repo(owner, "a0")  # a/a0 is also in source A
    apps[1].state.items["model"].append(make_item("model", "a/a0"))  # and in source B
    try:
        flat = sum(_everything(_query(page_size=100), sources), [])
    finally:
        apps[1].state.items["model"].pop()

    assert flat.count("a/a0") == 1 and flat[0] == "a/a0"  # the local row wins
    assert len(flat) == 1 + 6 + 4  # 6 more of A, all 4 of B, not the copy of a/a0


def test_a_repository_two_sources_both_have_is_listed_once_in_one_response(sources, upstreams):
    _, apps = upstreams
    apps[1].state.items["model"].append(make_item("model", "a/a1"))
    try:
        flat = sum(_everything(_query(page_size=100), sources), [])
    finally:
        apps[1].state.items["model"].pop()

    assert flat.count("a/a1") == 1


def test_a_source_that_ignores_the_limit_loses_nothing(sources, upstreams):
    _, apps = upstreams
    apps[0].state.ignore_limit = True

    page = _page(_query(page_size=1), None, [sources[0]])

    assert [i["id"] for i in page.items] == ["a/a0", "a/a1"]  # its own page of 2, not cut to 1
    assert page.next_state is not None


def test_a_failing_source_fails_the_page_unless_partial_results_are_allowed(sources, upstreams):
    _, apps = upstreams
    apps[0].state.failures.extend([503] * 6)
    _local(1)

    with pytest.raises(fallback_ops.ExternalListError) as failed:
        _page(_query(page_size=10), None, sources)

    assert failed.value.source == "A"


def test_partial_results_skip_the_failing_source_and_say_which(sources, upstreams):
    _, apps = upstreams
    apps[0].state.failures.extend([503] * 6)
    _local(1)

    page = _page(_query(page_size=100, partial=True), None, sources)

    assert [i["id"] for i in page.items] == ["loc/m0"] + [f"b/b{i}" for i in range(4)]
    assert page.incomplete == ["A"] and page.next_state is None


def test_a_page_stops_asking_a_source_that_only_gives_what_is_already_listed(sources, upstreams, monkeypatch):
    _, apps = upstreams
    owner = make_user("a")
    for i in range(7):
        make_repo(owner, f"a{i}")  # everything source A has, the hub has
    monkeypatch.setattr(listing, "MAX_SOURCE_REQUESTS", 2)

    page = _page(_query(page_size=100), None, [sources[0]])

    assert len(apps[0].state.calls) == 2  # bounded
    assert page.next_state is not None  # and the listing goes on from where it stopped
    assert sum(_everything(_query(page_size=100), [sources[0]]), []).count("a/a0") == 1


def test_a_cursor_for_a_source_that_is_no_longer_configured_is_refused(sources):
    _local(1)
    state = {"s": listing.source_id(sources[0]), "q": None}

    with pytest.raises(listing.CursorError):
        _page(_query(), state, [sources[1]])


def test_a_listing_resumes_from_a_state_read_back_from_json(sources):
    import json

    _local(5)
    first = _page(_query(page_size=2), None, sources)

    resumed = _page(_query(page_size=2), json.loads(json.dumps(first.next_state)), sources)

    assert [i["id"] for i in resumed.items] == [i["id"] for i in _page(_query(page_size=2), first.next_state, sources).items]
