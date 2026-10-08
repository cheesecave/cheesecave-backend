"""Unit tests for name validation helpers, on real user and repository rows."""

from __future__ import annotations

import pytest

import kohakuhub.api.validation as validation_api
from test.kohakuhub.support.factories import make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


@pytest.mark.asyncio
async def test_check_name_availability_covers_exact_repository_conflict():
    owner = make_user("owner")
    make_repo(owner, "demo-model")

    response = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(
            name="demo-model",
            namespace="owner",
            type="model",
        )
    )

    assert response.available is False
    assert response.conflict_with == "owner/demo-model"
    assert "already exists" in response.message


@pytest.mark.asyncio
async def test_check_name_availability_covers_user_conflict_paths():
    make_user("Taken_User")

    normalized_user = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="taken-user")
    )
    assert normalized_user.available is False
    assert normalized_user.conflict_with == "Taken_User"
    assert "case-insensitive" in normalized_user.message

    make_user("taken-user")

    exact_user = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="taken-user")
    )
    assert exact_user.available is False
    assert exact_user.conflict_with == "taken-user"


@pytest.mark.asyncio
async def test_check_name_availability_covers_org_conflict_and_available_username():
    make_org("acme-team")

    org_conflict = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="acme-team")
    )
    assert org_conflict.available is False
    assert org_conflict.conflict_with == "acme-team"

    available = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="brand-new-user")
    )
    assert available.available is True
    assert available.message == "Name is available"


@pytest.mark.asyncio
async def test_check_name_availability_rejects_reserved_names():
    response = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="Admin")
    )

    assert response.available is False
    assert response.conflict_with == "Admin"
    assert "reserved" in response.message


@pytest.mark.asyncio
async def test_check_name_availability_covers_repository_normalized_conflict_and_available():
    owner = make_user("owner")
    make_repo(owner, "My-Repo")

    normalized = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="my_repo", namespace="owner", type="model")
    )
    assert normalized.available is False
    assert normalized.conflict_with == "owner/My-Repo"
    assert "case-insensitive" in normalized.message

    available = await validation_api.check_name_availability(
        validation_api.CheckNameRequest(name="other-repo", namespace="owner", type="model")
    )
    assert available.available is True
    assert available.message == "Repository name is available"
