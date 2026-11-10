"""Invitation mode stays private through both HF visibility request shapes."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from kohakuhub.api import settings
from kohakuhub.api.admin.routers import repositories as admin_repositories
from kohakuhub.api.repo.routers import crud
from kohakuhub.db import Repository, RepositoryAccessGrant
from kohakuhub.repository_visibility import resolve_visibility
from test.kohakuhub.support.factories import make_repo, make_user


@pytest.mark.parametrize(
    "body,current,expected",
    [
        ({}, (False, False), (False, False)),
        ({}, (True, True), (True, True)),
        ({"private": True}, (True, True), (True, True)),
        ({"visibility": "private"}, (True, True), (True, True)),
        ({"private": False}, (True, True), (False, False)),
        ({"visibility": "public"}, (True, True), (False, False)),
        ({"invite_mode": True}, (False, False), (True, True)),
        ({"private": True, "invite_mode": True}, (False, False), (True, True)),
        ({"visibility": "private", "invite_mode": True}, (False, False), (True, True)),
        ({"invite_mode": False}, (True, True), (True, False)),
        ({"private": True}, (False, False), (True, False)),
        # Preserve the existing HF legacy-field precedence when no invite conflict exists.
        ({"private": True, "visibility": "public"}, (False, False), (True, False)),
    ],
)
def test_visibility_conversion(body, current, expected):
    assert resolve_visibility(
        **body, current_private=current[0], current_invite_mode=current[1]
    ) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"visibility": "invite"},
        {"visibility": "protected"},
        {"private": False, "invite_mode": True},
        {"visibility": "public", "invite_mode": True},
        {"private": True, "visibility": "public", "invite_mode": True},
    ],
)
def test_invalid_or_conflicting_visibility_is_rejected(body):
    with pytest.raises(HTTPException) as caught:
        resolve_visibility(**body)
    assert caught.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"private": True}, {"visibility": "private"}, {}])
async def test_hf_updates_do_not_disable_invitation_mode(db_scope, monkeypatch, body):
    owner = make_user()
    repo = make_repo(owner, "invite", private=True, invite_mode=True)
    await settings.update_repo_settings(
        "model", owner.username, repo.name, settings.UpdateRepoSettingsPayload(**body), user=owner
    )
    current = Repository.get_by_id(repo.id)
    assert current.private is True and current.invite_mode is True


@pytest.mark.asyncio
async def test_public_transition_clears_mode_and_conflicts_apply_nothing(db_scope, monkeypatch):
    owner = make_user()
    repo = make_repo(owner, "invite", private=True, invite_mode=True)
    with pytest.raises(HTTPException):
        await settings.update_repo_settings(
            "model", owner.username, repo.name,
            settings.UpdateRepoSettingsPayload(private=False, invite_mode=True, lfs_keep_versions=7),
            user=owner,
        )
    current = Repository.get_by_id(repo.id)
    assert current.private and current.invite_mode and current.lfs_keep_versions is None
    monkeypatch.setattr(settings, "check_quota", lambda **kwargs: (True, None))
    await settings.update_repo_settings(
        "model", owner.username, repo.name,
        settings.UpdateRepoSettingsPayload(visibility="public"), user=owner,
    )
    current = Repository.get_by_id(repo.id)
    assert current.private is False and current.invite_mode is False


@pytest.mark.asyncio
async def test_enabling_invite_checks_private_quota_and_gated_remains_unsupported(db_scope, monkeypatch):
    owner = make_user()
    repo = make_repo(owner, "public")
    checks = []
    def deny(**kwargs):
        checks.append(kwargs)
        return False, "private quota exhausted"
    monkeypatch.setattr(settings, "check_quota", deny)
    with pytest.raises(HTTPException):
        await settings.update_repo_settings(
            "model", owner.username, repo.name,
            settings.UpdateRepoSettingsPayload(invite_mode=True), user=owner,
        )
    assert checks[0]["is_private"] is True
    assert not Repository.get_by_id(repo.id).private
    response = await settings.update_repo_settings(
        "model", owner.username, repo.name,
        settings.UpdateRepoSettingsPayload(invite_mode=True, gated="manual"), user=owner,
    )
    assert response.status_code == 501
    assert not Repository.get_by_id(repo.id).invite_mode


@pytest.mark.asyncio
async def test_create_writes_invite_mode_and_rejects_conflict_before_storage(db_scope, monkeypatch):
    owner = make_user()
    client = AsyncMock()
    client.get_branch.return_value = {"commit_id": "c0"}
    monkeypatch.setattr(crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(crud, "allocate_lakefs_repo_name", AsyncMock(return_value="model:owner/invite"))
    result = await crud.create_repo(crud.CreateRepoPayload(name="invite", invite_mode=True), user=owner)
    assert result["repo_id"] == "owner/invite"
    row = Repository.get(full_id="owner/invite")
    assert row.private and row.invite_mode
    with pytest.raises(HTTPException):
        await crud.create_repo(
            crud.CreateRepoPayload(name="bad", private=False, invite_mode=True), user=owner
        )
    assert client.create_repository.await_count == 1


@pytest.mark.asyncio
async def test_admin_list_and_detail_distinguish_three_states_without_changing_private_type(db_scope):
    owner = make_user()
    for name, private, mode in (("public", False, False), ("private", True, False), ("invite", True, True)):
        make_repo(owner, name, private=private, invite_mode=mode)
    listed = await admin_repositories.list_repositories_admin(_admin=True)
    for row in listed["repositories"]:
        detail = await admin_repositories.get_repository_admin("model", owner.username, row["name"], _admin=True)
        for record in (row, detail):
            assert record["visibility"] == row["name"]
            assert record["invite_mode"] is (row["name"] == "invite")
            assert record["private"] is (row["name"] != "public")


@pytest.mark.asyncio
async def test_settings_checks_actual_manager_and_read_grant_cannot_change_mode(db_scope):
    owner, reader = make_user(), make_user("reader")
    repo = make_repo(owner, "private", namespace="reader", private=True)
    RepositoryAccessGrant.create(repository=repo, user=reader)
    payload = settings.UpdateRepoSettingsPayload(invite_mode=True)
    with pytest.raises(HTTPException) as denied:
        await settings.update_repo_settings("model", "reader", repo.name, payload, user=reader)
    assert denied.value.status_code == 403
    assert not Repository.get_by_id(repo.id).invite_mode
    await settings.update_repo_settings("model", "reader", repo.name, payload, user=owner)
    assert Repository.get_by_id(repo.id).invite_mode
