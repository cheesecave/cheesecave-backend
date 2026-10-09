"""Tests for XET file lookup helpers, on real repository and file rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.api.xet.utils.file_lookup as file_lookup
from test.kohakuhub.support.factories import make_file, make_repo, make_user


def test_lookup_file_by_sha256_returns_repo_and_file(db_scope):
    repo = make_repo(make_user("owner"), "repo")
    file_record = make_file(repo, "weights.bin", "abc123")
    make_file(repo, "old.bin", "abc123", is_deleted=True)

    actual_repo, actual_file = file_lookup.lookup_file_by_sha256("abc123")

    assert actual_repo == repo
    assert actual_file == file_record


def test_lookup_file_by_sha256_raises_for_missing_file(db_scope):
    make_file(make_repo(make_user("owner"), "repo"), "gone.bin", "abc123", is_deleted=True)

    with pytest.raises(HTTPException) as exc_info:
        file_lookup.lookup_file_by_sha256("deadbeef")

    assert exc_info.value.status_code == 404
    assert "deadbeef" in exc_info.value.detail["error"]

    # a deleted file is not found either
    with pytest.raises(HTTPException) as deleted:
        file_lookup.lookup_file_by_sha256("abc123")
    assert deleted.value.status_code == 404


def test_check_file_read_permission_delegates_to_repo_permission(monkeypatch):
    seen = {}

    def fake_check_repo_read_permission(repo, user):
        seen["repo"] = repo
        seen["user"] = user

    monkeypatch.setattr(file_lookup, "check_repo_read_permission", fake_check_repo_read_permission)

    repo = SimpleNamespace(full_id="owner/repo")
    user = SimpleNamespace(username="owner")

    file_lookup.check_file_read_permission(repo, user)

    assert seen == {"repo": repo, "user": user}
