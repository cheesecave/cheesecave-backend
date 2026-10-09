"""Unit tests for admin quota routes, on real user rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.admin.routers.quota as admin_quota
from kohakuhub.db import User
from test.kohakuhub.support.factories import make_user

pytestmark = pytest.mark.usefixtures("db_scope")


@pytest.mark.asyncio
async def test_get_quota_reports_real_usage_and_404s_unknown_names():
    make_user("alice", private_quota_bytes=1000)

    got = await admin_quota.get_quota_admin("alice", is_org=False)
    assert got["namespace"] == "alice"
    assert got["is_organization"] is False
    assert got["private_quota_bytes"] == 1000
    assert got["private_used_bytes"] == 0
    assert got["private_available_bytes"] == 1000

    with pytest.raises(HTTPException) as missing:
        await admin_quota.get_quota_admin("ghost", is_org=False)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_get_quota_for_org_does_not_match_a_user_of_the_same_name():
    make_user("team", is_org=False)

    with pytest.raises(HTTPException) as wrong_kind:
        await admin_quota.get_quota_admin("team", is_org=True)
    assert wrong_kind.value.status_code == 404


@pytest.mark.asyncio
async def test_set_quota_writes_the_row_and_404s_unknown_names():
    make_user("org-team", is_org=True)

    updated = await admin_quota.set_quota_admin(
        "org-team",
        admin_quota.SetQuotaRequest(private_quota_bytes=100, public_quota_bytes=200),
        is_org=True,
    )
    assert updated["namespace"] == "org-team"
    assert updated["is_organization"] is True
    assert updated["private_quota_bytes"] == 100
    assert updated["public_quota_bytes"] == 200

    stored = User.get(User.username == "org-team")
    assert (stored.private_quota_bytes, stored.public_quota_bytes) == (100, 200)

    with pytest.raises(HTTPException) as missing:
        await admin_quota.set_quota_admin(
            "ghost",
            admin_quota.SetQuotaRequest(private_quota_bytes=1),
            is_org=True,
        )
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_recalculate_schedules_a_recount_and_404s_unknown_names(monkeypatch):
    make_user("alice")
    scheduled = []
    monkeypatch.setattr(
        admin_quota.usage, "enqueue_recount", lambda namespace=None: scheduled.append(namespace) or 7
    )

    recalculated = await admin_quota.recalculate_quota_admin("alice")
    assert recalculated["namespace"] == "alice"
    assert recalculated["task_id"] == 7
    assert recalculated["already_pending"] is False
    assert scheduled == ["alice"]

    with pytest.raises(HTTPException) as missing:
        await admin_quota.recalculate_quota_admin("ghost")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_recalculate_all_repo_storage_admin_schedules_a_recount(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        admin_quota.usage, "enqueue_recount", lambda namespace=None: scheduled.append(namespace)
    )
    result = await admin_quota.recalculate_all_repo_storage_admin(namespace="owner")
    assert result == {"task_id": None, "already_pending": True}
    assert scheduled == ["owner"]
