"""The small helpers behind the account routes and the ``Link`` pagination header."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kohakuhub.api.utils import accounts, paging
from kohakuhub.config import cfg


def test_a_timestamp_is_written_in_utc_whether_or_not_it_carries_a_zone():
    naive = datetime(2026, 1, 1, 12, 0, 0, 5)
    aware = naive.replace(tzinfo=timezone(timedelta(hours=8)))

    assert accounts.utc_iso(naive) == "2026-01-01T12:00:00.000005Z"
    assert accounts.utc_iso(aware) == "2026-01-01T04:00:00.000005Z"


@pytest.mark.parametrize(
    "is_org,has_avatar,updated,expected",
    [
        (False, False, None, None),
        (False, True, None, "/api/users/ann/avatar?fallback=false"),
        (True, True, None, "/api/organizations/ann/avatar?fallback=false"),
        (False, True, datetime(2026, 1, 1), "/api/users/ann/avatar?fallback=false&v=2026-01-01T00:00:00.000000Z"),
    ],
)
def test_an_avatar_path_names_the_route_and_busts_the_cache_when_it_changed(is_org, has_avatar, updated, expected):
    assert accounts.avatar_path("ann", is_org, has_avatar, updated) == expected


def test_an_avatar_is_made_absolute_with_the_public_base_url(monkeypatch):
    monkeypatch.setattr(cfg.app, "base_url", "https://hub.example/")
    user = SimpleNamespace(username="ann", is_org=False, avatar=b"jpeg", avatar_updated_at=None)
    bare = SimpleNamespace(username="bob", is_org=False, avatar=None, avatar_updated_at=None)

    assert accounts.avatar_url(user) == "https://hub.example/api/users/ann/avatar?fallback=false"
    assert accounts.avatar_url(bare) is None and accounts.absolute(None) is None


def test_the_next_link_keeps_every_parameter_and_replaces_the_cursor(monkeypatch):
    monkeypatch.setattr(cfg.app, "base_url", "https://hub.example")
    request = SimpleNamespace(
        url=SimpleNamespace(path="/api/users/ann/followers"),
        query_params=SimpleNamespace(multi_items=lambda: [("limit", "5"), ("cursor", "old"), ("x", "1"), ("x", "2")]),
    )

    link = paging.link_header(request, "new")["Link"]

    assert link == '<https://hub.example/api/users/ann/followers?limit=5&x=1&x=2&cursor=new>; rel="next"'
    assert paging.link_header(request, None) == {}
