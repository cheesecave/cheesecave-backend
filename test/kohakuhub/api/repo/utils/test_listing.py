"""Repository listing: parameters, the signed cursor, local ordering and filters, page assembly.

The HF-compatible list endpoints answer a bare JSON array and tell the client where the next
page is in a ``Link: <url>; rel="next"`` header (RFC 8288, the format GitHub and the Hub use;
``huggingface_hub.utils.paginate`` follows it). A page is a slice of one ordered stream: the
local rows first, then every external fallback source in priority order. The cursor says where
the stream stopped; it is signed, bound to the query that made it and never holds a URL.
"""

from __future__ import annotations

import base64
import json
import random

import pytest

from kohakuhub.api.repo.utils import listing
from test.kohakuhub.support.factories import make_repo, make_user


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        (None, "trending"),
        ("", "trending"),
        ("trending", "trending"),
        ("trendingScore", "trending"),
        ("trending_score", "trending"),
        ("recent", "recent"),
        ("createdAt", "recent"),
        ("created_at", "recent"),
        ("updated", "updated"),
        ("lastModified", "updated"),
        ("last_modified", "updated"),
        ("likes", "likes"),
        ("downloads", "downloads"),
    ],
)
def test_sort_names_of_the_hub_and_of_this_hub_are_accepted(given, canonical):
    assert listing.parse_sort(given) == canonical


def test_an_unknown_sort_is_refused_with_the_names_that_work():
    with pytest.raises(listing.ListParamError) as refused:
        listing.parse_sort("alphabetical")

    assert "lastModified" in str(refused.value) and "downloads" in str(refused.value)


@pytest.mark.parametrize(("given", "ascending"), [(None, False), (-1, False), (1, True)])
def test_direction_minus_one_is_descending_and_one_ascending(given, ascending):
    assert listing.parse_direction(given) is ascending


def test_any_other_direction_is_refused():
    with pytest.raises(listing.ListParamError):
        listing.parse_direction(0)


@pytest.mark.parametrize(
    ("limit", "size"), [(None, 1000), (1, 1), (999, 999), (1000, 1000), (5000, 1000), (100000, 1000)]
)
def test_a_page_never_holds_more_than_a_thousand_like_the_hub(limit, size):
    assert listing.page_size_for(limit) == size


# ---------------------------------------------------------------------------
# The query and what it is bound to
# ---------------------------------------------------------------------------


def test_the_fingerprint_follows_what_selects_rows_not_how_many_a_page_holds():
    base = listing.ListQuery(repo_type="model", author="owner", sort="likes")

    assert base.fingerprint() == listing.ListQuery(
        repo_type="model", author="owner", sort="likes", page_size=7, partial=True
    ).fingerprint()
    for changed in (
        {"repo_type": "dataset"},
        {"author": "other"},
        {"sort": "downloads"},
        {"ascending": True},
        {"search": "x"},
        {"tags": ("a",)},
        {"pipeline_tag": ("text-generation",)},
        {"library": ("transformers",)},
        {"language": ("en",)},
        {"task_categories": ("x",)},
        {"size_categories": ("n<1k",)},
    ):
        other = listing.ListQuery(**{"repo_type": "model", "author": "owner", "sort": "likes", **changed})
        assert other.fingerprint() != base.fingerprint(), changed


def test_filters_are_sent_to_a_hugging_face_source_under_its_own_names():
    query = listing.ListQuery(
        repo_type="model",
        author="a",
        sort="updated",
        search="bert",
        tags=("license:mit", "en"),
        pipeline_tag=("fill-mask",),
        library=("transformers",),
        language=("en",),
        task_categories=("tc",),
        size_categories=("n<1k",),
    )

    params = query.external_params("huggingface")

    assert params == [
        ("author", "a"),
        ("sort", "lastModified"),
        ("direction", "-1"),
        ("search", "bert"),
        ("filter", "license:mit"),
        ("filter", "en"),
        ("pipeline_tag", "fill-mask"),
        ("library", "transformers"),
        ("language", "en"),
        ("task_categories", "tc"),
        ("size_categories", "n<1k"),
    ]


@pytest.mark.parametrize(
    ("sort", "hub_name"),
    [("trending", "trendingScore"), ("recent", "createdAt"), ("updated", "lastModified"), ("likes", "likes"), ("downloads", "downloads")],
)
def test_the_sort_goes_to_a_hugging_face_source_as_the_hub_names_it(sort, hub_name):
    params = dict(listing.ListQuery(repo_type="model", sort=sort).external_params("huggingface"))

    assert params["sort"] == hub_name


def test_a_cheesecave_source_gets_this_hubs_own_names_and_an_ascending_direction():
    query = listing.ListQuery(repo_type="dataset", sort="updated", ascending=True, search="x")

    assert query.external_params("kohakuhub") == [("sort", "updated"), ("direction", "1"), ("search", "x")]


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


def _state():
    return {"s": "local", "k": [5, 17]}


def test_a_cursor_round_trips_and_is_opaque():
    token = listing.encode_cursor(_state(), "fp")

    assert listing.decode_cursor(token, "fp") == _state()
    assert "local" not in token and "17" not in token  # base64, not readable


def test_a_cursor_of_another_query_is_refused():
    token = listing.encode_cursor(_state(), "fp")

    with pytest.raises(listing.CursorError):
        listing.decode_cursor(token, "another-query")


def test_a_cursor_that_was_changed_is_refused():
    token = listing.encode_cursor(_state(), "fp")
    payload, _, signature = token.partition(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged["s"] = "http://169.254.169.254/"
    tampered = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=") + "." + signature

    with pytest.raises(listing.CursorError):
        listing.decode_cursor(tampered, "fp")


@pytest.mark.parametrize("token", ["", "nonsense", "a.b", "....", "e30.e30", "%%%.%%%"])
def test_garbage_is_not_a_cursor(token):
    with pytest.raises(listing.CursorError):
        listing.decode_cursor(token, "fp")


def test_a_signed_payload_that_is_not_json_is_refused(monkeypatch):
    signed = listing._sign(b"not json")

    with pytest.raises(listing.CursorError):
        listing.decode_cursor(base64.urlsafe_b64encode(b"not json").decode().rstrip("=") + "." + signed, "fp")


def test_cursors_are_signed_with_a_key_of_this_server(monkeypatch):
    token = listing.encode_cursor(_state(), "fp")
    monkeypatch.setattr(listing.cfg.auth, "session_secret", "another secret")

    with pytest.raises(listing.CursorError):
        listing.decode_cursor(token, "fp")


# ---------------------------------------------------------------------------
# Where in an ordered list a cursor points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ascending", [False, True])
def test_the_position_after_a_key_is_the_same_as_scanning_for_it(ascending):
    rng = random.Random(7)
    keys = sorted({(rng.randint(0, 9), rng.randint(0, 99)) for _ in range(120)}, reverse=not ascending)

    for probe in [(rng.randint(-1, 10), rng.randint(-1, 100)) for _ in range(200)] + list(keys[:5]):
        expected = next(
            (i for i, key in enumerate(keys) if (key > probe if ascending else key < probe)),
            len(keys),
        )
        assert listing.position_after(keys, tuple(probe), ascending) == expected, probe


def test_the_position_after_nothing_is_the_start():
    assert listing.position_after([(3, 1), (2, 1)], None, False) == 0


# ---------------------------------------------------------------------------
# The local stream: ordering, filters, visibility on a real database
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.usefixtures("db_dual")  # SQLite and PostgreSQL: timestamps come back differently


def _repos(owner, count=6, repo_type="model"):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        make_repo(
            owner,
            f"r{i}",
            repo_type=repo_type,
            likes_count=(i * 7) % 5,
            downloads=(i * 3) % 4,
            created_at=start + timedelta(days=i),
        )
        for i in range(count)
    ]


def _ids(keys):
    return [key[-1] for key in keys]


@pytestmark_db
@pytest.mark.parametrize("sort", ["recent", "likes", "downloads", "updated", "trending"])
@pytest.mark.parametrize("ascending", [False, True])
def test_every_visible_repository_is_in_the_stream_once_in_a_strict_order(sort, ascending):
    owner = make_user("owner")
    rows = _repos(owner)

    keys = listing.local_order(listing.ListQuery(repo_type="model", sort=sort, ascending=ascending), owner)

    assert sorted(_ids(keys)) == sorted(row.id for row in rows)
    assert keys == sorted(keys, reverse=not ascending)


@pytestmark_db
def test_recent_means_newest_first_and_ties_are_broken_by_the_id():
    owner = make_user("owner")
    rows = _repos(owner, 4)

    ids = _ids(listing.local_order(listing.ListQuery(repo_type="model", sort="recent"), owner))

    assert ids == [row.id for row in reversed(rows)]


@pytestmark_db
def test_likes_and_downloads_order_by_their_counts():
    owner = make_user("owner")
    rows = _repos(owner)
    by_id = {row.id: row for row in rows}

    likes = _ids(listing.local_order(listing.ListQuery(repo_type="model", sort="likes"), owner))
    downloads = _ids(listing.local_order(listing.ListQuery(repo_type="model", sort="downloads"), owner))

    assert [by_id[i].likes_count for i in likes] == sorted((r.likes_count for r in rows), reverse=True)
    assert [by_id[i].downloads for i in downloads] == sorted((r.downloads for r in rows), reverse=True)


@pytestmark_db
def test_updated_follows_the_last_commit_on_main_and_falls_back_to_the_creation_time():
    from datetime import datetime, timezone

    from test.kohakuhub.support.factories import make_commit

    owner = make_user("owner")
    first, second, third = _repos(owner, 3)  # created in this order, so `third` is the newest
    make_commit(first, "c1", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))  # oldest repo, newest commit
    make_commit(second, "c2", branch="side", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc))  # not on main

    ids = _ids(listing.local_order(listing.ListQuery(repo_type="model", sort="updated"), owner))

    assert ids == [first.id, third.id, second.id]


@pytestmark_db
def test_trending_ranks_every_repository_not_only_the_ones_with_activity():
    from datetime import datetime, timezone

    from test.kohakuhub.support.factories import make_daily_stats

    owner = make_user("owner")
    quiet, busy = _repos(owner, 2)
    make_daily_stats(busy, datetime.now(timezone.utc).date(), download_sessions=9)

    ids = _ids(listing.local_order(listing.ListQuery(repo_type="model", sort="trending"), owner))

    assert ids == [busy.id, quiet.id]  # the repository nobody downloaded is still listed


@pytestmark_db
def test_private_repositories_are_listed_only_for_those_who_may_read_them():
    owner, stranger = make_user("owner"), make_user("stranger")
    public = make_repo(owner, "public")
    private = make_repo(owner, "private", private=True)
    query = listing.ListQuery(repo_type="model", sort="recent")

    assert set(_ids(listing.local_order(query, owner))) == {public.id, private.id}
    assert set(_ids(listing.local_order(query, stranger))) == {public.id}
    assert set(_ids(listing.local_order(query, None))) == {public.id}


@pytestmark_db
def test_author_and_type_select_the_rows():
    a, b = make_user("a"), make_user("b")
    wanted = make_repo(a, "x")
    make_repo(b, "x")
    make_repo(a, "x", repo_type="dataset")

    keys = listing.local_order(listing.ListQuery(repo_type="model", author="a"), None)

    assert _ids(keys) == [wanted.id]


@pytestmark_db
def test_search_is_a_case_insensitive_part_of_the_id_and_wildcards_are_plain_characters():
    owner = make_user("owner")
    bert = make_repo(owner, "Bert-Base")
    make_repo(owner, "gpt2")
    percent = make_repo(owner, "100%_sure")
    query = lambda text: listing.ListQuery(repo_type="model", search=text)

    assert _ids(listing.local_order(query("bert"), owner)) == [bert.id]
    assert _ids(listing.local_order(query("OWNER/gpt"), owner)) != []
    assert _ids(listing.local_order(query("%_"), owner)) == [percent.id]
    assert _ids(listing.local_order(query("zzz"), owner)) == []
