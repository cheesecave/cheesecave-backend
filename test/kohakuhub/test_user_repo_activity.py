"""Workspace sidebar ordering applies recent activity before per-type limits."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from kohakuhub.api.repo.routers import info
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import Commit
from test.kohakuhub.test_repository_discovery import catalog, repository


@pytest.fixture
def overview(catalog, monkeypatch):
    monkeypatch.setattr(info, "get_lakefs_client", lambda: object())
    monkeypatch.setattr(info, "_resolve_main_head_via_lakefs", AsyncMock(return_value=(None, None)))
    app = FastAPI()
    app.include_router(info.router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: catalog.owner
    with TestClient(app) as session:
        catalog.session = session
        yield catalog


def add_commit(catalog, repo, stamp, branch="main"):
    Commit.create(
        repository=repo,
        owner=catalog.owner,
        author=catalog.owner,
        username=catalog.owner.username,
        repo_type=repo.repo_type,
        branch=branch,
        commit_id=f"{repo.id}-{branch}",
        message="Activity",
        created_at=stamp,
    )


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
def test_updated_overview_keeps_old_active_repository_before_limit(overview, repo_type):
    catalog = overview
    stamp = datetime(2026, 1, 1)
    active = repository(catalog, "old-active", repo_type)
    for number in range(8):
        idle = repository(catalog, f"new-idle-{number}", repo_type)
        idle.created_at = stamp + timedelta(days=number)
        idle.save()
    add_commit(catalog, active, stamp + timedelta(days=10))
    response = catalog.session.get(
        "/api/users/owner/repos", params={"sort": "updated", "limit": 7, "fallback": False}
    )
    assert response.status_code == 200, response.text
    rows = response.json()[repo_type + "s"]
    assert len(rows) == 7
    assert rows[0]["id"] == active.full_id
    assert [row["id"] for row in rows[1:]] == [
        f"owner/new-idle-{number}" for number in range(7, 1, -1)
    ]
    # Created ordering remains unchanged for existing profile consumers.
    recent = catalog.session.get(
        "/api/users/owner/repos", params={"sort": "recent", "limit": 7, "fallback": False}
    )
    assert active.full_id not in {row["id"] for row in recent.json()[repo_type + "s"]}


def test_updated_overview_uses_creation_without_main_commit(overview):
    catalog = overview
    committed = repository(catalog, "committed")
    add_commit(catalog, committed, datetime(2026, 1, 2))
    fresh = repository(catalog, "fresh")
    fresh.created_at = datetime(2026, 1, 3)
    fresh.save()
    branch_only = repository(catalog, "branch-only")
    add_commit(catalog, branch_only, datetime(2026, 2, 1), branch="draft")
    response = catalog.session.get(
        "/api/users/owner/repos", params={"sort": "updated", "limit": 1, "fallback": False}
    )
    assert response.status_code == 200, response.text
    row = response.json()["models"][0]
    assert row["id"] == fresh.full_id
    assert row["createdAt"].startswith("2026-01-03")


def test_updated_overview_filters_private_repositories_before_limit(overview):
    catalog = overview
    visible = repository(catalog, "visible")
    hidden = repository(catalog, "hidden", private=True)
    add_commit(catalog, hidden, datetime(2026, 1, 2))
    # Public profile read under a different user's session.
    catalog.session.app.dependency_overrides[get_optional_user] = lambda: catalog.outsider
    response = catalog.session.get(
        "/api/users/owner/repos", params={"sort": "updated", "limit": 1, "fallback": False}
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["models"]] == [visible.full_id]
