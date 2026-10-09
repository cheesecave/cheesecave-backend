"""Unit tests for external token routes, on real user, token and fallback source rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.auth.external_tokens as external_tokens_api
from kohakuhub.db import FallbackSource
from kohakuhub.db_operations import get_user_external_tokens, set_user_external_token
from test.kohakuhub.support.db import table_missing
from test.kohakuhub.support.factories import make_fallback_source, make_user


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_scope")
async def test_get_available_fallback_sources_deduplicates_and_handles_database_errors(
    monkeypatch, db_scope
):
    monkeypatch.setattr(
        external_tokens_api.cfg.fallback,
        "sources",
        [
            {"url": "https://hf.local", "priority": 20},
            {
                "url": "https://mirror.local",
                "name": "Mirror",
                "source_type": "kohakuhub",
                "priority": 5,
            },
        ],
    )
    make_fallback_source("Duplicate HF", url="https://hf.local", priority=1)
    make_fallback_source("Database", url="https://db.local", priority=10)

    sources = await external_tokens_api.get_available_fallback_sources()

    assert sources == [
        {
            "url": "https://mirror.local",
            "name": "Mirror",
            "source_type": "kohakuhub",
            "priority": 5,
        },
        {
            "url": "https://db.local",
            "name": "Database",
            "source_type": "huggingface",
            "priority": 10,
        },
        {
            "url": "https://hf.local",
            "name": "Unknown",
            "source_type": "huggingface",
            "priority": 20,
        },
    ]

    # A real failure: the fallback source table is missing for the block.
    with table_missing(db_scope, FallbackSource):
        sources_without_db = await external_tokens_api.get_available_fallback_sources()
    assert [source["url"] for source in sources_without_db] == [
        "https://mirror.local",
        "https://hf.local",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_scope")
async def test_external_token_routes_cover_authorization_and_validation_errors():
    user = make_user("owner")
    other_user = make_user("other")
    set_user_external_token(user, "https://hf.local", "hf_secret")

    with pytest.raises(HTTPException) as list_exc:
        await external_tokens_api.list_external_tokens("owner", user=other_user)
    assert list_exc.value.status_code == 403

    tokens = await external_tokens_api.list_external_tokens("owner", user=user)
    assert tokens[0].token_preview == "hf_s***"

    with pytest.raises(HTTPException) as add_exc:
        await external_tokens_api.add_external_token(
            "owner",
            external_tokens_api.ExternalTokenRequest(url="ftp://invalid", token="secret"),
            user=user,
        )
    assert add_exc.value.status_code == 400

    with pytest.raises(HTTPException) as delete_auth_exc:
        await external_tokens_api.delete_external_token("owner", "https://hf.local", user=other_user)
    assert delete_auth_exc.value.status_code == 403

    # No token is stored for this URL, so the delete finds no row.
    with pytest.raises(HTTPException) as delete_missing_exc:
        await external_tokens_api.delete_external_token("owner", "https://missing.local", user=user)
    assert delete_missing_exc.value.status_code == 404
    assert [token["url"] for token in get_user_external_tokens(user)] == ["https://hf.local"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_scope")
async def test_bulk_update_external_tokens_covers_deletions_authorization_and_invalid_urls():
    user = make_user("owner")
    other_user = make_user("other")
    set_user_external_token(user, "https://old.local", "old_token")
    set_user_external_token(user, "https://keep.local", "keep_old")

    with pytest.raises(HTTPException) as auth_exc:
        await external_tokens_api.bulk_update_external_tokens(
            "owner",
            external_tokens_api.BulkExternalTokensRequest(tokens=[]),
            user=other_user,
        )
    assert auth_exc.value.status_code == 403

    with pytest.raises(HTTPException) as invalid_exc:
        await external_tokens_api.bulk_update_external_tokens(
            "owner",
            external_tokens_api.BulkExternalTokensRequest(
                tokens=[
                    external_tokens_api.ExternalTokenRequest(url="https://keep.local", token="keep"),
                    external_tokens_api.ExternalTokenRequest(url="bad-url", token="bad"),
                ]
            ),
            user=user,
        )
    assert invalid_exc.value.status_code == 400
    # The URL missing from the new list is deleted, and the valid entry before the bad one is saved.
    stored = {token["url"]: token["token"] for token in get_user_external_tokens(user)}
    assert stored == {"https://keep.local": "keep"}
