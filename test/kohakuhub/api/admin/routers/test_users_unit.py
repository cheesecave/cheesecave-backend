"""Unit tests for admin user routes, on real user and repository rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.admin.routers.users as admin_users
from kohakuhub.db import Repository, User
from test.kohakuhub.support.factories import make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


@pytest.mark.asyncio
async def test_get_user_info_reads_the_row_and_404s_unknown_names():
    alice = make_user("alice", private_quota_bytes=100, public_quota_bytes=200)

    payload = await admin_users.get_user_info("alice")
    assert payload.id == alice.id
    assert payload.username == "alice"
    assert payload.is_org is False
    assert payload.private_quota_bytes == 100
    assert payload.private_used_bytes == 0

    with pytest.raises(HTTPException) as not_found:
        await admin_users.get_user_info("missing")
    assert not_found.value.status_code == 404


@pytest.mark.asyncio
async def test_list_users_filters_by_type_and_search_and_counts_before_paging():
    make_user("ali-user")
    make_user("org-team", is_org=True, email=None)
    make_user("bob")

    users_only = await admin_users.list_users(include_orgs=False, limit=10, offset=0)
    assert [item["username"] for item in users_only["users"]] == ["ali-user", "bob"]
    assert users_only["total"] == 2

    with_orgs = await admin_users.list_users(include_orgs=True, limit=1, offset=1)
    assert [item["username"] for item in with_orgs["users"]] == ["org-team"]
    assert with_orgs["total"] == 3

    searched = await admin_users.list_users(search="ali", include_orgs=True)
    assert [item["username"] for item in searched["users"]] == ["ali-user"]
    assert searched["total"] == 1


@pytest.mark.asyncio
async def test_list_users_orders_by_id_before_pagination_and_keeps_total():
    """A user updated after organizations exist must stay in its ID position."""
    for user_id in range(1, 7):
        make_user(f"user-{user_id}", is_org=user_id > 5, email=None if user_id > 5 else f"u{user_id}@x.io")
    User.update(email_verified=True).where(User.username == "user-5").execute()

    first = await admin_users.list_users(include_orgs=True, limit=4, offset=0)
    assert [user["username"] for user in first["users"]] == ["user-1", "user-2", "user-3", "user-4"]
    assert first["total"] == 6

    second = await admin_users.list_users(include_orgs=True, limit=4, offset=4)
    assert [user["username"] for user in second["users"]] == ["user-5", "user-6"]
    assert second["users"][0]["email_verified"] is True
    assert second["total"] == 6


@pytest.mark.asyncio
async def test_create_user_rejects_taken_username_and_email_and_stores_hash():
    make_user("taken", email="taken@example.com")

    with pytest.raises(HTTPException) as username_conflict:
        await admin_users.create_user_admin(
            admin_users.CreateUserRequest(username="taken", email="new@example.com", password="secret")
        )
    assert username_conflict.value.status_code == 400

    with pytest.raises(HTTPException) as email_conflict:
        await admin_users.create_user_admin(
            admin_users.CreateUserRequest(username="bob", email="taken@example.com", password="secret")
        )
    assert email_conflict.value.status_code == 400
    assert User.select().where(User.username == "bob").count() == 0

    created = await admin_users.create_user_admin(
        admin_users.CreateUserRequest(
            username="bob",
            email="bob@example.com",
            password="secret",
            email_verified=True,
            private_quota_bytes=123,
            public_quota_bytes=456,
        )
    )
    assert created.username == "bob"
    assert created.private_quota_bytes == 123
    stored = User.get(User.username == "bob")
    assert stored.email_verified is True
    assert stored.password_hash.startswith("$2")
    assert stored.password_hash != "secret"


@pytest.mark.asyncio
async def test_delete_user_refuses_owners_without_force_and_removes_rows_with_force():
    bob = make_user("bob")
    make_repo(bob, "demo")

    with pytest.raises(HTTPException) as owns_repos:
        await admin_users.delete_user_admin("bob", force=False)
    assert owns_repos.value.status_code == 400
    assert owns_repos.value.detail["owned_repositories"] == ["model:bob/demo"]
    assert User.select().where(User.username == "bob").count() == 1

    deleted = await admin_users.delete_user_admin("bob", force=True)
    assert deleted["deleted_repositories"] == ["model:bob/demo"]
    assert deleted["storage_cleanup"] == "scheduled"
    assert User.select().where(User.username == "bob").count() == 0
    assert Repository.select().where(Repository.full_id == "bob/demo").count() == 0

    with pytest.raises(HTTPException) as missing_user:
        await admin_users.delete_user_admin("ghost")
    assert missing_user.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_without_repositories_reports_no_cleanup():
    make_user("carol")

    deleted = await admin_users.delete_user_admin("carol")
    assert deleted["deleted_repositories"] == []
    assert deleted["storage_cleanup"] == "none"


@pytest.mark.asyncio
async def test_email_verification_and_quota_update_write_the_row_and_404_unknown_names():
    make_user("alice", email="alice@example.com", private_quota_bytes=10, public_quota_bytes=20)

    with pytest.raises(HTTPException) as verification_missing:
        await admin_users.set_email_verification("ghost", True)
    assert verification_missing.value.status_code == 404

    verified = await admin_users.set_email_verification("alice", True)
    assert verified == {"username": "alice", "email": "alice@example.com", "email_verified": True}
    assert User.get(User.username == "alice").email_verified is True

    with pytest.raises(HTTPException) as quota_missing:
        await admin_users.update_user_quota("ghost", admin_users.UpdateQuotaRequest(private_quota_bytes=1))
    assert quota_missing.value.status_code == 404

    updated = await admin_users.update_user_quota(
        "alice",
        admin_users.UpdateQuotaRequest(private_quota_bytes=99, public_quota_bytes=199),
    )
    assert updated == {
        "username": "alice",
        "private_quota_bytes": 99,
        "public_quota_bytes": 199,
        "private_used_bytes": 0,
        "public_used_bytes": 0,
    }
    stored = User.get(User.username == "alice")
    assert (stored.private_quota_bytes, stored.public_quota_bytes) == (99, 199)
