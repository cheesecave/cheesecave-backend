"""HF-compatible repository listing: one ordered stream, cut into pages with a ``Link`` header.

``GET /api/{models,datasets,spaces}`` answers a bare JSON array, so where the next page is can
only go in a header: ``Link: <url>; rel="next"`` (RFC 8288, the format GitHub and the Hub use,
which ``huggingface_hub.utils.paginate`` follows). A response holds at most 1000 items, like
the Hub's, and carries no ``Link`` on the last page.

What is listed is one ordered stream: the local rows first, then every external fallback
source in priority order, each in the requested order. A cursor says where the stream
stopped. It is signed with a key of this server, bound to the query that made it (a cursor
of another query is refused) and never holds a URL: the next page of an external source is
requested from the configured source, with the query string that source gave us, so a forged
cursor cannot make this server fetch an address of its choosing. It only names a position;
visibility is decided again on every page.

The local part is ordered in Python over ``(sort key, id)`` of the rows the query selects.
That keeps the cursor independent of the database engine and of how it hands back
timestamps, and a row inserted or deleted between two pages moves nothing that was already
read. It reads every candidate's key on each page, which is fine for the catalogues this
serves (hundreds to some hundreds of thousands of repositories per type); a SQL keyset on an
index is the upgrade when one grows past that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from peewee import fn

from kohakuhub.api.fallback.operations import ExternalListError, fetch_external_page
from kohakuhub.api.utils.trending import calculate_trending_scores
from kohakuhub.auth.permissions import filter_readable_repositories
from kohakuhub.config import cfg
from kohakuhub.db import Commit, Repository
from kohakuhub.repository_discovery import F, _ready_ids
from kohakuhub.utils.datetime_utils import ensure_datetime

MAX_PAGE_SIZE = 1000  # what the Hub answers however large a limit is asked for
MAX_SOURCE_REQUESTS = 8  # requests to one source for one response; the listing goes on from the cursor
DEFAULT_SORT = "trending"  # the Hub's default order

# The names the Hub's clients send, and this hub's own, to the canonical sort.
SORT_NAMES = {
    "trending": "trending",
    "trendingScore": "trending",
    "trending_score": "trending",
    "recent": "recent",
    "createdAt": "recent",
    "created_at": "recent",
    "updated": "updated",
    "lastModified": "updated",
    "last_modified": "updated",
    "likes": "likes",
    "downloads": "downloads",
}
HUB_SORT_NAMES = {
    "trending": "trendingScore",
    "recent": "createdAt",
    "updated": "lastModified",
    "likes": "likes",
    "downloads": "downloads",
}

# A tag with one of these prefixes ("license:mit") is an indexed facet; any other prefix
# ("dataset:", "base_model:", ...) names something this hub does not index.
TAG_PREFIX_FACETS = {
    "license": "license",
    "language": "language",
    "library": "library",
    "task_categories": "task",
    "size_categories": "size",
    "format": "format",
    "modality": "modality",
    "sdk": "sdk",
}


class ListParamError(ValueError):
    """A list parameter this server cannot honour."""


class CursorError(ValueError):
    """A cursor that is forged, damaged, or made for another query."""


def parse_sort(value: str | None) -> str:
    if not value:
        return DEFAULT_SORT
    try:
        return SORT_NAMES[value]
    except KeyError:
        raise ListParamError(
            f"Unknown sort '{value}'; use one of: " + ", ".join(sorted(set(SORT_NAMES) - {"created_at", "last_modified", "trending_score"}))
        ) from None


def parse_direction(value: int | None) -> bool:
    """``True`` for ascending. The Hub's clients send ``-1`` (descending) or nothing."""
    if value in (None, -1):
        return False
    if value == 1:
        return True
    raise ListParamError("direction must be -1 (descending) or 1 (ascending)")


def page_size_for(limit: int | None) -> int:
    return min(limit or MAX_PAGE_SIZE, MAX_PAGE_SIZE)


@dataclass(frozen=True)
class ListQuery:
    """What a listing selects and in which order; ``page_size`` and ``partial`` shape one response."""

    repo_type: str
    author: str | None = None
    sort: str = DEFAULT_SORT
    ascending: bool = False
    search: str | None = None
    tags: tuple[str, ...] = ()
    pipeline_tag: tuple[str, ...] = ()
    library: tuple[str, ...] = ()
    language: tuple[str, ...] = ()
    task_categories: tuple[str, ...] = ()
    size_categories: tuple[str, ...] = ()
    page_size: int = MAX_PAGE_SIZE
    partial: bool = False
    def fingerprint(self) -> str:
        """What the cursor is bound to: everything that selects or orders rows."""
        selected = [
            self.repo_type,
            self.author,
            self.sort,
            self.ascending,
            self.search,
            self.tags,
            self.pipeline_tag,
            self.library,
            self.language,
            self.task_categories,
            self.size_categories,
        ]
        return hashlib.sha256(json.dumps(selected).encode()).hexdigest()[:24]

    def external_params(self, source_type: str) -> list[tuple[str, str]]:
        """The query a source is asked, without ``limit`` and ``cursor``."""
        params: list[tuple[str, str]] = []
        if self.author:
            params.append(("author", self.author))
        hub_names = source_type == "huggingface"
        params.append(("sort", HUB_SORT_NAMES[self.sort] if hub_names else self.sort))
        params.append(("direction", "1" if self.ascending else "-1"))
        if self.search:
            params.append(("search", self.search))
        for name in ("tags", "pipeline_tag", "library", "language", "task_categories", "size_categories"):
            for value in getattr(self, name):
                params.append(("filter" if name == "tags" else name, value))
        return params


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


def _key() -> bytes:
    return hmac.new(cfg.auth.session_secret.encode(), b"repository-list-cursor-v1", hashlib.sha256).digest()


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(_key(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def encode_cursor(state: dict, fingerprint: str) -> str:
    payload = json.dumps({"v": 1, "q": fingerprint, "p": state}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + _sign(payload)


def decode_cursor(token: str, fingerprint: str) -> dict:
    try:
        encoded, signature = token.split(".")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        raise CursorError("Invalid cursor") from None
    if not hmac.compare_digest(signature, _sign(payload)):
        raise CursorError("Invalid cursor")
    try:
        body = json.loads(payload)
    except ValueError:
        raise CursorError("Invalid cursor") from None
    if body.get("q") != fingerprint:
        raise CursorError("The cursor belongs to another query")
    return body["p"]


def position_after(keys: list, after, ascending: bool) -> int:
    """The index of the first key strictly after ``after`` in a list ordered the way it is read."""
    if after is None:
        return 0
    low, high = 0, len(keys)
    while low < high:
        middle = (low + high) // 2
        passed = keys[middle] <= after if ascending else keys[middle] >= after
        if passed:
            low = middle + 1
        else:
            high = middle
    return low


# ---------------------------------------------------------------------------
# The local part of the stream
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _micros(value) -> int:
    moment = ensure_datetime(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(microseconds=1)


def _has_facet(scope, key: str | None, value: str):
    wanted = (F.value == value) if key is None else ((F.key == key) & (F.value == value))
    return scope.where(Repository.id.in_(F.select(F.repository).where(wanted))).where(
        Repository.id.in_(_ready_ids())
    )


def _facet_conditions(query: ListQuery) -> list[tuple[str | None, str] | None]:
    """``(facet key or None for any, value)``, all of which must hold; ``None`` can never hold."""
    conditions: list[tuple[str | None, str] | None] = []
    for tag in query.tags:
        tag = tag.strip().casefold()
        prefix, colon, rest = tag.partition(":")
        if not colon:
            conditions.append((None, tag))
        elif prefix in TAG_PREFIX_FACETS and rest:
            conditions.append((TAG_PREFIX_FACETS[prefix], rest))
        else:
            conditions.append(None)
    for names, key in (
        (query.pipeline_tag, "task"),
        (query.task_categories, "task"),
        (query.library, "library"),
        (query.language, "language"),
        (query.size_categories, "size"),
    ):
        conditions.extend((key, value.strip().casefold()) for value in names)
    return conditions


def local_scope(query: ListQuery, user):
    scope = filter_readable_repositories(
        Repository.select().where(Repository.repo_type == query.repo_type), user, query.author
    )
    for condition in _facet_conditions(query):
        if condition is None:
            return scope.where(Repository.id < 0)
        scope = _has_facet(scope, *condition)
    return scope


def local_order(query: ListQuery, user) -> list[tuple]:
    """``(sort key..., id)`` of every row the query selects, in the order they are read."""
    scope = local_scope(query, user)
    rows = list(scope.select(Repository.id, Repository.full_id, Repository.created_at, Repository.likes_count, Repository.downloads).tuples())
    if query.search:
        needle = query.search.casefold()
        rows = [row for row in rows if needle in row[1].casefold()]
    ids = [row[0] for row in rows]
    sort = query.sort
    if sort == "updated":
        latest = dict(
            Commit.select(Commit.repository, fn.MAX(Commit.created_at))
            .where((Commit.repo_type == query.repo_type) & (Commit.branch == "main") & (Commit.repository.in_(ids)))
            .group_by(Commit.repository)
            .tuples()
        )
        keys = [(_micros(latest.get(row[0]) or row[2]), row[0]) for row in rows]
    elif sort == "trending":
        scores = calculate_trending_scores(query.repo_type)
        keys = [(round(scores.get(row[0], 0.0), 9), _micros(row[2]), row[0]) for row in rows]
    elif sort == "likes":
        keys = [(row[3], row[0]) for row in rows]
    elif sort == "downloads":
        keys = [(row[4], row[0]) for row in rows]
    else:
        keys = [(_micros(row[2]), row[0]) for row in rows]
    return sorted(keys, reverse=not query.ascending)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def source_id(source: dict) -> str:
    """Names a source in a cursor, so that a changed configuration cannot shift the position."""
    return hashlib.sha256(f"{source['name']}|{source['url']}".encode()).hexdigest()[:12]


@dataclass
class Page:
    items: list[dict]
    next_state: dict | None  # None: this is the last page
    incomplete: list[str] = field(default_factory=list)  # sources skipped because partial results were allowed


def _local_state(keys: list, index: int) -> dict:
    return {"s": "local", "k": list(keys[index])}


async def build_page(
    query: ListQuery,
    user,
    *,
    state: dict | None,
    sources: list[dict],
    render_local,
    fetch=fetch_external_page,
) -> Page:
    """One page of the stream, from ``state`` (``None`` for the start) to the next state.

    ``render_local(ids)`` turns repository ids into the items to answer, in that order.
    """
    segments = ["local"] + [source_id(source) for source in sources]
    where = 0
    inner = None
    if state is not None:
        if state["s"] not in segments:
            raise CursorError("The cursor names a source that is no longer configured")
        where = segments.index(state["s"])
        inner = state.get("k") if state["s"] == "local" else state.get("q")
    need = query.page_size
    items: list[dict] = []
    incomplete: list[str] = []
    listed: set[str] = set()

    for position in range(where, len(segments)):
        if position == 0:
            keys = local_order(query, user)
            start = position_after(keys, tuple(inner) if inner is not None and where == 0 else None, query.ascending)
            taken = keys[start : start + need]
            items.extend(await render_local([key[-1] for key in taken]))
            listed.update(item["id"] for item in items)
            if start + len(taken) < len(keys):
                return Page(items, _local_state(keys, start + len(taken) - 1), incomplete)
            inner = None
            continue

        source = sources[position - 1]
        if len(items) >= need:  # the page ended exactly where the previous segment did
            return Page(items, {"s": segments[position], "q": None}, incomplete)
        resume = inner if position == where else None
        requests = 0
        while True:
            if requests >= MAX_SOURCE_REQUESTS or len(items) >= need:
                return Page(items, {"s": segments[position], "q": resume}, incomplete)
            try:
                page = await fetch(
                    source,
                    query.repo_type,
                    query.external_params(source["source_type"]),
                    limit=min(need - len(items), MAX_PAGE_SIZE),
                    next_query=resume,
                )
            except ExternalListError as failure:
                if not query.partial:
                    raise
                incomplete.append(failure.source)
                break
            requests += 1
            wanted = [item for item in page.items if isinstance(item.get("id"), str) and item["id"] not in listed]
            hub_has = _local_ids(query, user, [item["id"] for item in wanted]) if wanted else set()
            for item in wanted:
                if item["id"] not in hub_has:
                    listed.add(item["id"])
                    items.append(item)
            resume = page.next_query
            if resume is None:
                break
        inner = None
    return Page(items, None, incomplete)


def _local_ids(query: ListQuery, user, full_ids: list[str]) -> set[str]:
    """Which of ``full_ids`` this hub lists itself (a local row wins over a source's copy)."""
    rows = local_scope(query, user).select(Repository.full_id).where(Repository.full_id.in_(full_ids)).tuples()
    return {row[0] for row in rows}
