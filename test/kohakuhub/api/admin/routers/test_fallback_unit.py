"""Unit tests for admin fallback routes, on real SQL rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.admin.routers.fallback as admin_fallback
from kohakuhub.db import FallbackSource
from test.kohakuhub.support.db import table_missing
from test.kohakuhub.support.factories import make_fallback_source


pytestmark = pytest.mark.usefixtures("db_scope")


@pytest.mark.asyncio
async def test_create_then_list_and_get_read_real_rows():
    created = await admin_fallback.create_fallback_source(
        admin_fallback.FallbackSourceCreate(
            namespace="owner",
            url="https://mirror.local/",
            name="Mirror",
            source_type="huggingface",
        )
    )

    assert created.url == "https://mirror.local"
    assert FallbackSource.get_by_id(created.id).namespace == "owner"

    listed = await admin_fallback.list_fallback_sources(namespace="owner")
    assert [item.id for item in listed] == [created.id]

    fetched = await admin_fallback.get_fallback_source(created.id)
    assert fetched.name == "Mirror"


@pytest.mark.asyncio
async def test_create_rejects_unknown_source_type_without_writing():
    with pytest.raises(HTTPException) as exc:
        await admin_fallback.create_fallback_source(
            admin_fallback.FallbackSourceCreate(name="Bad", url="https://x", source_type="ftp")
        )

    assert exc.value.status_code == 400
    assert FallbackSource.select().count() == 0


@pytest.mark.asyncio
async def test_update_and_delete_change_real_rows(monkeypatch):
    source = make_fallback_source(name="Mirror", priority=10)
    cleared = []
    monkeypatch.setattr(admin_fallback, "get_cache", lambda: type("C", (), {"clear": lambda self: cleared.append(1)})())

    updated = await admin_fallback.update_fallback_source(
        source.id, admin_fallback.FallbackSourceUpdate(priority=5, token="new-token")
    )

    assert updated.priority == 5
    assert FallbackSource.get_by_id(source.id).token == "new-token"
    assert cleared == [1]

    await admin_fallback.delete_fallback_source(source.id)
    assert FallbackSource.select().where(FallbackSource.id == source.id).count() == 0


@pytest.mark.asyncio
async def test_update_rejects_invalid_source_type_and_keeps_row():
    source = make_fallback_source(name="Mirror")

    with pytest.raises(HTTPException) as exc:
        await admin_fallback.update_fallback_source(
            source.id, admin_fallback.FallbackSourceUpdate(token="new-token", source_type="invalid")
        )

    assert exc.value.status_code == 400
    assert FallbackSource.get_by_id(source.id).token is None


@pytest.mark.asyncio
async def test_missing_source_returns_404():
    with pytest.raises(HTTPException) as get_exc:
        await admin_fallback.get_fallback_source(9999)
    assert get_exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cache_stats_and_clear_report_cache_failures(monkeypatch):
    # The cache is an in-process dependency, not the database; a deliberate failure here
    # checks the route's error envelope.
    def broken_cache():
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(admin_fallback, "get_cache", broken_cache)

    with pytest.raises(HTTPException) as stats_exc:
        await admin_fallback.get_cache_stats()
    assert stats_exc.value.status_code == 500

    with pytest.raises(HTTPException) as clear_exc:
        await admin_fallback.clear_cache()
    assert clear_exc.value.status_code == 500


@pytest.mark.asyncio
async def test_database_outage_on_create_list_and_get_returns_500(db_scope):
    with table_missing(db_scope, FallbackSource):
        with pytest.raises(HTTPException) as create_exc:
            await admin_fallback.create_fallback_source(
                admin_fallback.FallbackSourceCreate(namespace="owner", url="https://m", name="M", source_type="huggingface")
            )
        assert create_exc.value.status_code == 500

        with pytest.raises(HTTPException) as list_exc:
            await admin_fallback.list_fallback_sources()
        assert list_exc.value.status_code == 500

        with pytest.raises(HTTPException) as get_exc:
            await admin_fallback.get_fallback_source(1)
        assert get_exc.value.status_code == 500


@pytest.mark.asyncio
async def test_database_outage_on_update_and_delete_returns_500(db_scope):
    source = make_fallback_source(name="Mirror")
    with table_missing(db_scope, FallbackSource):
        with pytest.raises(HTTPException) as update_exc:
            await admin_fallback.update_fallback_source(source.id, admin_fallback.FallbackSourceUpdate(name="Broken"))
        assert update_exc.value.status_code == 500

        with pytest.raises(HTTPException) as delete_exc:
            await admin_fallback.delete_fallback_source(source.id)
        assert delete_exc.value.status_code == 500
